#!/usr/bin/env python3
"""Run the real gateway, two goblins, the CSV connector and a synthetic Plaid server as separate
processes, then drive everything through the gateway over HTTP. Needs no Plaid account.

    python3 scripts/synthetic_e2e.py

What it proves: our services work together over real HTTP, auth boundaries hold, Plaid-shaped data
flows end to end. What it does NOT prove: that real Plaid behaves like devtools/fake_plaid.py.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
PORTS = {"plaid": 9101, "csv": 9102, "alice": 9103, "gateway": 9104, "bob": 9105}
GW = f"http://127.0.0.1:{PORTS['gateway']}"
procs, results = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond and detail else ""))


def spawn(target, port, env, factory=True):
    e = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", **env}
    cmd = [sys.executable, "-m", "uvicorn", target, "--port", str(port), "--log-level", "warning", "--no-access-log"] + (["--factory"] if factory else [])
    procs.append(subprocess.Popen(cmd, env=e, cwd=ROOT))


def wait(port):
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health" if port != PORTS["plaid"] else f"http://127.0.0.1:{port}/_test/calls", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise SystemExit(f"port {port} never came up")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="goblin-e2e-"))
    secrets = {n: {"internal": f"int-{n}", "conn": f"conn-{n}", "token": f"login-{n}", "field": Fernet.generate_key().decode()} for n in ("alice", "bob")}
    (tmp / "gateway.yaml").write_text(yaml.safe_dump({
        "goblins": [{"name": n, "url": f"http://127.0.0.1:{PORTS[n]}", "internal_secret": secrets[n]["internal"], "user_token_sha256": sha(secrets[n]["token"])} for n in secrets],
        "orb_keys": [{"sha256": sha("orb-key"), "scopes": ["summary:read"]}]}))
    for d in ("rhcsv", "imports", "dbA", "dbB"):
        (tmp / d).mkdir()
    base = {"PLAID_CLIENT_ID": "synthetic-client", "PLAID_SECRET": "synthetic-secret", "PLAID_BASE_URL": f"http://127.0.0.1:{PORTS['plaid']}",
            "GATEWAY_URL": GW, "SHARE": "net_worth"}
    spawn("devtools.fake_plaid:app", PORTS["plaid"], {}, factory=False)
    spawn("goblinbank.main:app", PORTS["csv"], {"ROLE": "rh-csv", "CSV_DIR": str(tmp / "rhcsv"), "INTERNAL_SECRET": secrets["alice"]["conn"]})
    for n, db in (("alice", "dbA"), ("bob", "dbB")):
        spawn("goblinbank.main:app", PORTS[n], {**base, "ROLE": "goblin", "GOBLIN_NAME": n, "INTERNAL_SECRET": secrets[n]["internal"], "CONNECTOR_SECRET": secrets[n]["conn"],
              "FIELD_KEY": secrets[n]["field"], "GOBLIN_DB": str(tmp / db / "goblin.db"), "RH_CSV_URL": f"http://127.0.0.1:{PORTS['csv']}",
              "RH_CSV_DIR": str(tmp / "rhcsv") if n == "alice" else str(tmp / "bob-csv"), "IMPORT_DIR": str(tmp / "imports") if n == "alice" else str(tmp / "bob-imp")})
    spawn("goblinbank.main:app", PORTS["gateway"], {"ROLE": "gateway", "GATEWAY_CONFIG": str(tmp / "gateway.yaml"), "UI_DIR": str(ROOT / "ui")})
    for p in PORTS.values():
        wait(p)

    A = {"Authorization": "Bearer login-alice"}
    B = {"Authorization": "Bearer login-bob"}
    c = httpx.Client(base_url=GW, timeout=20)
    plaid = httpx.Client(base_url=f"http://127.0.0.1:{PORTS['plaid']}", timeout=10)

    # --- auth & isolation ---
    check("gateway serves the UI", "Goblin Bank" in c.get("/").text)
    check("no token -> 401", c.get("/g/alice/api/overview").status_code == 401)
    check("bob cannot open alice's goblin", c.get("/g/alice/api/overview", headers=B).status_code == 401)
    check("goblin refuses direct access without the internal secret", httpx.get(f"http://127.0.0.1:{PORTS['alice']}/api/overview").status_code == 401)

    # --- plaid link + first sync (2 pages) ---
    check("link token", c.post("/g/alice/plaid/link-token", headers=A).json().get("link_token") == "link-sandbox-synthetic")
    bad = c.post("/g/alice/plaid/exchange", headers=A, json={"public_token": "nope", "institution": "Robinhood"})
    check("bad public token -> clean 502", bad.status_code == 502 and "INVALID_PUBLIC_TOKEN" in bad.text, bad.text)
    ex = c.post("/g/alice/plaid/exchange", headers=A, json={"public_token": "public-sandbox-abc", "institution": "Robinhood"}).json()
    check("exchange pulled 4 transactions across 2 pages", ex.get("sync", {}).get("added") == 4, str(ex))
    check("holdings synced", ex["sync"].get("holdings") == 1, str(ex))
    ov = c.get("/g/alice/api/overview", headers=A).json()
    check("net worth = 1250.55 + 8000 + 5000 - 430.20", ov["net_worth"] == 13820.35, str(ov["net_worth"]))
    check("recurring streams became subscriptions", {s["name"] for s in c.get("/g/alice/api/subscriptions", headers=A).json()["items"]} == {"Netflix", "Gym Co"})
    tx = {t["id"]: t["amount"] for t in c.get("/g/alice/api/transactions", headers=A).json()["items"]}
    check("signs flipped (spend negative, pay positive)", tx["t1"] == -15.49 and tx["t2"] == 2400.0, str(tx))
    check("bob sees none of alice's data", c.get("/g/bob/api/accounts", headers=B).json() == [])
    check("robinhood main from Plaid holdings", c.get("/g/alice/robinhood/main", headers=A).json().get("source") == "plaid")

    # --- webhook: signed relay through the gateway ---
    plaid.post("/_test/add_txn")
    hook = plaid.post("/_test/webhook").json()
    h = {"Content-Type": "application/json", "Plaid-Verification": hook["token"]}
    r = c.post("/webhooks/plaid", content=hook["body"], headers=h)
    check("signed webhook relayed, verified, synced", r.status_code == 200 and r.json().get("synced", {}).get("added") == 1, r.text)
    check("tampered webhook (still routed) rejected by signature check", c.post("/webhooks/plaid", content=hook["body"].replace("SYNC_UPDATES_AVAILABLE", "DEFAULT_UPDATE"), headers=h).status_code == 401)
    check("unsigned webhook rejected", c.post("/webhooks/plaid", content=hook["body"], headers={"Content-Type": "application/json"}).status_code == 401)
    check("new transaction appears in money movement", any("Coffee Shop" in e["title"] for e in c.get("/g/alice/api/events", headers=A).json()))

    # --- subscriptions, calendar, settings, orb ---
    s = c.post("/g/alice/api/subscriptions", headers=A, json={"name": "Spotify", "amount": 10.99, "cycle": "monthly", "next_date": "2026-12-09"})
    check("create subscription through gateway", s.status_code == 200, s.text)
    check("update + delete through gateway", c.put(f"/g/alice/api/subscriptions/{s.json()['id']}", headers=A, json={"name": "Spotify", "amount": 11.99, "cycle": "monthly", "next_date": "2026-12-09"}).json()["amount"] == 11.99
          and c.delete(f"/g/alice/api/subscriptions/{s.json()['id']}", headers=A).status_code == 200)
    ics = c.get("/g/alice/subscriptions.ics", headers=A).text
    check("ics export", "BEGIN:VCALENDAR" in ics and "Netflix" in ics)
    st = c.put("/g/alice/api/settings", headers=A, json={"caldav_password": "s3cret", "plaid_rate_item_month": "1.50", "plaid_budget_month": "1"}).json()
    check("secret setting never echoed", st["caldav_password"] == {"set": True} and "s3cret" not in json.dumps(st))
    orb = c.get("/api/v1/summary", headers={"Authorization": "Bearer orb-key"}).json()["goblins"]
    check("orb sees only opted-in totals", orb["alice"] == {"goblin": "alice", "net_worth": 13820.35} and orb["bob"]["goblin"] == "bob", str(orb))
    check("orb key cannot open a goblin", c.get("/g/alice/api/overview", headers={"Authorization": "Bearer orb-key"}).status_code == 401)

    # --- drop folder + robinhood csv ---
    (tmp / "imports" / "cashapp.csv").write_text("Transaction ID,Date,Transaction Type,Currency,Amount,Fee,Net Amount,Asset Type,Account,Notes\nCA1,2026-11-06,Sent,USD,-$8.00,$0,-$8.00,Cash,Cash,Coffee\n")
    sy = c.post("/g/alice/sync", headers=A).json()
    check("drop-folder CSV imported on sync", sy["imports"] and sy["imports"][0]["added"] == 1, str(sy))
    up = c.post("/g/alice/robinhood/csv-upload", headers={**A, "Content-Type": "text/csv"}, content="Activity Date,Instrument,Description,Trans Code,Quantity,Price,Amount\n1/2/2026,AAPL,Apple,Buy,2,$10.00,($20.00)\n")
    check("robinhood CSV upload", up.status_code == 200, up.text)
    c.put("/g/alice/api/settings", headers=A, json={"rh_main_source": "csv"})
    rh = c.get("/g/alice/robinhood/main", headers=A).json()
    check("csv connector (separate process) serves positions", rh.get("source") == "rh-main-csv" and rh["positions"][0]["symbol"] == "AAPL", str(rh))

    # --- usage, cost, cap ---
    u = c.get("/g/alice/api/plaid/usage", headers=A).json()
    server_calls = plaid.get("/_test/calls").json()
    check("usage count matches what the server saw", u["calls_month"] == sum(v for k, v in server_calls.items() if not k.startswith("/_test") and k != "/health"), f"{u['calls_month']} vs {server_calls}")
    check("estimate and over-budget flag", u["est_monthly_cost"] == 1.5 and u["over_budget"] is True, str(u))
    c.put("/g/alice/api/settings", headers=A, json={"plaid_daily_cap": "1"})
    check("daily cap stops further plaid calls", "cap" in c.post("/g/alice/sync", headers=A).json().get("plaid_error", ""))

    # --- unlink ---
    c.put("/g/alice/api/settings", headers=A, json={"plaid_daily_cap": "500"})
    rm = c.delete("/g/alice/api/plaid/items/item-synth-1", headers=A).json()
    check("unlink removes the item and its data", rm.get("accounts") == 4 and not [a for a in c.get("/g/alice/api/accounts", headers=A).json() if a["source"] == "plaid"])
    check("plaid was told to remove the item", plaid.get("/_test/calls").json().get("/item/remove") == 1)

    n = len(results)
    print(f"\n{sum(results)}/{n} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        for p in procs:
            p.terminate()
    sys.exit(code)
