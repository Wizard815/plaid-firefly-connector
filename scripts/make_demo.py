#!/usr/bin/env python3
"""Build a sample-data copy of the UI for previews: seed random data, record every GET the UI makes, embed it.
Usage: python3 scripts/make_demo.py OUT.html [--artifact]   (--artifact strips the doctype/meta lines the artifact viewer adds itself)"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.update({"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "x", "FIELD_KEY": "Zm9yLXRlc3RpbmctZm9yLXRlc3Rpbmctb25seS0xMjM0NTY="})

from cryptography.fernet import Fernet  # noqa: E402
os.environ["FIELD_KEY"] = Fernet.generate_key().decode()
from fastapi.testclient import TestClient  # noqa: E402

from devtools.seed import seed  # noqa: E402
from goblinbank import goblin, ledger, sync  # noqa: E402
from goblinbank.db import DB  # noqa: E402

TODAY = date.today()
H = {"X-Internal-Secret": "x"}


def main(out: str, artifact: bool):
    db = DB(":memory:")
    seed(db, 7, TODAY, months=5)
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY))
    post = lambda p, j: c.post(p, headers=H, json=j).json()
    # friendlier names for the seeded accounts
    db.conn.executemany("UPDATE accounts SET name=?, institution=? WHERE id=?", [("Everyday Checking ••4821", "Huntington", "chk"), ("High Yield Savings", "Axos", "sav"), ("Quicksilver ••0182", "Capital One", "crd"), ("Individual", "Robinhood", "brk")])
    db.conn.execute("UPDATE accounts SET min_balance=500 WHERE id='chk'"); db.conn.commit()
    d = lambda n: (TODAY + timedelta(days=n)).isoformat()
    for s in [dict(name="Netflix", amount=15.49, cycle="monthly", next_date=d(6), account_id="chk", category="Subscriptions"),
              dict(name="Spotify Family", amount=16.99, cycle="monthly", next_date=d(11), account_id="crd", category="Subscriptions"),
              dict(name="Planet Fitness", amount=24.99, cycle="monthly", next_date=d(3), account_id="chk", category="Health"),
              dict(name="State Farm", amount=294.0, cycle="quarterly", next_date=d(29), account_id="chk", category="Bills"),
              dict(name="Domain renewals", amount=54.0, cycle="yearly", next_date=d(160), category="Subscriptions", payment_method="Visa 1234"),
              dict(name="Dad's Hulu", amount=18.99, cycle="monthly", next_date=d(9), payer="other", payer_name="Dad", category="Subscriptions", payment_method="Dad's Visa"),
              dict(name="Trash pickup", amount=45.0, cycle=None, every=3, unit="month", next_date=d(18), account_id="chk", category="Bills"),
              dict(name="City Water", amount=61.25, cycle="monthly", next_date=d(4), account_id="chk", category="Bills")]:
        post("/api/subscriptions", s)
    for s in [dict(name="Ally 12-month CD", kind="cd", institution="Ally", balance=10000, apy=4.8, opened=d(-150), maturity=d(215), term_months=12, goal=12000),
              dict(name="Axos HYSA", kind="savings", institution="Axos", balance=0, apy=4.2, account_id="sav"), dict(name="6-month CD", kind="cd", institution="Huntington", balance=5000, apy=4.5, opened=d(-160), maturity=d(22)),
              dict(name="Emergency jar", kind="cash", balance=800, apy=0, goal=1500)]:
        post("/api/savings", s)
    from goblinbank import recur  # noqa: E402
    for sub in db.all("SELECT * FROM subscriptions WHERE account_id IS NOT NULL AND name != 'City Water'"):     # their payments really did arrive...
        for back in range(1, 6):
            dt = recur.shift(date.fromisoformat(sub["next_date"]), sub["every"], sub["unit"], -back)
            if dt <= TODAY:
                db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)", (f"demo-{sub['id']}-{back}", sub["account_id"], dt.isoformat(), sub["name"].upper(), -sub["amount"], "seed", sub["category"] if sub["category"] in ("Health", "Bills") else "Misc"))
    db.conn.commit()          # ...except City Water, so the sample shows one missed-payment flag
    post("/api/accounts", {"name": "Cash wallet", "kind": "cash", "balance": 140, "min_balance": 20})
    n1 = post("/api/notes", {"title": "Goals for the year", "body": "- Fund the CD ladder\n- Pay the card in full each month\n- Keep checking above $500", "pinned": True})
    ts = sync.now()
    db.conn.execute("INSERT INTO notes(title,body,author,pinned,created,updated) VALUES(?,?,?,?,?,?)", ("Plan: save $500/month (draft)", "Spending last month averaged well above the Food budget.\nTrim dining out by about $120 and groceries by $60,\nthen move $500 on payday into the Axos HYSA.\n(sample note, written by the agent)", "agent", 0, ts, ts)); db.conn.commit()
    for cat, amt in [("Food", 400), ("Groceries", 450), ("Gas", 120), ("Fun", 150)]:
        c.put("/api/budgets", headers=H, json={"category": cat, "monthly_amount": amt})
    post("/api/agent/providers", {"name": "Ollama (this machine)", "kind": "openai", "base_url": "http://host.docker.internal:11434/v1", "model": "llama3.1", "tools": True})
    post("/api/agent/providers", {"name": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "tools": True})
    c.put("/api/settings", headers=H, json={"caldav_url": "https://dav.example/calendars/me/bills/", "caldav_user": "me", "rh_mcp_url": "https://mcp.example/robinhood", "rh_mcp_read_tools": "get_account,get_positions",
                                             "rh_mcp_tool_balances": "get_account", "rh_mcp_tool_positions": "get_positions", "share": ["net_worth", "robinhood_agent"], "plaid_rate_item_month": "1.50", "plaid_budget_month": "10"})
    for i, inst in enumerate(["Huntington", "Axos", "Capital One"]):
        db.conn.execute("INSERT INTO plaid_items VALUES(?,?,?,NULL)", (f"item-{i}", inst, "x"))
    for ep, n in {"/transactions/sync": 96, "/accounts/get": 62, "/transactions/recurring/get": 30, "/investments/holdings/get": 18}.items():
        db.conn.execute("INSERT INTO plaid_usage VALUES(?,?,?)", (TODAY.isoformat(), ep, n))
    db.conn.commit()

    g = lambda p: c.get(p, headers=H).json()
    rec = {"transactions": g("/api/transactions?limit=500")}
    for p in ["/api/overview", "/api/history", "/api/spending", f"/api/spending?start={TODAY:%Y-%m}-01", "/api/categories", "/api/accounts", "/api/subscriptions", "/api/suggestions", "/api/alerts", "/api/savings", "/api/notes",
              "/api/budgets", "/api/agent/providers", "/api/agent/presets", "/api/settings", "/api/plaid/usage", "/api/category-rules"]:
        rec[p] = g(p)
    for dd in list(range(1, 121)) + [365]:
        rec[f"/api/upcoming?days={dd}"] = g(f"/api/upcoming?days={dd}")
    for a in rec["/api/accounts"]:
        rec[f"/api/accounts/{a['id']}"] = g(f"/api/accounts/{a['id']}")
    rec["/api/status"] = {"plaid": {"configured": True, "env": "sandbox", "items": [{"item_id": f"item-{i}", "institution": n} for i, n in enumerate(["Huntington", "Axos", "Capital One"])]},
                          "robinhood": {"agent": "up", "csv": "up", "live": "not deployed", "csv_files": ["import-2026-11-01.csv"], "agent_configured": True}, "caldav_configured": True,
                          "imports": [{"provider": "cashapp", "transactions": 41, "last_transaction": (TODAY - timedelta(days=38)).isoformat()}, {"provider": "paypal", "transactions": 12, "last_transaction": (TODAY - timedelta(days=6)).isoformat()}]}
    pos = lambda rows, eq, src: {"source": src, "as_of": sync.now(), "balances": {"equity": eq}, "positions": rows}
    rec["/robinhood/main"] = pos([{"symbol": "VTI", "quantity": 62.4, "net_cost": 14900}, {"symbol": "AAPL", "quantity": 40, "net_cost": 6120}], 38902.55, "plaid")
    rec["/robinhood/agent"] = pos([{"symbol": "NVDA", "quantity": 4, "net_cost": 540}, {"symbol": "SPY", "quantity": 1.2, "net_cost": 610}], 1204.1, "rh-agent")
    up60 = rec["/api/upcoming?days=60"]; proj = ledger.project_balance(db, 30, TODAY, ["chk"])
    rec["chatProvider"] = rec["/api/agent/providers"]["items"][0]["id"]
    rec["chat"] = {"reply": f"(Sample answer) Over the next 60 days you have {up60['count']} payments totalling ${up60['total']:,.2f}. The largest single bill is {max(up60['items'], key=lambda i: i['amount'])['name']}.\n"
                            f"For checking: it starts at ${proj['start']:,.2f}, and after the bills and your usual paycheck it ends the month near ${proj['end']:,.2f} (lowest ${proj['lowest']:,.2f} on {proj['lowest_date']}).\n"
                            "That estimate leaves out everyday spending.",
                   "trace": [{"tool": "upcoming_bills", "args": {"days": 60}, "ok": True, "preview": ""}, {"tool": "project_balance", "args": {"days": 30, "account_id": "chk"}, "ok": True, "preview": ""}, {"tool": "calculate", "args": {"expression": "6200 - 1200"}, "ok": True, "preview": ""}],
                   "provider": {"id": rec["chatProvider"], "name": "Ollama (this machine)", "model": "llama3.1", "local": True}, "steps": 2}
    rec["/api/budgets?month="] = rec["/api/budgets"]
    html = (ROOT / "ui" / "index.html").read_text()
    html = html.replace("window.GOBLIN_DEMO = null; /*DEMO*/", "window.GOBLIN_DEMO = " + json.dumps(rec, default=str).replace("</", "<\\/") + ";")
    if artifact:
        html = "\n".join(html.split("\n")[2:])
    Path(out).write_text(html)
    print(f"wrote {out} ({len(html) // 1024} KB, {len(rec['transactions']['items'])} transactions)")


if __name__ == "__main__":
    main(sys.argv[1], "--artifact" in sys.argv)
