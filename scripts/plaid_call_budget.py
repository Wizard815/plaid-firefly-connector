#!/usr/bin/env python3
"""Measure how many Plaid calls this app really makes, against the real Plaid Sandbox: link two banks (one with an investment account), then
replay a whole day of the 15-minute timer and a month's worth of days, and print the calls and the estimated bill.
    PLAID_CLIENT_ID=... PLAID_SECRET=... python3 scripts/plaid_call_budget.py     (or PLAID_AUTH=header behind a credential-injecting proxy)"""
import os
import sys
from datetime import date
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.update({"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()})
from fastapi.testclient import TestClient  # noqa: E402

from plaidproxy import worker, sync  # noqa: E402
from plaidproxy.db import DB  # noqa: E402
from plaidproxy.plaid import Plaid  # noqa: E402

H = {"X-Internal-Secret": "isec"}


def main():
    from collections import Counter
    counted = Counter()

    class Count:                                           # counts the calls made by the timer-driven pulls below
        def before(self, path): pass
        def record(self, path): counted[path] += 1
    raw = Plaid()
    pulls = Plaid(usage=Count())
    db = DB(":memory:")
    app = TestClient(worker.create_app(db=db, today=date.today))
    now = [1_900_000_000.0]
    sync.clock = lambda: now[0]
    for who, prod in (("Checking bank", "transactions"), ("Brokerage", "investments")):
        pt = raw.post("/sandbox/public_token/create", {"institution_id": "ins_109508", "initial_products": [prod], "options": {"override_username": "user_good", "override_password": "pass_good"}})["public_token"]
        r = app.post("/plaid/exchange", headers=H, json={"public_token": pt, "institution": who})
        assert r.status_code == 200, r.text
    usage = lambda: {k: v for k, v in app.get("/api/plaid/usage", headers=H).json()["by_endpoint"].items()}
    after_link = usage()
    print("calls to link two banks:", sum(after_link.values()), after_link)
    for _ in range(96):                                   # one day of the 15-minute timer
        now[0] += 900
        sync.sync_all(db, pulls)
    delta = dict(counted)
    n_day = sum(delta.values())
    print(f"one more day (96 timer rounds): {n_day} real Plaid calls", delta)
    u = app.get("/api/plaid/usage", headers=H).json()
    print(f"a 30-day month is about {sum(after_link.values()) + 30 * n_day} calls; estimated bill ${u['est_monthly_cost']:.2f} (budget ${u['budget']:.2f}, {u['items']} banks)")
    print("Plaid charges per bank per month for these, not per call: Transactions $0.30, Recurring $0.15, Investments Holdings $0.18.")
    for it in db.all("SELECT item_id FROM plaid_items"):
        app.delete(f"/api/plaid/items/{it['item_id']}", headers=H)


if __name__ == "__main__":
    main()
