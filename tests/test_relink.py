"""A bank removed and linked again: Plaid sends the whole history again under NEW transaction and account ids. Nothing may be sent to Firefly III twice."""
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
    ff = FakeFirefly()
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, firefly_transport=ff.transport()))
    c.put("/api/settings", headers=H, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token"})
    return c, db, ff


def history(n=40):
    """n days of everyday spending, two identical coffees on one day, a paycheck, and a charge that posted a day later than Firefly III has it."""
    rows = [(d(i + 1), f"MERCHANT {i % 7} #{i % 3}", -round(10 + i * 1.37, 2)) for i in range(n)]
    rows += [(d(5), "SQ *COFFEE SHOP", -4.5), (d(5), "SQ *COFFEE SHOP", -4.5), (d(14), "ACME PAYROLL", 2000.0)]
    return rows


def add_account(db, aid, mask="0986", name="Asterisk-Free Checking"):
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,subtype,balance,mask,currency) VALUES(?,?,?,?,?,?,?,?,?)", (aid, "plaid", "Huntington", name, "cash", "checking", 1000.0, mask, "USD"))
    db.conn.commit()


def fill(db, aid, prefix, rows, shift=None):
    for i, (day, name, amt) in enumerate(rows):
        if shift and i in shift:
            day = (date.fromisoformat(day) + timedelta(days=shift[i])).isoformat()
        db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES(?,?,?,?,?,?,?,?)", (f"{prefix}{i}", aid, day, name, amt, "Food", 0, "plaid"))
    db.conn.commit()


def test_relinked_bank_with_new_ids_sends_nothing_twice():
    c, db, ff = rig()
    rows = history()
    # before: the old link sent everything, under the OLD ids
    add_account(db, "old-acct")
    fill(db, "old-acct", "old-", rows)
    assert c.put("/api/firefly/accounts/old-acct", headers=H, json={"mode": "create"}).status_code == 200
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == len(rows)
    target = int(db.one("SELECT ff_id FROM ff_accounts WHERE account_id='old-acct'")["ff_id"])
    before = len(ff.groups)
    # the bank is removed (its data here goes) and linked again: same real account, brand new ids, Plaid's description spacing a little different
    c.delete  # noqa: B018
    with db.tx() as x:
        x.execute("DELETE FROM transactions WHERE account_id='old-acct'"); x.execute("DELETE FROM accounts WHERE id='old-acct'"); x.execute("DELETE FROM ff_accounts WHERE account_id='old-acct'")
    add_account(db, "new-acct")
    fill(db, "new-acct", "new-", [(day, name.replace(" #", "#").lower(), amt) for day, name, amt in rows], shift={3: 1})     # one charge dated a day later this time
    assert c.put("/api/firefly/accounts/new-acct", headers=H, json={"mode": "link", "ff_id": target}).status_code == 200
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["created"] == 0 and r["errors"] == 0 and not r.get("held")
    assert len(ff.groups) == before                                                           # not one transaction doubled
    assert db.one("SELECT COUNT(*) n FROM ff_tx f JOIN transactions t ON t.id=f.tx_id WHERE t.account_id='new-acct'")["n"] == len(rows)
    # a genuinely new purchase after the relink still goes
    fill(db, "new-acct", "later-", [(d(0), "NEW PLACE", -12.0)])
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 1


def test_a_batch_that_would_double_an_account_is_held_until_the_admin_releases_it():
    c, db, ff = rig()
    rows = history(30)
    add_account(db, "old-acct")
    fill(db, "old-acct", "old-", rows)
    c.put("/api/firefly/accounts/old-acct", headers=H, json={"mode": "create"})
    c.post("/api/firefly/sync", headers=H)
    target = int(db.one("SELECT ff_id FROM ff_accounts WHERE account_id='old-acct'")["ff_id"])
    before = len(ff.groups)
    with db.tx() as x:
        x.execute("DELETE FROM transactions WHERE account_id='old-acct'"); x.execute("DELETE FROM accounts WHERE id='old-acct'"); x.execute("DELETE FROM ff_accounts WHERE account_id='old-acct'")
    add_account(db, "new-acct")
    fill(db, "new-acct", "new-", [(day, f"RENAMED BY THE BANK {i}", amt) for i, (day, _n, amt) in enumerate(rows)])   # nothing can be matched
    c.put("/api/firefly/accounts/new-acct", headers=H, json={"mode": "link", "ff_id": target})
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["created"] == 0 and len(ff.groups) == before
    assert r["held"] and r["held"][0]["waiting"] == len(rows) and r["held"][0]["existing"] == len(rows)
    acct = next(a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"] if a["id"] == "new-acct")
    assert acct["held"] == len(rows)
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 0                     # still held on the next run
    r = c.post("/api/firefly/accounts/new-acct/release", headers=H)
    assert r.status_code == 200 and len(ff.groups) == before + len(rows)                     # the admin said so: sent
    assert c.post("/api/firefly/accounts/new-acct/release", headers=H).status_code == 404


def test_first_send_of_a_new_account_and_ordinary_growth_are_never_held():
    c, db, ff = rig()
    add_account(db, "a")
    fill(db, "a", "x-", history(60))
    c.put("/api/firefly/accounts/a", headers=H, json={"mode": "create"})
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 63
    fill(db, "a", "y-", [(d(0), f"SHOP {i}", -5.0) for i in range(15)])                        # a busy week: 15 against 63
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["created"] == 15 and not r["held"]


def test_two_plaid_accounts_that_look_like_one_real_account_need_confirming():
    c, db, ff = rig()
    add_account(db, "sav-1", mask="2568", name="Premier Savings")
    add_account(db, "sav-2", mask="2568", name="Premier  Savings")                           # Plaid sent one real account twice
    add_account(db, "chk", mask="0986")
    accts = {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}
    assert accts["sav-1"]["looks_like"] == ["sav-2"] and accts["sav-2"]["looks_like"] == ["sav-1"] and accts["chk"]["looks_like"] == []
    r = c.put("/api/firefly/accounts/sav-1", headers=H, json={"mode": "create"})
    assert r.status_code == 409 and "same bank account" in r.json()["detail"]
    assert c.put("/api/firefly/accounts/sav-2", headers=H, json={"mode": "skip"}).status_code == 200     # skipping one is always fine
    assert c.put("/api/firefly/accounts/sav-1", headers=H, json={"mode": "create", "confirm_duplicate": True}).status_code == 200
    assert c.put("/api/firefly/accounts/chk", headers=H, json={"mode": "create"}).status_code == 200


def test_adoption_matches_each_firefly_transaction_once():
    """Two identical coffees in Firefly III and three locally: two are taken over, the third is new."""
    db = DB(":memory:")
    ff = FakeFirefly()
    fid = ff.add_account("Checking")
    for g in (500, 501):
        ff.groups[g] = {"transactions": [{"type": "withdrawal", "date": d(3), "amount": "4.50", "description": "Coffee", "source_id": fid, "destination_name": "Coffee", "external_id": f"gone-{g}"}]}
    add_account(db, "a")
    fill(db, "a", "n-", [(d(3), "COFFEE", -4.5)] * 3)
    with db.tx() as x:
        x.execute("INSERT INTO ff_accounts(account_id, mode, ff_id) VALUES('a','link',?)", (fid,))
    cl = firefly.Firefly("https://firefly.example.com", "good-token", ff.transport())
    assert firefly.adopt_existing(db, cl, "a", fid) == 2
    assert db.one("SELECT ff_seen FROM ff_accounts WHERE account_id='a'")["ff_seen"] == 2
    assert {r["ff_id"] for r in db.all("SELECT ff_id FROM ff_tx")} == {500, 501}
