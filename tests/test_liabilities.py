"""A bank loan goes to Firefly III as a liability you owe (as Firefly III 6.7.9 really behaves, checked on a live instance): its balance is the
negative debt, a payment from checking is a withdrawal into it (Firefly III refuses a transfer), interest raises it, and a loan that was
sent to an asset account before can be moved to a liability."""
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


def account(db, aid, kind, balance, name, subtype=None, mask=None):
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,subtype,balance,mask,currency) VALUES(?,?,?,?,?,?,?,?,?)",
                    (aid, "plaid", "Huntington", name, kind, subtype, balance, mask, "USD"))
    db.conn.commit()


def tx(db, tid, aid, day, name, amt, cat="Loan"):
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES(?,?,?,?,?,?,?,?)", (tid, aid, day, name, amt, cat, 0, "plaid"))
    db.conn.commit()


def car_loan(db):
    """Checking with 2,000 and a car loan with 13,637.35 owed (Plaid gives a loan's balance as the positive amount owed). Two payments from
    checking (each seen on both accounts, as Plaid sends them) and one month's interest on the loan."""
    account(db, "chk", "cash", 2000.0, "Checking", "checking", "0000")
    account(db, "car", "loan", 13637.35, "2017 CHEVROLET COLORADO", "auto", "9003")
    for n, day in ((1, d(40)), (2, d(10))):
        tx(db, f"chk-pay{n}", "chk", day, "LOAN PAYMENT", -350.0, "Transfer")
        tx(db, f"car-pay{n}", "car", day, "PAYMENT THANK YOU", 350.0, "Transfer")
    tx(db, "car-int", "car", d(20), "INTEREST CHARGED", -61.42, "Bank fees")
    tx(db, "chk-food", "chk", d(3), "KROGER", -80.0, "Food")


def approve(c, *ids):
    for aid in ids:
        assert c.put(f"/api/firefly/accounts/{aid}", headers=H, json={"mode": "create"}).status_code == 200
    return c.post("/api/firefly/sync", headers=H).json()


def test_a_loan_is_made_as_a_liability_you_owe_and_its_balance_matches_the_bank():
    c, db, ff = rig()
    car_loan(db)
    r = approve(c, "chk", "car")
    assert r["errors"] == 0 and r["accounts_created"] == 2
    m = {x["account_id"]: x for x in db.all("SELECT * FROM ff_accounts")}
    loan = ff.accounts[m["car"]["ff_id"]]
    assert loan["type"] == "liabilities" and loan["liability_direction"] == "debit" and loan["liability_type"] == "loan" and m["car"]["ff_type"] == "liability"
    assert ff.balance(m["car"]["ff_id"]) == -13637.35                                  # Firefly III shows what you owe as a negative balance
    assert ff.balance(m["chk"]["ff_id"]) == 2000.0
    # each payment went ONCE, as a withdrawal from checking into the loan; the interest as a withdrawal from the loan
    kinds = [(t["type"], t.get("source_id"), t.get("destination_id")) for g in ff.groups.values() for t in g["transactions"]]
    pay = ("withdrawal", m["chk"]["ff_id"], m["car"]["ff_id"])
    assert kinds.count(pay) == 2 and ("withdrawal", m["car"]["ff_id"], None) in kinds and len(ff.groups) == 4
    accts = {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}
    assert not accts["car"]["drift"] and not accts["chk"]["drift"] and not accts["car"]["should_be_liability"]
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 0                # nothing goes twice


def test_a_mortgage_is_a_mortgage_and_a_debt_can_be_matched_to_the_bank_again():
    c, db, ff = rig()
    account(db, "home", "loan", 210000.0, "Mortgage", "mortgage")
    tx(db, "h1", "home", d(30), "ESCROW PAYMENT", 1500.0)
    approve(c, "home")
    fid = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='home'")["ff_id"]
    assert ff.accounts[fid]["liability_type"] == "mortgage" and ff.balance(fid) == -210000.0
    db.conn.execute("UPDATE accounts SET balance=209000.0 WHERE id='home'"); db.conn.commit()          # the bank now says less is owed
    assert c.get("/api/firefly/accounts", headers=H).json()["accounts"][0]["drift"] is True
    r = c.post("/api/firefly/accounts/home/rebalance", headers=H)
    assert r.status_code == 200 and r.json()["difference"] == 1000.0 and ff.balance(fid) == -209000.0      # sent with its direction, as Firefly III requires


def test_money_between_two_debts_goes_as_two_halves_not_a_transfer():
    c, db, ff = rig()
    account(db, "car", "loan", 5000.0, "Car loan", "auto")
    account(db, "loc", "loan", 800.0, "Personal loan", "consumer")
    tx(db, "a", "loc", d(5), "ADVANCE", -500.0, "Transfer")
    tx(db, "b", "car", d(5), "PAYMENT", 500.0, "Transfer")
    r = approve(c, "car", "loc")
    assert r["errors"] == 0 and r["created"] == 2 and not any(t["type"] == "transfer" for g in ff.groups.values() for t in g["transactions"])


def test_link_only_to_an_account_that_fits():
    c, db, ff = rig()
    car_loan(db)
    owed_to_me = ff.add_liability("Loan to Bob", "500", direction="credit")
    mine = ff.add_liability("Car loan (mine)", "13000")
    chk = ff.add_account("Checking (hand kept)")
    r = c.put("/api/firefly/accounts/car", headers=H, json={"mode": "link", "ff_id": owed_to_me})
    assert r.status_code == 422 and "I owe this debt" in r.json()["detail"]
    assert c.put("/api/firefly/accounts/chk", headers=H, json={"mode": "link", "ff_id": mine}).status_code == 422
    assert c.put("/api/firefly/accounts/car", headers=H, json={"mode": "link", "ff_id": mine}).status_code == 200
    assert c.put("/api/firefly/accounts/chk", headers=H, json={"mode": "link", "ff_id": chk}).status_code == 200
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["errors"] == 0 and db.one("SELECT ff_type FROM ff_accounts WHERE account_id='car'")["ff_type"] == "liability"
    assert ("withdrawal", chk, mine) in [(t["type"], t.get("source_id"), t.get("destination_id")) for g in ff.groups.values() for t in g["transactions"]]
    listed = c.get("/api/firefly/accounts", headers=H).json()["firefly_accounts"]
    assert {(x["name"], x["type"]) for x in listed} >= {("Car loan (mine)", "liability"), ("Checking (hand kept)", "asset")}


def test_a_loan_sent_to_an_asset_account_before_moves_to_a_liability():
    c, db, ff = rig()
    car_loan(db)
    # how the connector used to do it: the loan as an ASSET account (the "overdrawn" car loan), payments as transfers
    old = ff.add_account("Huntington - 2017 CHEVROLET COLORADO ••9003", opening="-13697.35")
    chk = ff.add_account("Huntington - Checking ••0000", opening="2780")
    with db.tx() as t:
        t.execute("INSERT INTO ff_accounts(account_id,mode,ff_id,created,adopted,ff_type) VALUES('car','create',?,1,1,'asset'),('chk','create',?,1,1,'asset')", (old, chk))
    for g, (kind, s, dst, amt, ext) in enumerate((("transfer", chk, old, 350, "chk-pay1"), ("transfer", chk, old, 350, "chk-pay2"), ("withdrawal", old, None, 61.42, "car-int"),
                                                  ("withdrawal", chk, None, 80, "chk-food")), start=500):
        ff.groups[g] = {"transactions": [{"type": kind, "source_id": s, "destination_id": dst, "amount": str(amt), "external_id": ext, "date": d(1),
                                          "description": ext, "internal_reference": firefly.MARK, "tags": [firefly.MARK]}]}
        with db.tx() as t:
            t.execute("INSERT INTO ff_tx(tx_id,ff_id,state) VALUES(?,?,'ok')", (ext, g))
            if ext.startswith("chk-pay"):
                t.execute("INSERT INTO ff_tx(tx_id,ff_id,state) VALUES(?,?,'paired')", ("car-pay" + ext[-1], g))
    ff.groups[600] = {"transactions": [{"type": "withdrawal", "source_id": old, "amount": "10", "description": "fee I typed in myself", "date": d(2)}]}
    accts = {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}
    assert accts["car"]["should_be_liability"] and not accts["chk"]["should_be_liability"]
    r = c.post("/api/firefly/accounts/car/to-liability", headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["deleted"] == 3 and out["kept"] == 1 and out["firefly"]["errors"] == 0
    new = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='car'")["ff_id"]
    assert ff.accounts[new]["type"] == "liabilities" and ff.accounts[new]["name"] == "Huntington - 2017 CHEVROLET COLORADO ••9003"
    assert ff.accounts[old]["name"].endswith("(old asset)") and ff.accounts[old]["active"] is False and 600 in ff.groups     # off, not deleted; your own entry stays
    assert ff.balance(new) == -13637.35 and ff.balance(chk) == 2000.0                    # the payments now come out of checking into the loan
    pays = [t for g in ff.groups.values() for t in g["transactions"] if t.get("destination_id") == new]
    assert len(pays) == 2 and all(t["type"] == "withdrawal" for t in pays)
    assert c.post("/api/firefly/accounts/car/to-liability", headers=H).status_code == 409             # done: it says so
    assert c.post("/api/firefly/accounts/chk/to-liability", headers=H).status_code == 409             # not a loan
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 0


def test_the_admin_page_may_move_a_loan_and_the_person_may_not(tmp_path, monkeypatch):
    from test_admin_router import A, world, bank
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    bank(db)
    r = gw.post("/admin/p/alice/api/firefly/accounts/plaid-chk/to-liability", headers=A)
    assert r.status_code == 404 and "not in Firefly III" in r.json()["detail"]     # it reached the person's connector (that account is not sent yet)
    r = gw.post("/g/alice/api/firefly/accounts/plaid-chk/to-liability", headers={"Authorization": "Bearer t"})
    assert r.status_code == 403                                                    # the person themselves cannot
