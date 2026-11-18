"""A middleman account (PayPal, Venmo, Cash App) set to monitor: its own rows never reach Firefly III, they name the bank or card charge that paid."""
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from plaidproxy import firefly, ledger, worker
from plaidproxy.db import DB
from tests.fake_firefly import FakeFirefly

H = {"X-Internal-Secret": "isec"}
TODAY = date.today()
d = lambda n: (TODAY - timedelta(days=n)).isoformat()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def rig():
    ff, db = FakeFirefly(), DB(":memory:")
    c = TestClient(worker.create_app(db=db, firefly_transport=ff.transport()))
    c.put("/api/settings", headers=H, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token", "firefly_send_categories": "on"})
    for a in (("chk", "Huntington", "Checking", "cash", "checking", 1000.0), ("pp", "PayPal", "PayPal", "cash", "checking", 0.0)):
        db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,subtype,balance,currency) VALUES(?,?,?,?,?,?,?,'USD')", ("plaid-" + a[0], "plaid") + a[1:])
    db.conn.commit()
    return c, db, ff


def tx(db, tid, acct, days_ago, name, amount, category, plaid_cat=None):
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source,plaid_category) VALUES(?,?,?,?,?,?,0,'plaid',?)",
                    (tid, "plaid-" + acct, d(days_ago), name, amount, category, plaid_cat))
    db.conn.commit()


def mode(c, aid, m):
    r = c.put(f"/api/firefly/accounts/plaid-{aid}", headers=H, json={"mode": m})
    assert r.status_code == 200, r.text


def legs(ff):
    return [t for g in ff.groups.values() for t in g["transactions"]]


def by_ext(ff, ext):
    return next(t for t in legs(ff) if t.get("external_id") == ext)


def sync(c):
    r = c.post("/api/firefly/sync", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_paypal_purchase_is_counted_once_on_the_bank_named_after_the_seller():
    c, db, ff = rig()
    tx(db, "pp1", "pp", 5, "Valve Corporation", -25.00, "Games")
    tx(db, "b1", "chk", 4, "PAYPAL PURCHASE 261021", -25.00, "Transfer")
    tx(db, "b2", "chk", 3, "KROGER #66", -40.00, "Groceries")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    exts = firefly_exts = {t["external_id"] for t in legs(ff)}
    assert firefly_exts == {"b1", "b2"}                                   # PayPal's own row never goes across
    t = by_ext(ff, "b1")
    assert t["type"] == "withdrawal" and t["description"] == "Valve Corporation" and t["destination_name"] == "Valve Corporation"
    assert t["category_name"] == "Games" and "PAYPAL PURCHASE 261021" in t["notes"] and "via-paypal" in t["tags"]
    assert by_ext(ff, "b2")["description"] == "KROGER #66" and "notes" not in by_ext(ff, "b2")    # a charge with no middleman is untouched
    assert len(exts) == 2 and sync(c)["updated"] == 0 and sync(c)["created"] == 0                  # stable: nothing changes on the next runs


def test_the_bank_charge_first_and_paypal_three_days_later_renames_it_in_place():
    c, db, ff = rig()
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    tx(db, "b1", "chk", 1, "PAYPAL INST XFER", -58.40, "Transfer")
    sync(c)
    assert by_ext(ff, "b1")["description"] == "PAYPAL INST XFER"           # sent straight away, so the balance is right
    groups = set(ff.groups)
    tx(db, "pp1", "pp", 4, "AliExpress", -58.40, "Shopping")               # PayPal's row arrives later, dated 3 days before the bank's
    r = sync(c)
    assert r["updated"] == 1 and r["created"] == 0 and set(ff.groups) == groups
    t = by_ext(ff, "b1")
    assert t["description"] == "AliExpress" and t["category_name"] == "Shopping"


def test_outside_the_window_or_a_different_amount_is_not_matched_and_shows_in_the_preview():
    c, db, ff = rig()
    tx(db, "pp1", "pp", 20, "Humble Bundle", -16.56, "Games")
    tx(db, "b1", "chk", 10, "PAYPAL PURCHASE", -16.56, "Transfer")        # 10 days later: too far
    tx(db, "pp2", "pp", 3, "Kenu", -59.46, "Shopping")
    tx(db, "b2", "chk", 3, "PAYPAL PURCHASE", -30.00, "Transfer")         # paid partly from the PayPal balance: amounts differ
    mode(c, "chk", "create"); mode(c, "pp", "create")
    pv = c.get("/api/firefly/accounts/plaid-pp/monitor-preview", headers=H).json()
    assert pv["renamed"] == [] and {b["amount"] for b in pv["bank_unmatched"]} == {-16.56, -30.0} and len(pv["wallet_unmatched"]) == 2
    assert not db.all("SELECT * FROM wallet_match")                        # a preview saves nothing


def test_switching_an_existing_paypal_account_to_monitor_cleans_firefly():
    c, db, ff = rig()
    for i, (n, amt, cat) in enumerate((("Valve Corporation", -10.0, "Games"), ("Patreon", -5.0, "Fun"), ("Cults.", -13.38, "Clothes"))):
        tx(db, f"pp{i}", "pp", 10 - i, n, amt, cat)
        tx(db, f"b{i}", "chk", 9 - i, "PAYPAL PURCHASE", amt, "Transfer")
    mode(c, "chk", "create"); mode(c, "pp", "create")
    sync(c)
    pp_ff = db.one("SELECT ff_id FROM ff_accounts WHERE account_id='plaid-pp'")["ff_id"]
    assert len(legs(ff)) == 6 and {"pp0", "pp1", "pp2"} <= {t["external_id"] for t in legs(ff)}     # the old way: every purchase twice
    pv = c.get("/api/firefly/accounts/plaid-pp/monitor-preview", headers=H).json()
    assert pv["removed_from_firefly"] == 3 and len(pv["renamed"]) == 3 and pv["double_counted"] == 28.38
    assert {r["becomes"] for r in pv["renamed"]} == {"Valve Corporation", "Patreon", "Cults."}
    mode(c, "pp", "monitor")
    r = sync(c)
    assert r["deleted"] == 3 and r["updated"] == 3
    assert {t["external_id"] for t in legs(ff)} == {"b0", "b1", "b2"}
    assert {t["description"] for t in legs(ff)} == {"Valve Corporation", "Patreon", "Cults."}
    assert ff.balance(pp_ff) == 0 and ff.accounts[pp_ff]["include_net_worth"] is False     # PayPal stops going negative and leaves net worth
    mode(c, "pp", "create")                                                 # and back again: its rows return, the charges lose the names
    sync(c)
    assert len(legs(ff)) == 6 and by_ext(ff, "b0")["description"] == "PAYPAL PURCHASE"


def test_pay_in_4_installments_name_the_purchase_they_pay_for():
    c, db, ff = rig()
    tx(db, "buy", "pp", 30, "AliExpress", -58.40, "Shopping")
    for k, ago in enumerate((30, 16, 2)):
        tx(db, f"i{k}", "pp", ago, "Paypal Pay in 4", -14.60, "Bills", "LOAN_PAYMENTS_BNPL")
        tx(db, f"b{k}", "chk", ago - 1, "PAYPAL INST XFER", -14.60, "Transfer")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    assert [by_ext(ff, f"b{k}")["description"] for k in range(3)] == [f"AliExpress - Pay in 4 ({k} of 4)" for k in (1, 2, 3)]
    assert {by_ext(ff, f"b{k}")["category_name"] for k in range(3)} == {"Shopping"}
    assert "buy" not in {t["external_id"] for t in legs(ff)}                # the purchase itself is counted through its installments, once


def test_matches_are_kept_once_made():
    c, db, ff = rig()
    tx(db, "pp1", "pp", 6, "Ae Co.", -10.60, "Clothes")
    tx(db, "b1", "chk", 5, "PAYPAL INST XFER", -10.60, "Transfer")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    tx(db, "pp2", "pp", 5, "Valve Corporation", -10.60, "Games")         # a same-amount row that would be "closer" arrives later
    tx(db, "b2", "chk", 4, "PAYPAL INST XFER", -10.60, "Transfer")
    sync(c)
    assert by_ext(ff, "b1")["description"] == "Ae Co." and by_ext(ff, "b2")["description"] == "Valve Corporation"


def test_money_paypal_sends_on_to_the_bank_says_where_it_came_from():
    c, db, ff = rig()
    tx(db, "in", "pp", 5, "Payment from Bob Henderson Jr", 63.43, "Income")
    tx(db, "out", "pp", 5, "Money Transfer to HUNTINGTON NATIONAL BANK", -63.43, "Transfer")
    tx(db, "b1", "chk", 4, "Money Transfer to HUNTINGTON NATIONAL BANK", 63.43, "Transfer")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    t = by_ext(ff, "b1")
    assert t["type"] == "deposit" and t["description"] == "Payment from Bob Henderson Jr (via PayPal)" and t["category_name"] == "Income"


def test_a_monitor_account_is_never_sent_even_when_it_has_no_matches():
    c, db, ff = rig()
    tx(db, "pp1", "pp", 3, "Some Seller", -12.0, "Shopping")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    assert legs(ff) == [] and firefly.status(db)["waiting"] == 0


@pytest.mark.parametrize("name,amount,plaid,want", [
    ("Anthropic", -20.0, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Tech"),
    ("Openrouter", -10.0, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Tech"),
    ("Kickstarter", -99.0, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Fun"),
    ("INTEREST CHARGE:PURCHASES", -40.0, "BANK_FEES_INTEREST_CHARGE", "Fees"),
    ("INTERNATIONAL TRANSACTION FEE", -0.5, "BANK_FEES_FOREIGN_TRANSACTION_FEES", "Fees"),
    ("L Automotive", -1745.28, "GENERAL_SERVICES_AUTOMOTIVE", "Car"),
    ("Boot.dev Education", -299.25, "GENERAL_SERVICES_EDUCATION", "Education"),
    ("Dorr Truck Stop", -25.2, "GENERAL_SERVICES_POSTAGE_AND_SHIPPING", "Gas"),
    ("Valve Corporation", -10.0, None, "Games"),
    ("KROGER #66", -40.0, "FOOD_AND_DRINK_GROCERIES", "Groceries"),            # a clear bank category still wins
    ("Patreon", -5.0, "ENTERTAINMENT_TV_AND_MOVIES", "Fun"),
    ("Mystery LLC", -9.0, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Misc"),
    ("ACME PAYROLL", 2000.0, "INCOME_WAGES", "Income"),
])
def test_categories(name, amount, plaid, want):
    assert ledger.categorize(DB(":memory:"), name, amount, plaid) == want


def test_your_own_rule_still_wins_and_new_categories_exist():
    db = DB(":memory:")
    db.conn.execute("INSERT INTO category_rules(pattern,category) VALUES('anthropic','Subscriptions')"); db.conn.commit()
    assert ledger.categorize(db, "Anthropic", -20.0, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES") == "Subscriptions"
    assert {"Car", "Tech", "Education", "Fees"} <= {r["name"] for r in db.all("SELECT name FROM categories")}


def test_pay_monthly_installments_name_the_purchase_and_same_day_ties_follow_the_bank_wording():
    c, db, ff = rig()
    tx(db, "sam", "pp", 90, "Samsung Electronics America", -2335.18, "Shopping")
    for k, ago in enumerate((88, 60, 30)):
        tx(db, f"m{k}", "pp", ago, "Pay Monthly", -389.20, "Bills", "LOAN_PAYMENTS_BNPL")
        tx(db, f"bm{k}", "chk", ago - 1, "PAYPAL INST XFER", -389.20, "Transfer")
    tx(db, "ebay", "pp", 40, "eBay", -29.64, "Shopping")
    tx(db, "e1", "pp", 12, "Paypal Pay in 4", -7.41, "Bills", "LOAN_PAYMENTS_BNPL")       # an installment and a direct buy, same amount, same day
    tx(db, "valve", "pp", 12, "Valve Corporation", -7.41, "Games")
    tx(db, "bp", "chk", 11, "PAYPAL PURCHASE 260927", -7.41, "Transfer")
    tx(db, "bi", "chk", 11, "PAYPAL INST XFER 260927", -7.41, "Transfer")
    mode(c, "chk", "create"); mode(c, "pp", "monitor")
    sync(c)
    assert [by_ext(ff, f"bm{k}")["description"] for k in range(3)] == [f"Samsung Electronics America - Pay Monthly ({k} of 6)" for k in (1, 2, 3)]
    assert by_ext(ff, "bp")["description"] == "Valve Corporation" and by_ext(ff, "bi")["description"].startswith("eBay - Pay in 4")
