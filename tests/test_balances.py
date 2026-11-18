"""Which Plaid balance is kept: current, current less pending, or available, by account kind."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from plaidproxy import crypto, sync
from plaidproxy.db import DB
from plaidproxy.plaid import Plaid


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FIELD_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PLAID_CLIENT_ID", "c"); monkeypatch.setenv("PLAID_SECRET", "s")


def acct(aid, type_, subtype, current, available, name="Account"):
    return {"account_id": aid, "name": name, "type": type_, "subtype": subtype, "mask": "0001", "balances": {"current": current, "available": available, "iso_currency_code": "USD"}}


def stored(db, aid):
    return db.one("SELECT balance FROM accounts WHERE id=?", (aid,))["balance"]


def pending(db, aid, amount, tid):
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,pending,source) VALUES(?,?,?,?,?,1,'plaid')", (tid, aid, "2026-11-18", "PENDING", amount))
    db.conn.commit()


def test_credit_card_keeps_current_not_the_credit_left():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("cc", "credit", "credit card", 410.0, 9590.0)])
    assert stored(db, "cc") == 410.0


def test_ira_keeps_current_when_available_is_a_zero_sentinel():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("ira", "investment", "ira", 320.76, 0.0)])
    assert stored(db, "ira") == 320.76
    # a cash-type retirement account sending 0 for available: the 0 is ignored too
    sync.upsert_accounts(db, "i", "Bank", [acct("hsa", "depository", "hsa", 6009.0, 0.0)])
    assert stored(db, "hsa") == 6009.0


def test_overdraft_protection_does_not_inflate_checking():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("chk", "depository", "checking", 110.0, 610.0)])     # 500 of overdraft line counted in available
    assert stored(db, "chk") == 110.0


def test_pending_transactions_we_hold_are_taken_off_current():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("chk", "depository", "checking", 1000.0, None)])
    pending(db, "chk", -40.0, "p1"); pending(db, "chk", 15.0, "p2")
    sync.upsert_accounts(db, "i", "Bank", [acct("chk", "depository", "checking", 1000.0, 975.0)])
    assert stored(db, "chk") == 975.0                                      # 1000 - 40 + 15; available agrees
    sync.upsert_accounts(db, "i", "Bank", [acct("chk", "depository", "checking", 1000.0, None)])
    assert stored(db, "chk") == 975.0                                      # no available: still current less pending


def test_stale_current_with_no_pending_trusts_available():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("sav", "depository", "savings", 5000.0, 4200.0)])  # current stuck for days, nothing pending
    assert stored(db, "sav") == 4200.0
    sync.upsert_accounts(db, "i", "Bank", [acct("sav", "depository", "savings", 5000.0, 4999.995)])  # within a cent: not an override
    assert stored(db, "sav") == 5000.0


def test_loans_and_null_current_are_untouched():
    db = DB(":memory:")
    sync.upsert_accounts(db, "i", "Bank", [acct("ln", "loan", "auto", 23211.33, 0.0), acct("x", "depository", "checking", None, 50.0)])
    assert stored(db, "ln") == 23211.33 and stored(db, "x") is None


def test_a_sync_counts_the_pending_charges_that_arrive_in_the_same_pull():
    """The balance is set after every page of transactions is stored, so a pending charge in this very sync counts."""
    db = DB(":memory:")
    db.conn.execute("INSERT INTO plaid_items(item_id,institution,token_enc,cursor) VALUES('i','Bank',?,NULL)", (crypto.encrypt("tok"),))
    db.conn.commit()
    pages = [
        {"added": [], "modified": [], "removed": [], "next_cursor": "c1", "has_more": True, "accounts": [acct("chk", "depository", "checking", 1000.0, 1000.0)]},
        {"added": [{"transaction_id": "t1", "account_id": "chk", "amount": 60.0, "date": "2026-11-18", "name": "GAS", "pending": True}],      # Plaid: positive = money out
         "modified": [], "removed": [], "next_cursor": "c2", "has_more": False, "accounts": [acct("chk", "depository", "checking", 1000.0, 1000.0)]},
    ]
    calls = []

    def h(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/transactions/sync":
            return httpx.Response(200, json=pages[len([c for c in calls if c == "/transactions/sync"]) - 1])
        return httpx.Response(200, json={"outflow_streams": []})
    sync.sync_item(db, Plaid(httpx.MockTransport(h)), "i", force=True)
    assert stored(db, "chk") == 940.0                                      # 1000 less the pending 60 (available 1000 is higher: ignored)
    assert "/accounts/get" not in calls and "/accounts/balance/get" not in calls      # no extra Plaid call
    snaps = db.all("SELECT balance FROM snapshots WHERE account_id='chk'")
    assert [s["balance"] for s in snaps] == [940.0]                         # one snapshot, the final one
