#!/usr/bin/env python3
"""Build a sample-data copy of the UI for previews: seed random data, record every GET the UI makes, embed it.
Usage: python3 scripts/make_demo.py OUT.html [--artifact]   (--artifact strips the doctype/meta lines the artifact viewer adds itself)"""
import json
import os
import sys
import time
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
    db.conn.execute("UPDATE accounts SET subtype='savings' WHERE id='sav'"); db.conn.commit()
    tot = c.get("/api/goals", headers=H).json()["total"]
    post("/api/goals", {"name": "House down payment", "target": round(tot - 1500, -1), "priority": 1, "deadline": d(400), "monthly": 600, "note": "20% of a $300k house", "auto_allocate": True})
    post("/api/goals", {"name": "Car fund", "target": 1500, "priority": 2, "monthly": 150, "auto_allocate": True})
    cdk = next(p["key"] for p in c.get("/api/goals", headers=H).json()["places"] if p["name"].startswith("Ally"))
    gid = next(g["id"] for g in c.get("/api/goals", headers=H).json()["items"] if g["name"].startswith("House"))
    c.post(f"/api/goals/{gid}/release", headers=H, json={}); c.post(f"/api/goals/{gid}/allocate", headers=H, json={"place": cdk, "amount": 9000}); c.post(f"/api/goals/{gid}/fund", headers=H, json={"amount": round(tot - 1500, -1) - 9000})
    post("/api/goals", {"name": "New laptop", "target": 1200, "priority": 3, "monthly": 250, "deadline": d(120)})      # nothing free left: starts unfunded
    # a linked partner (bob), one goal shared with him (with notes), and one he is offering
    import json as _json
    db.conn.execute("INSERT INTO links VALUES('bob','active',?)", (sync.now(),))
    gid_v = db.conn.execute("INSERT INTO goals(name,target,deadline,priority,monthly,note,status,created,shared_id,members,shared_state,rev,edited_by) VALUES('Family vacation',3200,?,2,200,'','active',?,?,?,'active',2,'bob')",
                            (d(200), TODAY.isoformat(), "demo-shared-vacation-1", _json.dumps(["bob"]))).lastrowid
    db.conn.execute("INSERT INTO goal_partners VALUES(?,?,?)", (gid_v, "bob", 900))
    for nid, who, txt, back in [("n1", "bob", "Found flights for $1,150 for the two of us in July.", 3), ("n2", "alice", "Great. I will put in $100 each payday.", 2)]:
        db.conn.execute("INSERT INTO goal_notes VALUES(?,?,?,?,?,0)", (nid, gid_v, who, (sync.now()[:10] + "T00:00:00+00:00") if back == 0 else (TODAY - timedelta(days=back)).isoformat() + "T12:00:00+00:00", txt))
    db.conn.execute("INSERT INTO goals(name,target,deadline,priority,monthly,note,status,created,shared_id,members,shared_state,rev,edited_by) VALUES('New couch',1400,NULL,3,0,'','active',?,?,?,'offered',1,'bob')", (TODAY.isoformat(), "demo-shared-couch-0001", _json.dumps(["bob"])))
    db.conn.execute("INSERT INTO links VALUES('cara','pending',?)", (sync.now(),))
    db.conn.execute("INSERT INTO link_shares(peer,kind,ref) VALUES('bob','account','sav')")
    db.conn.execute("INSERT INTO shared_in VALUES('bob','account','bob-joint','Joint Savings','Axos','cash',4000,?)", (sync.now(),))
    db.conn.commit()
    inc = c.get("/api/incomes", headers=H).json()
    for sg in inc["suggestions"]:
        if "payroll" in sg["name"].lower():
            post("/api/incomes/accept", {"key": sg["key"], "name": "Acme paycheck"})
    post("/api/incomes", {"name": "Side gig", "amount": 300, "cycle": "monthly", "next_date": d(12)})
    post("/api/tokens", {"name": "Home Assistant", "scopes": ["summary", "bills"]})
    post("/api/tokens", {"name": "Old script", "scopes": ["goals"]})
    c.delete("/api/tokens/2", headers=H)
    post("/api/accounts", {"name": "Cash wallet", "kind": "cash", "balance": 140, "min_balance": 20})
    n1 = post("/api/notes", {"title": "Goals for the year", "body": "- Fund the CD ladder\n- Pay the card in full each month\n- Keep checking above $500", "pinned": True})
    ts = sync.now()
    db.conn.execute("INSERT INTO notes(title,body,author,pinned,created,updated) VALUES(?,?,?,?,?,?)", ("Plan: save $500/month (draft)", "Spending last month averaged well above the Food budget.\nTrim dining out by about $120 and groceries by $60,\nthen move $500 on payday into the Axos HYSA.\n(sample note, written by the agent)", "agent", 0, ts, ts)); db.conn.commit()
    for cat, amt in [("Food", 400), ("Groceries", 450), ("Gas", 120), ("Fun", 150)]:
        c.put("/api/budgets", headers=H, json={"category": cat, "monthly_amount": amt})
    post("/api/agent/providers", {"name": "Ollama (this machine)", "kind": "openai", "base_url": "http://host.docker.internal:11434/v1", "model": "llama3.1", "tools": True})
    post("/api/agent/providers", {"name": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "tools": True})
    c.put("/api/settings", headers=H, json={"caldav_url": "https://dav.example/calendars/me/bills/", "caldav_user": "me", "rh_mcp_url": "https://mcp.example/robinhood", "rh_mcp_read_tools": "get_account,get_positions",
                                             "rh_mcp_tool_balances": "get_account", "rh_mcp_tool_positions": "get_positions", "share": ["net_worth", "bills", "alerts", "savings", "goals", "spending", "robinhood_agent"], "plaid_budget_month": "10"})
    for i, inst in enumerate(["Huntington", "Axos", "Capital One"]):
        db.conn.execute("INSERT INTO plaid_items(item_id,institution,token_enc,cursor) VALUES(?,?,?,NULL)", (f"item-{i}", inst, "x"))
    for ep, n in {"/transactions/sync": 96, "/accounts/get": 62, "/transactions/recurring/get": 30, "/investments/holdings/get": 18}.items():
        db.conn.execute("INSERT INTO plaid_usage VALUES(?,?,?)", (TODAY.isoformat(), ep, n))
    db.conn.commit()

    # a few saved chats so the sample shows the chat list, undo, and compaction
    def chat(title, summary, msgs):
        cid = db.conn.execute("INSERT INTO chats(title,provider_id,summary,created,updated) VALUES(?,?,?,?,?)", (title, 1, summary, ts, ts)).lastrowid
        for role, text, trace, comp in msgs:
            db.conn.execute("INSERT INTO chat_messages(chat_id,role,content,trace,compacted,created) VALUES(?,?,?,?,?,?)", (cid, role, text, json.dumps(trace), comp, ts))
        db.conn.commit()
    chat("Bills for the next two months", "", [("user", "How much will I pay in bills over the next 60 days?", [], 0), ("assistant", "About $576 across 10 payments. State Farm is the biggest at $294 due in a few weeks.", [{"tool": "upcoming_bills", "args": {"days": 60}, "ok": True, "preview": ""}], 0)])
    chat("Add my gym", "", [("user", "please add my gym, $24.99 a month from checking", [], 0), ("assistant", "Done. I added Planet Fitness at $24.99 a month, paid from Everyday Checking.", [{"tool": "add_subscription", "args": {}, "ok": True, "preview": "", "result": {"added": True, "id": 9999, "name": "Planet Fitness", "amount": 24.99, "repeats": "monthly"}}], 0)])
    chat("Saving plan (long chat)", "You are saving toward a CD ladder: $10k CD matures in July, $5k CD matures in December. Checking should stay above $500. Goal: move $500 each payday.",
         [("user", "Where should my first $500 go?", [], 1), ("assistant", "Into the 6-month CD, it matures soonest.", [], 1), ("user", "And the second $500?", [], 0), ("assistant", "Axos HYSA until the CD matures, then roll it over.", [], 0)])
    g = lambda p: c.get(p, headers=H).json()
    rec = {"transactions": g("/api/transactions?limit=500")}
    for p in ["/api/overview", "/api/history", "/api/spending", f"/api/spending?start={TODAY:%Y-%m}-01", "/api/categories", "/api/accounts", "/api/subscriptions", "/api/suggestions", "/api/alerts", "/api/savings", "/api/notes",
              "/api/budgets", "/api/links", "/api/links/people", "/api/requests", "/api/sharing", "/api/agent/providers", "/api/agent/presets", "/api/settings", "/api/plaid/usage", "/api/category-rules"]:
        rec[p] = g(p)
    for dd in list(range(1, 121)) + [365]:
        rec[f"/api/upcoming?days={dd}"] = g(f"/api/upcoming?days={dd}")
    for a in rec["/api/accounts"]:
        rec[f"/api/accounts/{a['id']}"] = g(f"/api/accounts/{a['id']}")
    rec["/api/links/people"] = {"people": ["bob", "cara", "dana"]}
    rec["/api/status"] = {"plaid": {"configured": True, "env": "sandbox", "items": [{"item_id": f"item-{i}", "institution": n, "schedule": "auto", "schedule_effective": se, "recurring": "auto", "recurring_effective": rc, "interval_minutes": im, "last_sync": time.time() - 3600 * lh, "calls_per_day": cpd} for i, (n, se, rc, im, lh, cpd) in enumerate([("Huntington", "daily", True, 1440, 5, 2.1), ("Axos", "weekly", False, 10080, 40, 0.3), ("Capital One", "daily", True, 1440, 5, 2.1)])]},
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
    for p in ["/api/goals", "/api/incomes", "/api/tokens", "/summary", "/api/forecast", "/api/maturities", "/api/notify/status"]:
        rec[p] = g(p)
    for k in range(4):
        y, mth = TODAY.year, TODAY.month - k
        while mth < 1:
            y, mth = y - 1, mth + 12
        rec[f"/api/plan?month={y}-{mth:02d}"] = g(f"/api/plan?month={y}-{mth:02d}")
    rec["models"] = {"models": [{"id": "llama3.1:8b", "context": None}, {"id": "qwen2.5:32b", "context": None}, {"id": "mistral-nemo:12b", "context": None}], "local": True}
    rec["/api/chats"] = g("/api/chats")
    for ch in rec["/api/chats"]:
        rec[f"/api/chats/{ch['id']}"] = g(f"/api/chats/{ch['id']}")
    html = (ROOT / "ui" / "index.html").read_text()
    html = html.replace("window.GOBLIN_DEMO = null; /*DEMO*/", "window.GOBLIN_DEMO = " + json.dumps(rec, default=str).replace("</", "<\\/") + ";")
    if artifact:
        html = "\n".join(html.split("\n")[2:])
    Path(out).write_text(html)
    print(f"wrote {out} ({len(html) // 1024} KB, {len(rec['transactions']['items'])} transactions)")


if __name__ == "__main__":
    main(sys.argv[1], "--artifact" in sys.argv)
