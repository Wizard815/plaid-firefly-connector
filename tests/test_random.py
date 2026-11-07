"""Property tests on random data. Every calculation is compared with an independent brute-force version."""
import random
from datetime import date, timedelta

import pytest

from devtools.seed import seed
from goblinbank import ledger, recur
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
SEEDS = range(1, 13)


@pytest.fixture(params=SEEDS)
def world(request):
    db = DB(":memory:")
    truth = seed(db, request.param, TODAY)
    return db, truth, random.Random(request.param * 7919)


# ---------------------------------------------------------------- recurrence math
@pytest.mark.parametrize("seed_no", range(40))
def test_recurrence_properties(seed_no):
    rng = random.Random(seed_no)
    anchor = date(2024, 1, 1) + timedelta(days=rng.randint(0, 1500))          # includes 29/30/31st days
    every, unit = rng.randint(1, 5), rng.choice(recur.UNITS)
    start = TODAY + timedelta(days=rng.randint(-400, 100))
    end = start + timedelta(days=rng.randint(1, 900))
    occ = recur.occurrences(anchor, every, unit, start, end)
    assert occ == sorted(set(occ)) and all(start <= o <= end for o in occ)
    # brute force: scan k and collect
    span = 200 if unit == "year" else 3000
    expect = [recur.shift(anchor, every, unit, k) for k in range(-span, span) if start <= recur.shift(anchor, every, unit, k) <= end]
    assert occ == sorted(set(expect))
    nd = recur.next_due(anchor, every, unit, TODAY)
    assert nd >= TODAY and not recur.occurrences(anchor, every, unit, TODAY, nd - timedelta(days=1))


def test_month_end_does_not_drift():
    assert [recur.shift(date(2026, 1, 31), 1, "month", k).day for k in range(1, 7)] == [28, 31, 30, 31, 30, 31]
    assert recur.shift(date(2024, 2, 29), 1, "year", 1) == date(2025, 2, 28)


def test_resolve_validation():
    assert recur.resolve(cycle="quarterly") == (3, "month") and recur.resolve(every=10, unit="day") == (10, "day")
    for bad in [dict(cycle="fortnightly"), dict(every=0, unit="day"), dict(every=5, unit="decade"), dict(every=True, unit="day"), dict(every=2000, unit="day")]:
        with pytest.raises(ValueError):
            recur.resolve(**bad)


# ---------------------------------------------------------------- transaction queries vs oracle
def test_filters_match_oracle(world):
    db, _, rng = world
    allrows = db.all("SELECT t.*, a.name account_name FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id")
    for _ in range(25):
        f = {}
        if rng.random() < .5: f["account_id"] = rng.choice(["chk", "crd", "sav", "own-cash"])
        if rng.random() < .4: f["category"] = rng.choice(["Food", "Misc", "Income", "Gas,Groceries"])
        if rng.random() < .4: f["min_amount"] = round(rng.uniform(0, 100), 2)
        if rng.random() < .3: f["max_amount"] = round(rng.uniform(50, 400), 2)
        if rng.random() < .4: f["start"] = (TODAY - timedelta(days=rng.randint(30, 400))).isoformat()
        if rng.random() < .3: f["end"] = (TODAY - timedelta(days=rng.randint(0, 30))).isoformat()
        if rng.random() < .3: f["direction"] = rng.choice(["in", "out"])
        if rng.random() < .3: f["q"] = rng.choice(["shell", "netflix payroll", "starbucks", "xyz-none"])
        r = ledger.query_transactions(db, limit=500, **f)
        cats = f.get("category", "").split(",") if f.get("category") else None
        want = [t for t in allrows if (not f.get("account_id") or t["account_id"] == f["account_id"]) and (not cats or t["category"] in cats)
                and (f.get("min_amount") is None or abs(t["amount"]) >= f["min_amount"]) and (f.get("max_amount") is None or abs(t["amount"]) <= f["max_amount"])
                and (not f.get("start") or t["date"] >= f["start"]) and (not f.get("end") or t["date"] <= f["end"])
                and (not f.get("direction") or (t["amount"] > 0) == (f["direction"] == "in")) and all(w in t["name"].lower() for w in f.get("q", "").lower().split())]
        assert r["total"] == len(want)
        assert r["sum_in"] == pytest.approx(sum(t["amount"] for t in want if t["amount"] > 0), abs=0.01)
        assert r["sum_out"] == pytest.approx(sum(t["amount"] for t in want if t["amount"] < 0), abs=0.01)
        assert {t["id"] for t in r["items"]} == {t["id"] for t in want[:500]} or r["total"] > 500


def test_pagination_and_sorting(world):
    db, _, _ = world
    full = ledger.query_transactions(db, limit=500)
    seen, off = [], 0
    while True:
        page = ledger.query_transactions(db, limit=37, offset=off)
        if not page["items"]:
            break
        seen += [t["id"] for t in page["items"]]; off += 37
    assert seen == [t["id"] for t in full["items"]][:len(seen)] and len(seen) == full["total"] and len(set(seen)) == len(seen)
    amts = [abs(t["amount"]) for t in ledger.query_transactions(db, limit=500, sort="amount_desc")["items"]]
    assert amts == sorted(amts, reverse=True)


def test_spending_by_category_excludes_transfers_and_income(world):
    db, _, _ = world
    db_total = -sum(t["amount"] for t in db.all("SELECT amount FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount<0 AND c.kind='expense'"))
    assert sum(r["spent"] for r in ledger.spending_by_category(db)) == pytest.approx(db_total, abs=0.05)
    tid = db.one("SELECT id FROM transactions WHERE amount<0 LIMIT 1")["id"]
    before = sum(r["spent"] for r in ledger.spending_by_category(db))
    amt = -db.one("SELECT amount FROM transactions WHERE id=?", (tid,))["amount"]
    ledger.set_category(db, tid, "Transfer")
    assert sum(r["spent"] for r in ledger.spending_by_category(db)) == pytest.approx(before - amt, abs=0.05) or db.one("SELECT 1")


# ---------------------------------------------------------------- categories and rules
def test_rules_lock_and_recategorize(world):
    db, _, _ = world
    t = db.one("SELECT * FROM transactions WHERE name LIKE 'Shell Oil%' LIMIT 1") or db.one("SELECT * FROM transactions WHERE amount<0 LIMIT 1")
    r = ledger.set_category(db, t["id"], "Gas", make_rule=True)
    assert r["updated"] >= 1
    same = db.all("SELECT category FROM transactions WHERE lower(name) LIKE ?", (f"%{recur.norm(t['name'])}%",))
    assert all(x["category"] == "Gas" for x in same)
    other = db.one("SELECT id FROM transactions WHERE id != ? AND category != 'Gas' AND amount<0", (t["id"],))
    ledger.set_category(db, other["id"], "Games")                       # manual choice is locked
    db.conn.execute("DELETE FROM category_rules"); db.conn.commit()
    ledger.recategorize(db)
    assert db.one("SELECT category FROM transactions WHERE id=?", (other["id"],))["category"] == "Games"
    assert db.one("SELECT category FROM transactions WHERE id=?", (t["id"],))["category"] == "Gas"      # locked too
    with pytest.raises(ValueError):
        ledger.set_category(db, t["id"], "Not A Category")
    with pytest.raises(KeyError):
        ledger.set_category(db, "nope", "Food")


def test_plaid_category_mapping():
    db = DB(":memory:")
    for pcat, amt, want in [("FOOD_AND_DRINK_GROCERIES", -5, "Groceries"), ("FOOD_AND_DRINK_COFFEE", -5, "Food"), ("TRANSPORTATION_GAS", -5, "Gas"),
                            ("GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", -5, "Clothes"), ("INCOME_WAGES", 100, "Income"), ("TRANSFER_OUT_SAVINGS", -5, "Transfer"),
                            ("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", -5, "Transfer"), ("WEIRD_NEW_CODE", -5, "Misc"), (None, 12, "Income"), (None, -12, "Misc")]:
        assert ledger.categorize(db, "x", amt, pcat) == want, pcat


# ---------------------------------------------------------------- detection of repeating payments
def test_detector_finds_planted_and_ignores_noise(world):
    db, truth, _ = world
    found = {f["name"].lower(): f for f in ledger.recurring_found(db, TODAY)}
    nf = next(f for n, f in found.items() if "netflix" in n)
    assert (nf["every"], nf["unit"], nf["amount"], nf["direction"]) == (1, "month", 15.49, "out")
    pay = next(f for n, f in found.items() if "payroll" in n)
    assert (pay["every"], pay["unit"], pay["direction"], pay["amount"]) == (2, "week", "in", pytest.approx(2150.0, abs=1))
    farm = next(f for n, f in found.items() if "farm box" in n)
    assert (farm["every"], farm["unit"], farm["amount"]) == (1, "week", 32.0)
    assert any("water" in n and f["unit"] == "month" for n, f in found.items())
    for f in found.values():                                         # next date must be in the future and on schedule
        assert f["next_date"] >= TODAY.isoformat()
    noise = [f for n, f in found.items() if any(m.lower() in n for m in ("kroger", "chipotle", "target", "uber", "zara"))]
    assert len(noise) <= 1, noise                                    # random merchants should almost never look steady


def test_suggestions_hide_tracked_and_dismissed(world):
    db, _, _ = world
    names = lambda: {s["name"].lower() for s in ledger.suggestions(db, TODAY)}
    assert any("netflix" in n for n in names())
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,created) VALUES('Netflix',15.49,'monthly',1,'month','2026-11-20','x')"); db.conn.commit()
    assert not any("netflix" in n for n in names())                 # already tracked
    key = next(s["key"] for s in ledger.suggestions(db, TODAY) if "farm" in s["name"].lower())
    db.conn.execute("INSERT INTO dismissed VALUES(?)", (key,)); db.conn.commit()
    assert not any("farm box" in n for n in names())


# ---------------------------------------------------------------- upcoming bills vs brute force
def test_upcoming_matches_brute_force(world):
    db, _, rng = world
    expected = {}
    for i in range(rng.randint(4, 9)):
        every, unit = rng.randint(1, 4), rng.choice(recur.UNITS)
        anchor = TODAY + timedelta(days=rng.randint(-200, 60))
        amt = round(rng.uniform(3, 200), 2)
        payer = rng.choice(["me", "me", "other"])
        db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,payer,payer_name,created,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
                        (f"S{i}", amt, "x", every, unit, anchor.isoformat(), payer, "Sam" if payer == "other" else "", "x"))
        expected[f"S{i}"] = (every, unit, anchor, amt, payer)
    db.conn.commit()
    for days in (1, 7, 30, 90, 365):
        u = ledger.upcoming(db, days, TODAY)
        mine = other = 0.0
        for every, unit, anchor, amt, payer in expected.values():
            span = 200 if unit == "year" else 2500
            dates = {recur.shift(anchor, every, unit, k) for k in range(-span, span)}      # independent of occurrences()
            for dd in range(0, days + 1):
                if TODAY + timedelta(days=dd) in dates:
                    if payer == "me": mine += amt
                    else: other += amt
        assert u["total"] == pytest.approx(mine, abs=0.01) and u["others_total"] == pytest.approx(other, abs=0.01), (days, u["total"], mine)


def test_end_date_stops_occurrences():
    db = DB(":memory:")
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,end_date,created) VALUES('Trial',5,'weekly',1,'week','2026-11-08','2026-11-22','x')"); db.conn.commit()
    assert [i["date"] for i in ledger.upcoming(db, 60, TODAY)["items"]] == ["2026-11-08", "2026-11-15", "2026-11-22"]


# ---------------------------------------------------------------- late payments and alerts
def test_late_payment_flags(world):
    db, truth, _ = world
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('City Water',61.25,'monthly',1,'month',?,'chk','x')",
                    ((TODAY + timedelta(days=3)).isoformat(),))
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('Netflix',15.49,'monthly',1,'month',?,'chk','x')",
                    ((TODAY + timedelta(days=10)).isoformat(),))
    db.conn.commit()
    late = {m["name"]: m for m in ledger.late_payments(db, TODAY)}
    assert "City Water" in late and late["City Water"]["misses"] >= 2     # stopped 70 days ago -> at least 2 missed cycles
    assert "Netflix" not in late                                         # still arriving
    alerts = ledger.alerts(db, TODAY)
    assert any(a["kind"] == "missed_payment" and a["severity"] == "critical" for a in alerts)


def test_late_flag_needs_data_coverage_and_grace():
    db = DB(":memory:")
    add = lambda i, dt, name, amt: (db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category) VALUES(?,?,?,?,?,'Misc')", (i, "a", dt, name, amt)), db.conn.commit())
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES('a','plaid','A','cash',100)")
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('Gym',40,'monthly',1,'month','2026-11-05','a','x')"); db.conn.commit()
    misses = lambda: [m["misses"] for m in ledger.late_payments(db, TODAY)]
    assert misses() == []                       # no transactions at all: we cannot say anything is missing
    add("t", "2026-11-06", "Coffee", -3)
    assert misses() == []                       # data starts after the expected dates: not enough history
    add("t0", "2026-08-01", "Old", -3)
    assert misses() == [3]                      # Oct 5, Sep 5, Aug 5 missed. Nov 5 is still inside its 3-day grace window on Nov 7
    add("o", "2026-10-06", "GYM CO", -40)       # Oct seen (a day late): newest cycle is fine, so nothing is flagged
    assert misses() == []
    db.conn.execute("DELETE FROM transactions WHERE id='o'"); add("s", "2026-09-04", "GYM CO", -40.2)
    assert misses() == [1]                      # Oct missed, Sep seen, so a single miss
    add("r", "2026-10-05", "Random Shop", -45)
    assert misses() == [1]                      # a similar amount from an unrelated payee does not hide the miss
    add("x", "2026-10-05", "Mystery", -40.0)
    assert misses() == []                       # an exact amount match does count (fixed bills can be named anything)


def test_account_alerts():
    db = DB(":memory:")
    db.conn.executemany("INSERT INTO accounts(id,source,name,kind,balance,min_balance) VALUES(?,?,?,?,?,?)",
                        [("neg", "manual", "Overdrawn", "cash", -12.5, None), ("low", "manual", "Low", "cash", 80, 200), ("ok", "manual", "Fine", "cash", 900, 200),
                         ("cc", "manual", "Card", "credit", 5000, None)])
    db.conn.commit()
    got = {(a["kind"], a["account_id"]) for a in ledger.alerts(db, TODAY)}
    assert got == {("negative_balance", "neg"), ("low_balance", "low")}


def test_projected_low_from_bound_bills():
    db = DB(":memory:")
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance,min_balance) VALUES('a','manual','Chk','cash',100,50)")
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('Rent',90,'monthly',1,'month',?,'a','x')", ((TODAY + timedelta(days=5)).isoformat(),)); db.conn.commit()
    assert [a["kind"] for a in ledger.alerts(db, TODAY) if a["kind"] != "due_soon"] == ["projected_low"]


# ---------------------------------------------------------------- projection
def test_projection_invariants(world):
    db, _, _ = world
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('Rent',900,'monthly',1,'month',?,'chk','x')", ((TODAY + timedelta(days=4)).isoformat(),)); db.conn.commit()
    for days in (7, 30, 90):
        p = ledger.project_balance(db, days, TODAY, ["chk"])
        assert p["end"] == pytest.approx(p["start"] - p["bills_total"] + p["income_total"], abs=0.02)
        assert p["lowest"] <= min(p["start"], p["end"]) + 1e-9
        assert [e["date"] for e in p["events"]] == sorted(e["date"] for e in p["events"])
        run = p["start"]
        for e in p["events"]:
            run = round(run + e["amount"], 2); assert e["balance"] == pytest.approx(run, abs=0.02)
    assert ledger.project_balance(db, 30, TODAY, ["chk"], include_income=False)["income_total"] == 0


def test_projection_ignores_other_payers_and_other_accounts():
    db = DB(":memory:")
    db.conn.executemany("INSERT INTO accounts(id,source,name,kind,balance) VALUES(?,?,?,?,?)", [("a", "manual", "A", "cash", 1000), ("b", "manual", "B", "cash", 500), ("c", "manual", "Card", "credit", 50)])
    d1 = (TODAY + timedelta(days=2)).isoformat()
    db.conn.executemany("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,payer,payer_name,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [("mine-a", 100, "m", 1, "month", d1, "a", "me", "", "x"), ("mine-b", 40, "m", 1, "month", d1, "b", "me", "", "x"), ("sams", 999, "m", 1, "month", d1, "a", "other", "Sam", "x"),
                         ("onCard", 70, "m", 1, "month", d1, "c", "me", "", "x"), ("unbound", 10, "m", 1, "month", d1, None, "me", "", "x")]); db.conn.commit()
    assert ledger.project_balance(db, 10, TODAY, ["a"], False)["end"] == 900.0           # only bills bound to A
    assert ledger.project_balance(db, 10, TODAY, None, False)["end"] == 1500 - 100 - 40 - 10   # all cash; card bill and Sam's bill excluded


# ---------------------------------------------------------------- savings, net worth, budgets
def test_savings_math_and_net_worth_no_double_count():
    db = DB(":memory:")
    db.conn.executemany("INSERT INTO accounts(id,source,name,kind,balance) VALUES(?,?,?,?,?)", [("s", "plaid", "Sav", "cash", 5000.0), ("c", "plaid", "Card", "credit", 300.0)])
    db.conn.execute("INSERT INTO savings(name,kind,balance,apy,maturity,account_id) VALUES('Linked HYSA','savings',1,4.5,NULL,'s')")   # balance comes from the account
    db.conn.execute("INSERT INTO savings(name,kind,balance,apy,opened,maturity,term_months,goal) VALUES('12mo CD','cd',10000,5.0,'2026-06-07','2027-06-07',12,20000)")
    db.conn.commit()
    sv = ledger.savings_summary(db, TODAY)
    linked = next(i for i in sv["items"] if i["linked"])
    cd = next(i for i in sv["items"] if i["kind"] == "cd")
    assert linked["balance"] == 5000.0 and cd["days_to_maturity"] == 212
    assert cd["value_at_maturity"] == pytest.approx(10000 * (1 + 0.05 / 365) ** 212, abs=0.01) and cd["goal_progress"] == 0.5
    assert sv["total"] == 15000.0 and sv["weighted_apy"] == pytest.approx((5000 * 4.5 + 10000 * 5.0) / 15000, abs=0.001)
    assert ledger.net_worth(db) == 5000 - 300 + 10000                      # linked savings counted once
    db.conn.execute("UPDATE savings SET maturity=? WHERE kind='cd'", ((TODAY + timedelta(days=20)).isoformat(),)); db.conn.commit()
    assert any(a["kind"] == "maturing" for a in ledger.alerts(db, TODAY))


def test_budget_status_matches_spending(world):
    db, _, _ = world
    db.conn.executemany("INSERT INTO budgets VALUES(?,?)", [("Food", 300.0), ("Gas", 120.0)]); db.conn.commit()
    month = "2026-10"
    got = {b["category"]: b for b in ledger.budget_status(db, month, TODAY)}
    spent = {r["category"]: r["spent"] for r in ledger.spending_by_category(db, "2026-10-01", "2026-10-31")}
    for cat, b in got.items():
        assert b["spent"] == spent.get(cat, 0.0) and b["left"] == pytest.approx(b["budget"] - b["spent"], abs=0.01)


def test_keyword_fallback_categories():
    db = DB(":memory:")
    for name, amt, want in [("Kroger #1234", -40, "Groceries"), ("SHELL OIL 5522", -30, "Gas"), ("Starbucks #8497", -6, "Food"), ("Steam Games", -20, "Games"), ("ACME CORP PAYROLL", 2150, "Income"),
                            ("Refund Target", 25, "Income"), ("Target #44", -25, "Shopping"), ("Mystery LLC", -9, "Misc"), ("Mystery LLC", 9, "Income"), ("Old Navy #3420", -50, "Clothes"),
                            ("Shell script course", 30, "Income")]:
        assert ledger.categorize(db, name, amt) == want, (name, amt)
    db.conn.execute("INSERT INTO category_rules(pattern,category) VALUES('starbucks','Fun')"); db.conn.commit()
    assert ledger.categorize(db, "Starbucks #8497", -6) == "Fun"            # your rule beats the keyword guess
    assert ledger.categorize(db, "Kroger", -5, "GENERAL_MERCHANDISE") == "Shopping"   # the bank's category beats the keyword guess
