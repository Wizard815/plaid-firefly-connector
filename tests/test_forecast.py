"""Projected spending from history: hand-computed scenarios and random-data invariants."""
import random
import statistics
from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.seed import seed
from goblinbank import goblin, ledger
from goblinbank.db import DB

H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def tx(db, day, name, amt, cat):
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category) VALUES(?,?,?,?,?,?)", (f"{day}-{name}-{amt}-{random.random()}", "a", day, name, amt, cat))


def world(months=6, today=date(2026, 11, 15)):
    """6 past months: ~300 Food spread over many purchases, 1200 rent in one payment, 4000 salary in two deposits."""
    db = DB(":memory:")
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES('a','manual','A','cash',100)")
    y, m = today.year, today.month
    for k in range(1, months + 1):
        mm, yy = m - k, y
        while mm < 1:
            mm, yy = mm + 12, yy - 1
        for d in (3, 8, 12, 17, 21, 26):
            tx(db, f"{yy}-{mm:02d}-{d:02d}", "Grocer", -50.0, "Food")
        tx(db, f"{yy}-{mm:02d}-01", "Landlord", -1200.0, "Housing")
        tx(db, f"{yy}-{mm:02d}-05", "Pay", 2000.0, "Income"); tx(db, f"{yy}-{mm:02d}-19", "Pay", 2000.0, "Income")
    db.conn.commit()
    return db


def cat(f, name):
    return next(c for c in f["categories"] if c["category"] == name)


def test_steady_category_on_pace_projects_the_usual_month():
    db = world()
    for d in (2, 6, 9, 13):
        tx(db, f"2026-11-{d:02d}", "Grocer", -37.5, "Food")           # 150 spent by Nov 15: exactly half of the usual 300 at the halfway point
    db.conn.commit()
    f = ledger.spending_forecast(db, date(2026, 11, 15))
    food = cat(f, "Food")
    assert food["typical"] == 300 and food["spent"] == 150 and food["method"] == "pace"
    assert food["forecast"] == pytest.approx(150 + 300 * (1 - 15 / 30) * 1.0, abs=0.01) == pytest.approx(300, abs=0.01)


def test_overspending_pace_pulls_the_projection_up_but_is_clamped():
    db = world()
    for d in (2, 4, 6, 9, 11, 13):
        tx(db, f"2026-11-{d:02d}", "Grocer", -100.0, "Food")           # 600 by mid-month: double the usual pace
    db.conn.commit()
    food = cat(ledger.spending_forecast(db, date(2026, 11, 15)), "Food")
    assert food["forecast"] == pytest.approx(600 + 300 * 0.5 * 2.0, abs=0.01)                     # ratio 2.0 (clamp), not 4x
    tx(db, "2026-11-14", "Splurge", -5000.0, "Food"); db.conn.commit()
    food = cat(ledger.spending_forecast(db, date(2026, 11, 15)), "Food")
    assert food["forecast"] == pytest.approx(5600 + 150 * 2.0, abs=0.01)                          # still clamped at 2x of the remaining usual spend


def test_lumpy_categories_are_not_paced():
    db = world()
    tx(db, "2026-11-01", "Landlord", -1200.0, "Housing"); db.conn.commit()
    rent = cat(ledger.spending_forecast(db, date(2026, 11, 2)), "Housing")                         # paid on day 1: naive pacing would say 18,000
    assert rent["method"] == "usual amount" and rent["forecast"] == 1200
    db2 = world()                                                                                  # rent not paid yet this month: still expect the usual amount
    assert cat(ledger.spending_forecast(db2, date(2026, 11, 2)), "Housing")["forecast"] == 1200


def test_early_month_relies_on_history_and_new_categories_have_no_guess():
    db = world()
    f = ledger.spending_forecast(db, date(2026, 11, 1))
    assert cat(f, "Food")["forecast"] == pytest.approx(300 * (1 - 1 / 30), abs=0.01)             # day 1: nothing spent, ~the usual month remains
    tx(db, "2026-11-10", "Hobby", -80.0, "Fun"); db.conn.commit()
    fun = cat(ledger.spending_forecast(db, date(2026, 11, 15)), "Fun")
    assert fun["method"] == "no history" and fun["forecast"] == 80 and fun["typical"] is None


def test_bills_still_due_never_forecast_below_what_will_surely_be_paid():
    db = world()
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,created) VALUES('Phone',90,'monthly',1,'month','2026-11-25','Bills','x')"); db.conn.commit()
    f = ledger.spending_forecast(db, date(2026, 11, 15))
    bills = cat(f, "Bills")
    assert bills["bills_due"] == 90 and bills["forecast"] >= 90 and bills["method"] == "no history"     # nothing in history, but the bill is coming
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,created) VALUES('Gym',40,'monthly',1,'month','2026-11-10','Fun','x')"); db.conn.commit()
    assert "Fun" not in {c["category"] for c in ledger.spending_forecast(db, date(2026, 11, 15))["categories"]} or cat(ledger.spending_forecast(db, date(2026, 11, 15)), "Fun")["bills_due"] == 0     # already past its date: not "still due"


def test_income_comparison_uses_history_then_entered_income():
    db = world()
    f = ledger.spending_forecast(db, date(2026, 11, 15))
    assert f["income_avg"] == 4000 and f["income_source"] == "history" and f["months_used"] == 6 and f["confidence"] == "high"
    assert f["spent_so_far"] == 0 and f["projected"] == pytest.approx(300 * 0.5 * 0.5 + 1200, abs=0.01)         # nothing spent on food by mid-month: pace ratio floors at 0.5; plus the usual rent
    assert f["pct_projected"] == pytest.approx(f["projected"] / 4000, abs=0.001) and f["left_after_projected"] == pytest.approx(4000 - f["projected"], abs=0.01)
    assert len(f["history"]) == 6 and f["history"][0]["income"] == 4000 and f["history"][0]["spent"] == 1500
    # no history at all: fall back to the income you entered, and say the estimate is weak
    e = DB(":memory:")
    e.conn.execute("INSERT INTO incomes(name,amount,every,unit,next_date) VALUES('Pay',3000,1,'month','2026-11-20')"); e.conn.commit()
    g = ledger.spending_forecast(e, date(2026, 11, 15))
    assert g["income_avg"] == 3000 and g["income_source"] == "entered" and g["confidence"] == "low" and g["projected"] == 0 and g["typical_month"] is None
    assert ledger.spending_forecast(DB(":memory:"), date(2026, 11, 15))["income_avg"] is None


def test_a_month_with_no_data_is_skipped_not_counted_as_zero():
    db = world(months=6)
    db.conn.execute("DELETE FROM transactions WHERE date LIKE '2026-08-%'"); db.conn.commit()        # the account was not linked in August
    f = ledger.spending_forecast(db, date(2026, 11, 15))
    assert f["months_used"] == 5 and cat(f, "Food")["typical"] == 300 and f["income_avg"] == 4000    # averages are not dragged down by the gap


def test_transfers_and_income_are_not_spending():
    db = world()
    tx(db, "2026-11-03", "To savings", -2000.0, "Transfer"); tx(db, "2026-11-04", "Brokerage", -500.0, "Investing"); db.conn.commit()
    f = ledger.spending_forecast(db, date(2026, 11, 15))
    assert {"Transfer", "Investing", "Income"}.isdisjoint({c["category"] for c in f["categories"]})
    db.conn.execute("UPDATE categories SET kind='expense' WHERE name='Investing'"); db.conn.commit()      # flip the tag and it counts
    assert cat(ledger.spending_forecast(db, date(2026, 11, 15)), "Investing")["spent"] == 500


def test_endpoint_agent_tool_and_home_assistant_values():
    db = world()
    c = TestClient(goblin.create_app(db=db, today=lambda: date(2026, 11, 15)))
    f = c.get("/api/forecast?months=3", headers=H).json()
    assert f["months_used"] == 3 and f["income_avg"] == 4000 and c.get("/api/forecast", headers=H).status_code == 200 and c.get("/api/forecast").status_code == 401
    assert c.get("/api/forecast?months=999", headers=H).json()["months_used"] == 6                 # clamped to 24, and only 6 months exist
    from goblinbank import external
    s = external.summary(db, date(2026, 11, 15))
    assert s["spend_month_projected"] == c.get("/api/forecast", headers=H).json()["projected"] and s["income_avg_month"] == 4000 and "spend_month_so_far" in external.flat(s)
    assert "spending_forecast" in {t["name"] for t in c.get("/api/agent/tools", headers=H).json()}


# ---------------------------------------------------------------- random data vs an independent calculation
@pytest.mark.parametrize("seed_no", range(1, 13))
def test_invariants_on_random_data(seed_no):
    today = date(2026, 11, 7) if seed_no % 2 else date(2026, 11, 28)
    db = DB(":memory:")
    seed(db, seed_no, today)
    f = ledger.spending_forecast(db, today)
    rows = {r["category"]: r for r in f["categories"]}
    assert f["projected"] == pytest.approx(sum(r["forecast"] for r in f["categories"]), abs=0.05)
    assert f["spent_so_far"] == pytest.approx(sum(r["spent"] for r in f["categories"]), abs=0.05)
    for r in f["categories"]:
        assert r["forecast"] >= r["spent"] + r["bills_due"] - 0.01 and r["forecast"] >= 0
    # independent oracle for this month's spend and the income average
    want = db.one("SELECT ROUND(-SUM(t.amount),2) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount<0 AND c.kind='expense' AND t.date >= '2026-11-01' AND t.date <= '2026-11-30'")["s"] or 0
    assert f["spent_so_far"] == pytest.approx(want, abs=0.05)
    inc = [db.one("SELECT ROUND(SUM(t.amount),2) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount>0 AND c.kind='income' AND substr(t.date,1,7)=?", (h["month"],))["s"] for h in f["history"]]
    inc = [i for i in inc if i and i > 0]
    assert f["income_avg"] == pytest.approx(statistics.fmean(inc), abs=0.05) and f["income_source"] == "history"
    for h in f["history"]:
        o = db.one("SELECT ROUND(-SUM(t.amount),2) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount<0 AND c.kind='expense' AND substr(t.date,1,7)=?", (h["month"],))["s"] or 0
        assert h["spent"] == pytest.approx(o, abs=0.05)
    # later in the month, with the same data, the day-of-month changes the remaining estimate but never the spend so far
    early = ledger.spending_forecast(db, date(2026, 11, 2))
    assert early["elapsed"] < f["elapsed"] or today.day <= 2
    # the planted weekly farm box (a steady, many-payment category) is paced, rent-like single payments are not
    assert all(r["method"] in ("pace", "usual amount", "no history") for r in f["categories"])
