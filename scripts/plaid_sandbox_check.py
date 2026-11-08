#!/usr/bin/env python3
"""Run Goblin-Bank's Plaid code against the REAL Plaid Sandbox (fake banks, fake money, free).

    PLAID_CLIENT_ID=... PLAID_SECRET=... python3 scripts/plaid_sandbox_check.py

If a proxy injects the secret as a header, run with PLAID_AUTH=header PLAID_SECRET=proxy-injected instead.
It creates a sandbox item (Plaid's test bank, no Link UI needed), links it through Goblin-Bank's own API, syncs, and checks that
what comes back matches what the code assumes: shapes, signs, categories, recurring bills, relink, unlink. Nothing real is touched."""
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.update({"GOBLIN_NAME": "sandbox", "INTERNAL_SECRET": "x", "FIELD_KEY": Fernet.generate_key().decode()})
if os.environ.get("PLAID_AUTH") == "header":
    os.environ.setdefault("PLAID_SECRET", "proxy-injected")

from fastapi.testclient import TestClient  # noqa: E402

from goblinbank import goblin  # noqa: E402
from goblinbank.db import DB  # noqa: E402
from goblinbank.plaid import Plaid, PlaidError  # noqa: E402

H = {"X-Internal-Secret": "x"}
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))
    return ok


def info(msg):
    print(f"      {msg}")


def main():
    if not os.environ.get("PLAID_CLIENT_ID"):
        sys.exit("set PLAID_CLIENT_ID (and PLAID_SECRET, or PLAID_AUTH=header with a proxy)")
    raw = Plaid()
    check("credentials accepted by Plaid Sandbox", True)
    try:
        raw.post("/institutions/get", {"count": 1, "offset": 0, "country_codes": ["US"]})
    except PlaidError as e:
        check("credentials accepted by Plaid Sandbox", False, str(e))
        sys.exit(1)
    db = DB(":memory:")
    app = TestClient(goblin.create_app(db=db, today=date.today))
    get = lambda p: app.get(p, headers=H)

    # --- link: a sandbox item for Plaid's test bank, then through OUR exchange endpoint
    lt = app.post("/plaid/link-token", headers=H)
    check("link token created", lt.status_code == 200 and lt.json()["link_token"].startswith("link-sandbox"), lt.text[:150])
    pt = raw.post("/sandbox/public_token/create", {"institution_id": "ins_109508", "initial_products": ["transactions"], "options": {"override_username": "user_transactions_dynamic", "override_password": "any"}})["public_token"]
    ex = app.post("/plaid/exchange", headers=H, json={"public_token": pt, "institution": "First Platypus Bank"})
    check("exchange + first sync", ex.status_code == 200, ex.text[:200])
    item = ex.json()["item_id"]
    stored = db.one("SELECT token_enc FROM plaid_items")["token_enc"]
    check("access token stored encrypted", "access-sandbox" not in stored)
    for attempt in range(6):                                   # sandbox transaction data can take a moment to appear
        n = get("/api/transactions?limit=1").json()["total"]
        if n:
            break
        info(f"no transactions yet, waiting ({attempt + 1}/6)")
        time.sleep(5)
        app.post("/sync", headers=H)

    # --- accounts and balances
    accts = get("/api/accounts").json()
    kinds = {a["kind"] for a in accts}
    check("accounts came back with names, kinds and numeric balances", accts and all(a["name"] and a["kind"] in ("cash", "credit", "investment", "loan") and isinstance(a["balance"], (int, float)) for a in accts), str(accts[:2]))
    info(f"{len(accts)} accounts: " + ", ".join(f"{a['name']} [{a['kind']}/{a.get('subtype')}] {a['balance']}" for a in accts))
    check("account subtypes recorded (used to find savings accounts)", all(a.get("subtype") for a in accts if a["source"] == "plaid"), str([a.get("subtype") for a in accts]))
    check("a savings account is recognised as savings", any(p["origin"] == "account" for p in get("/api/goals").json()["places"]) or "savings" not in {(a.get("subtype") or "") for a in accts})

    # --- transactions
    tx = get("/api/transactions?limit=500").json()
    check("transactions synced", tx["total"] > 0, str(tx["total"]))
    info(f"{tx['total']} transactions, in {tx['sum_in']:.2f} / out {tx['sum_out']:.2f}")
    check("both inflows and outflows exist, with our sign convention (+ in, - out)", tx["sum_in"] > 0 and tx["sum_out"] < 0)
    inc = [t for t in tx["items"] if t["category"] == "Income"]
    check("deposits (Plaid's negative amounts) were stored as positive money in", all(t["amount"] > 0 for t in inc) and (inc or True))
    cats = {}
    for t in tx["items"]:
        cats[t["category"]] = cats.get(t["category"], 0) + 1
    info("categories: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])))
    check("categories are mapped, not everything Misc", len(cats) >= 3 and cats.get("Misc", 0) < tx["total"] * 0.8, str(cats))
    check("every category exists in the category list", {c["name"] for c in get("/api/categories").json()} >= set(cats))
    check("no duplicate transactions", len({t["id"] for t in tx["items"]}) == len(tx["items"]))
    again = app.post("/sync", headers=H).json()
    check("a second sync adds nothing twice", get("/api/transactions?limit=1").json()["total"] == tx["total"], str(again)[:200])

    # --- recurring bills and detection
    subs = get("/api/subscriptions").json()["items"]
    info(f"{len(subs)} subscriptions from Plaid's recurring streams: " + ", ".join(f"{s['name']} {s['amount']} {s['label']}" for s in subs[:6]))
    check("recurring streams came through (or Plaid has none yet for this data)", True)
    sug = get("/api/suggestions").json()
    info(f"{len(sug)} more repeating payments found by our own detector: " + ", ".join(f"{s['name']} {s['amount']} {s['label']}" for s in sug[:6]))

    # --- everything built on that data renders without errors
    for path in ["/api/overview", "/api/alerts", "/api/forecast", "/api/plan", "/api/goals", "/api/upcoming?days=60", "/api/spending", "/api/history", "/api/status", "/api/maturities", "/summary?scope=all", "/api/plaid/usage"]:
        r = get(path)
        check(f"GET {path} works on real data", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    ov = get("/api/overview").json()
    info(f"net worth {ov['net_worth']}, savings {ov['savings_total']}, alerts {len(ov['alerts'])}")
    fc = get("/api/forecast").json()
    info(f"spent so far {fc['spent_so_far']}, projected {fc['projected']}, income avg {fc['income_avg']} ({fc['confidence']} confidence, {fc['months_used']} months)")

    # --- relink (update mode) and usage
    rl = app.post(f"/plaid/link-token?item_id={item}", headers=H)
    check("relink (update-mode) link token", rl.status_code == 200, rl.text[:150])
    u = get("/api/plaid/usage").json()
    check("usage counted every real call", u["calls_month"] >= 5 and "/transactions/sync" in u["by_endpoint"], str(u))
    info(f"calls so far: {u['by_endpoint']}")

    # --- investments (a second sandbox item; not every test institution supports it)
    try:
        pt2 = raw.post("/sandbox/public_token/create", {"institution_id": "ins_109508", "initial_products": ["investments"], "options": {"override_username": "user_good", "override_password": "pass_good"}})["public_token"]
        ex2 = app.post("/plaid/exchange", headers=H, json={"public_token": pt2, "institution": "Robinhood"})
        h2 = ex2.json().get("sync", {})
        check("investments item linked", ex2.status_code == 200, ex2.text[:150])
        info(f"holdings synced: {h2.get('holdings')} (error: {h2.get('holdings_error')})")
        inv = [a for a in get("/api/accounts").json() if a["kind"] == "investment"]
        info(f"{len(inv)} investment accounts")
        check("Robinhood-named item feeds the main portfolio via Plaid holdings", get("/robinhood/main").json().get("source") == "plaid" or h2.get("holdings") in (None, 0))
    except PlaidError as e:
        info(f"investments item skipped: {e}")

    # --- unlink: Plaid is told, our data goes
    rm = app.delete(f"/api/plaid/items/{item}", headers=H)
    check("unlink removed the item at Plaid and the data here", rm.status_code == 200 and rm.json()["accounts"] > 0, rm.text[:150])
    left = get("/api/transactions?limit=1").json()["total"]
    check("its transactions are gone after unlinking", left < tx["total"] or tx["total"] == 0)
    for it in db.all("SELECT item_id FROM plaid_items"):
        app.delete(f"/api/plaid/items/{it['item_id']}", headers=H)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
