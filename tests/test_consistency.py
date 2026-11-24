"""Firefly III ends up with exactly what the bank has: a long first history goes over in several rounds without being held, and amounts
finer than a cent (Plaid's sandbox sends -52.34218) do not leave Firefly III's balance a cent off."""
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from plaidproxy import firefly, worker
from plaidproxy.db import DB
from fake_firefly import FakeFirefly

H = {"X-Internal-Secret": "isec"}
d = lambda n: (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def rig():
    ff, db = FakeFirefly(), DB(":memory:")
    c = TestClient(worker.create_app(db=db, firefly_transport=ff.transport()))
    c.put("/api/settings", headers=H, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token"})
    return c, db, ff


def account(db, aid, kind, balance, rows):
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,currency) VALUES(?,?,?,?,?,?,?)", (aid, "plaid", "Bank", aid, kind, balance, "USD"))
    for i, (day, amt) in enumerate(rows):
        db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES(?,?,?,?,?,?,?,?)", (f"{aid}-{i}", aid, day, f"SHOP {i}", amt, "Food", 0, "plaid"))
    db.conn.commit()


@pytest.mark.parametrize("kind", ["cash", "loan"])
def test_a_long_first_history_goes_over_in_rounds_and_is_never_held(kind):
    c, db, ff = rig()
    account(db, "a", kind, 9000.0, [(d(i % 300 + 1), -round(5 + i * 0.01, 2)) for i in range(700)])     # more than one round (300 per round)
    assert c.put("/api/firefly/accounts/a", headers=H, json={"mode": "create"}).status_code == 200
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["held"] == [] and r["errors"] == 0 and r["created"] == 700 and r["remaining"] == 0
    fid = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='a'")["ff_id"]
    assert ff.balance(fid) == (-9000.0 if kind == "loan" else 9000.0)
    assert c.get("/api/firefly/accounts", headers=H).json()["accounts"][0]["drift"] is False


def test_amounts_finer_than_a_cent_leave_no_cent_off():
    c, db, ff = rig()
    rows = [(d(i + 1), a) for i, a in enumerate((-52.34218, -70.61283, -21.771215, -15.607323, 519.6777, -6.0424743, -9.7853775))]
    account(db, "chk", "cash", -56.62, rows)
    c.put("/api/firefly/accounts/chk", headers=H, json={"mode": "create"})
    c.post("/api/firefly/sync", headers=H)
    fid = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='chk'")["ff_id"]
    assert ff.balance(fid) == -56.62 and c.get("/api/firefly/accounts", headers=H).json()["accounts"][0]["drift"] is False


def test_a_loan_whose_history_does_not_add_up_starts_at_zero_and_says_its_balance_differs():
    c, db, ff = rig()
    account(db, "loc", "loan", 500.0, [(d(i + 1), -100.0) for i in range(20)])       # 500 owed now, but 2,000 charged and nothing paid in the history
    c.put("/api/firefly/accounts/loc", headers=H, json={"mode": "create"})
    assert c.post("/api/firefly/sync", headers=H).json()["errors"] == 0
    fid = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='loc'")["ff_id"]
    assert float(ff.accounts[fid]["opening_balance"]) == 0 and ff.balance(fid) == -2000.0          # never a flipped sign
    assert c.get("/api/firefly/accounts", headers=H).json()["accounts"][0]["drift"] is True
    r = c.post("/api/firefly/accounts/loc/rebalance", headers=H)
    assert r.status_code == 502 and "less is owed" in r.json()["detail"]


# ------------------------------------------------------------------ reading Plaid: a bank whose data changes mid-pull
from plaidproxy import crypto, sync
from plaidproxy.plaid import PlaidError


class ChangingBank:
    """Two pages; the bank's data changes once while the second page is asked for (Plaid's sandbox does this often)."""
    def __init__(self, fail_times=1):
        self.calls, self.fail_times = [], fail_times

    def sync(self, token, cursor):
        self.calls.append(cursor)
        acc = [{"account_id": "chk", "name": "Checking", "type": "depository", "subtype": "checking", "mask": "1", "balances": {"current": 70.0, "available": None, "iso_currency_code": "USD"}}]
        tx = lambda i, amt: {"transaction_id": f"t{i}", "account_id": "chk", "date": "2026-11-01", "name": f"SHOP {i}", "amount": amt, "pending": False}
        if cursor in (None, "c0"):
            return {"accounts": acc, "added": [tx(1, 10.0), tx(2, 20.0)], "modified": [], "removed": [], "next_cursor": "c1", "has_more": True}
        if self.fail_times:
            self.fail_times -= 1
            raise PlaidError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION: Underlying transaction data changed since last page was fetched.")
        return {"accounts": acc, "added": [tx(3, 0.5)], "modified": [], "removed": [], "next_cursor": "c2", "has_more": False}

    def recurring(self, *a, **k):
        raise PlaidError("not needed here")


def bank_item(db):
    with db.tx() as c:
        c.execute("INSERT INTO plaid_items(item_id, token_enc, institution, cursor) VALUES('i1', ?, 'Bank', 'c0')", (crypto.encrypt("access-x"),))


def test_a_pull_starts_again_when_the_bank_changes_mid_way_and_nothing_is_doubled(monkeypatch):
    monkeypatch.setenv("FIELD_KEY", Fernet.generate_key().decode())
    db = DB(":memory:")
    bank_item(db)
    p = ChangingBank(fail_times=1)
    out = sync.sync_item(db, p, "i1", force=True)
    assert "error" not in out and p.calls == ["c0", "c1", "c0", "c1"]               # started again from where this pull began
    assert db.one("SELECT COUNT(*) n FROM transactions")["n"] == 3 and db.one("SELECT cursor FROM plaid_items")["cursor"] == "c2"


def test_a_bank_that_keeps_changing_gives_up_after_a_few_tries_and_keeps_its_place(monkeypatch):
    monkeypatch.setenv("FIELD_KEY", Fernet.generate_key().decode())
    db = DB(":memory:")
    bank_item(db)
    with pytest.raises(PlaidError):
        sync.sync_item(db, ChangingBank(fail_times=99), "i1", force=True)
    assert db.one("SELECT cursor FROM plaid_items")["cursor"] == "c0"               # the next pull starts from the same place again
