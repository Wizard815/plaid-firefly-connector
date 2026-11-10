"""Saying what each account is for, and the manual scan for subscriptions you have not tracked yet."""
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import goblin, plaidplan
from goblinbank.db import DB

H = {"X-Internal-Secret": "isec"}
TODAY = date(2026, 11, 7)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def app():
    db = DB(":memory:")
    return TestClient(goblin.create_app(db=db, today=lambda: TODAY)), db


def tx(db, acct, name, amount, days_ago, tid):
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)", (tid, acct, (TODAY - timedelta(days=days_ago)).isoformat(), name, amount, "csv", "Misc"))


# ---------------------------------------------------------------- the manual scan
def test_the_scan_finds_newer_repeats_the_automatic_pass_misses_and_can_be_limited_to_one_account():
    c, db = app()
    for a in ("a1", "a2"):
        db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES(?,?,?,?,?,?)", (a, "manual", "Axos", a, "cash", 100))
    for i, d in enumerate((70, 40, 10)):
        tx(db, "a1", "OLD GYM CLUB", -29.99, d, f"g{i}")                  # three months: the automatic pass already finds this one
    for i, d in enumerate((35, 5)):
        tx(db, "a1", "NEWFLIX STREAMING", -15.49, d, f"n{i}")             # only two so far: automatic detection waits for a third
    for i, d in enumerate((21, 14, 7)):
        tx(db, "a2", "WEEKLY FARM BOX", -32.00, d, f"w{i}")               # three weekly: needs four normally
    tx(db, "a2", "RANDOM SHOP", -12.00, 3, "r0"); tx(db, "a2", "RANDOM SHOP", -45.00, 50, "r1")
    db.conn.commit()
    auto = {s["name"] for s in c.get("/api/suggestions", headers=H).json()}
    assert "Old Gym Club" in auto and "Newflix Streaming" not in auto and "Weekly Farm Box" not in auto
    r = c.post("/api/subscriptions/scan", headers=H, json={}).json()
    names = {f["name"] for f in r["found"]}
    assert {"Old Gym Club", "Newflix Streaming", "Weekly Farm Box"} <= names and "Random Shop" not in names
    assert r["transactions_checked"] == 10 and all(f["source"] == "scan" for f in r["found"])
    one = c.post("/api/subscriptions/scan", headers=H, json={"account_id": "a2"}).json()
    assert {f["name"] for f in one["found"]} == {"Weekly Farm Box"} and one["transactions_checked"] == 5
    assert c.post("/api/subscriptions/scan", headers=H, json={"account_id": "nope"}).status_code == 404
    strict = c.post("/api/subscriptions/scan", headers=H, json={"thorough": False}).json()
    assert {f["name"] for f in strict["found"]} == {"Old Gym Club"}                                  # the normal sensitivity is the automatic one


def test_scan_results_can_be_added_dismissed_and_brought_back():
    c, db = app()
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('a1','manual','M','A','cash',1)")
    for i, d in enumerate((35, 5)):
        tx(db, "a1", "NEWFLIX STREAMING", -15.49, d, f"n{i}")
    for i, d in enumerate((50, 20)):
        tx(db, "a1", "MAYBE SERVICE", -9.99, d, f"m{i}")
    db.conn.commit()
    found = {f["name"]: f for f in c.post("/api/subscriptions/scan", headers=H, json={}).json()["found"]}
    assert set(found) == {"Newflix Streaming", "Maybe Service"}
    r = c.post("/api/suggestions/accept", headers=H, json={"key": found["Newflix Streaming"]["key"], "name": "Newflix"})
    assert r.status_code == 200 and r.json()["name"] == "Newflix"
    c.post("/api/suggestions/dismiss", headers=H, json={"key": found["Maybe Service"]["key"]})
    after = c.post("/api/subscriptions/scan", headers=H, json={}).json()
    assert after["found"] == [] and after["already_tracked"] == 1 and after["previously_dismissed"] == 1
    back = c.post("/api/subscriptions/scan", headers=H, json={"include_dismissed": True}).json()
    assert [f["name"] for f in back["found"]] == ["Maybe Service"] and back["found"][0]["dismissed"] is True


# ---------------------------------------------------------------- what each account is for
def huntington(c, db):
    db.conn.execute("INSERT INTO plaid_items(item_id,institution,token_enc) VALUES('i1','Huntington','x')")
    for i, (n, sub) in enumerate([("Checking 1", "checking"), ("Checking 2", "checking"), ("Checking 3", "checking"), ("Savings 1", "savings")]):
        db.conn.execute("INSERT INTO accounts(id,item_id,source,institution,name,kind,subtype,balance) VALUES(?,?,?,?,?,?,?,?)", (f"h{i}", "i1", "plaid", "Huntington", n, "cash", sub, 100.0 * (i + 1)))
    db.conn.commit()


def test_a_bank_gets_a_category_and_each_of_its_accounts_can_have_its_own():
    c, db = app()
    huntington(c, db)
    cats = c.get("/api/account-categories", headers=H).json()
    assert "Checking" in cats and "Emergency fund" in cats
    assert c.put("/api/plaid/items/i1", headers=H, json={"category": "x" * 41}).status_code == 422
    r = c.put("/api/plaid/items/i1", headers=H, json={"category": "Checking", "apply_to_accounts": True}).json()
    assert [a["category"] for a in r["accounts"]] == ["Checking"] * 4 and r["category"] == "Checking"
    # say what each is: bills, spending, the main one, and the savings one
    for aid, cat in (("h0", "Bills"), ("h1", "Spending"), ("h3", "Emergency fund"), ("h2", "Joint & family")):
        assert c.put(f"/api/accounts/{aid}", headers=H, json={"category": cat}).status_code == 200
    accts = {a["id"]: a for a in c.get("/api/accounts", headers=H).json()}
    assert [accts[k]["category_effective"] for k in ("h0", "h1", "h2", "h3")] == ["Bills", "Spending", "Joint & family", "Emergency fund"]
    assert "Joint & family" in c.get("/api/account-categories", headers=H).json()                  # your own names are remembered
    assert c.put("/api/accounts/h0", headers=H, json={"category": ""}).status_code == 200           # clearing falls back to the bank's category
    assert {a["id"]: a for a in c.get("/api/accounts", headers=H).json()}["h0"]["category_effective"] == "Checking"


def test_what_you_called_an_account_decides_whether_it_counts_as_savings_and_how_often_its_bank_is_asked():
    c, db = app()
    huntington(c, db)
    names = lambda: {p["name"] for p in c.get("/api/goals", headers=H).json()["places"]}
    assert names() == {"Savings 1"}                                                                # the bank's own label, to start with
    c.put("/api/accounts/h2", headers=H, json={"category": "Emergency fund"})                      # a "checking" you keep as a cushion
    c.put("/api/accounts/h3", headers=H, json={"category": "Spending"})                            # a "savings" you really spend from
    assert names() == {"Checking 3"}
    row = db.one("SELECT * FROM plaid_items WHERE item_id='i1'")
    assert plaidplan.schedule_of(db, row) == "daily"                                                # it still holds spending accounts
    for aid in ("h0", "h1", "h3"):
        c.put(f"/api/accounts/{aid}", headers=H, json={"category": "Savings"})
    assert plaidplan.schedule_of(db, db.one("SELECT * FROM plaid_items WHERE item_id='i1'")) == "weekly"       # all savings now: the quiet schedule
    c.put("/api/plaid/items/i1", headers=H, json={"category": "Retirement", "apply_to_accounts": True})
    assert plaidplan.schedule_of(db, db.one("SELECT * FROM plaid_items WHERE item_id='i1'")) == "monthly"
