"""Savings places, goals + allocations, income and the monthly plan: invariants on random data and the scenarios you described."""
import random
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.seed import seed
from goblinbank import goblin, plan
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def api(seeded=False, seed_no=1):
    db = DB(":memory:")
    if seeded:
        seed(db, seed_no, TODAY)
    return TestClient(goblin.create_app(db=db, today=lambda: TODAY)), db


def add_place(db, name, balance, kind="savings", **kw):
    cur = db.conn.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES(?,?,?,?,?)", (name, kind, balance, kw.get("apy", 4.0), kw.get("maturity")))
    db.conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- your scenario, step by step
def test_ten_thousand_nine_for_the_house_one_for_the_car_then_nothing_free():
    c, db = api()
    add_place(db, "Axos HYSA", 10000)
    house = c.post("/api/goals", headers=H, json={"name": "House", "target": 9000}).json()
    assert house["allocated"] == 9000 and house["funded"] and house["shortfall"] == 0          # free money is earmarked on creation
    car = c.post("/api/goals", headers=H, json={"name": "Car", "target": 1000}).json()
    assert car["allocated"] == 1000 and c.get("/api/goals", headers=H).json()["free"] == 0
    laptop = c.post("/api/goals", headers=H, json={"name": "Laptop", "target": 1500, "monthly": 250}).json()
    assert laptop["allocated"] == 0 and laptop["shortfall"] == 1500 and not laptop["funded"]   # nothing free: it just becomes a savings goal
    assert laptop["ready_date"] == (TODAY + timedelta(days=round(6 * 30.4375))).isoformat()      # 1500 / 250 = 6 months
    v = c.get("/api/goals", headers=H).json()
    assert (v["total"], v["allocated"], v["free"], v["needed"]) == (10000, 10000, 0, 1500)
    # you can't earmark money you don't have
    r = c.post(f"/api/goals/{laptop['id']}/fund", headers=H, json={"amount": 100})
    assert r.status_code == 409 and "only $0.00" in r.json()["detail"]
    # the car goal changes its mind: release it, and it becomes free for the laptop
    c.post(f"/api/goals/{car['id']}/release", headers=H, json={})
    assert c.get("/api/goals", headers=H).json()["free"] == 1000
    assert c.post(f"/api/goals/{laptop['id']}/fund", headers=H, json={"amount": 1000}).json()["shortfall"] == 500
    # move earmarked money between goals
    v = c.post("/api/goals/move", headers=H, json={"from_id": house["id"], "to_id": car["id"], "amount": 1000}).json()
    assert {g["name"]: g["allocated"] for g in v["items"]} == {"House": 8000, "Car": 1000, "Laptop": 1000} and v["free"] == 0
    assert c.post("/api/goals/move", headers=H, json={"from_id": car["id"], "to_id": house["id"], "amount": 5000}).status_code == 409


def test_afford_check_uses_free_money_only():
    c, db = api()
    add_place(db, "Savings", 10000)
    c.post("/api/goals", headers=H, json={"name": "House", "target": 9000})
    a = c.get("/api/afford?amount=800", headers=H).json()
    assert a["can_buy_now"] and a["free_savings"] == 1000 and "leave $200.00 free" in a["note"]
    b = c.get("/api/afford?amount=1500", headers=H).json()
    assert not b["can_buy_now"] and b["shortfall"] == 500 and "months_to_save" not in b and "no saving pace" in b["note"]      # empty plan -> honest answer, no invented date
    db.conn.execute("INSERT INTO incomes(name,amount,every,unit,next_date) VALUES('Pay',3000,1,'month','2026-11-15')"); db.conn.commit()
    b = c.get("/api/afford?amount=1500", headers=H).json()
    assert b["monthly_left_in_plan"] == 3000 and b["months_to_save"] == 1 and b["ready_date"] == (TODAY + timedelta(days=round(30.4375))).isoformat()
    assert c.get("/api/afford?amount=0", headers=H).status_code == 422


def test_pinning_money_to_one_account_and_autofill_by_priority():
    c, db = api()
    cd = add_place(db, "CD", 6000, kind="cd"); sv = add_place(db, "HYSA", 4000)
    h = c.post("/api/goals", headers=H, json={"name": "House", "target": 7000, "priority": 1, "auto_allocate": False}).json()
    k = c.post("/api/goals", headers=H, json={"name": "Car", "target": 5000, "priority": 2, "auto_allocate": False}).json()
    assert c.post(f"/api/goals/{h['id']}/allocate", headers=H, json={"place": f"s:{cd}", "amount": 6500}).status_code == 409      # the CD only has 6000
    assert c.post(f"/api/goals/{h['id']}/allocate", headers=H, json={"place": f"s:{cd}", "amount": 6000}).status_code == 200
    v = c.get("/api/goals", headers=H).json()
    assert {p["name"]: (p["earmarked"], p["free"]) for p in v["places"]} == {"CD": (6000, 0), "HYSA": (0, 4000)} and v["free"] == 4000
    assert c.post(f"/api/goals/{k['id']}/allocate", headers=H, json={"place": "s:999", "amount": 1}).status_code == 422           # unknown place
    v = c.post("/api/goals/autofill", headers=H).json()
    got = {g["name"]: g["allocated"] for g in v["items"]}
    assert got == {"House": 7000, "Car": 3000} and v["free"] == 0            # house (priority 1) topped up first, then the car with what's left


def test_closing_and_deleting_goals_free_their_money():
    c, db = api()
    add_place(db, "Savings", 3000)
    g = c.post("/api/goals", headers=H, json={"name": "TV", "target": 2000}).json()
    assert c.get("/api/goals", headers=H).json()["free"] == 1000
    done = c.post(f"/api/goals/{g['id']}/close", headers=H, json={"status": "done"}).json()
    assert done["status"] == "done" and c.get("/api/goals", headers=H).json()["free"] == 3000 and done["closed"] == TODAY.isoformat()
    h = c.post("/api/goals", headers=H, json={"name": "Trip", "target": 500}).json()
    c.delete(f"/api/goals/{h['id']}", headers=H)
    assert c.get("/api/goals", headers=H).json()["free"] == 3000 and db.one("SELECT COUNT(*) n FROM allocations")["n"] == 0
    assert c.post(f"/api/goals/{g['id']}/fund", headers=H, json={"amount": 5}).status_code == 409        # closed goals take no money
    assert c.post(f"/api/goals/{g['id']}/close", headers=H, json={"status": "weird"}).status_code == 422


def test_goal_validation_and_deadline_math():
    c, db = api()
    add_place(db, "Savings", 100)
    for bad in [dict(name=" ", target=5), dict(name="x", target=0), dict(name="x", target=5, priority=9), dict(name="x", target=5, deadline="2020-01-01"), dict(name="x", target=5, deadline="soon"), dict(name="x", target=5, monthly=-1)]:
        assert c.post("/api/goals", headers=H, json=bad).status_code == 422, bad
    g = c.post("/api/goals", headers=H, json={"name": "Bike", "target": 1000, "deadline": "2027-05-07", "auto_allocate": False}).json()
    assert g["days_left"] == 181 and g["monthly_needed"] == pytest.approx(1000 / 6, abs=0.01)            # 181 days = 6 months
    assert c.put(f"/api/goals/{g['id']}", headers=H, json={"target": 600}).json()["monthly_needed"] == pytest.approx(100, abs=0.01)
    assert c.put("/api/goals/999", headers=H, json={"target": 1}).status_code == 404


def test_overallocation_when_a_balance_drops():
    c, db = api()
    sid = add_place(db, "Savings", 1000)
    c.post("/api/goals", headers=H, json={"name": "Move", "target": 1000})
    db.conn.execute("UPDATE savings SET balance=400 WHERE id=?", (sid,)); db.conn.commit()           # you spent from it
    v = c.get("/api/goals", headers=H).json()
    assert v["overallocated"] and v["free"] == -600
    assert any(a["kind"] == "overallocated" for a in c.get("/api/alerts", headers=H).json())


def test_free_savings_alert_points_at_the_top_unfunded_goal():
    c, db = api()
    add_place(db, "Savings", 800)
    c.post("/api/goals", headers=H, json={"name": "Later", "target": 5000, "priority": 3, "auto_allocate": False})
    c.post("/api/goals", headers=H, json={"name": "Now", "target": 400, "priority": 1, "auto_allocate": False})
    a = next(x for x in c.get("/api/alerts", headers=H).json() if x["kind"] == "free_savings")
    assert "800.00" in a["title"] and a["detail"].startswith("Now")


# ---------------------------------------------------------------- which accounts count as savings
def test_savings_accounts_are_listed_automatically_and_can_be_switched():
    c, db = api()
    db.conn.executemany("INSERT INTO accounts(id,source,institution,name,kind,balance,subtype) VALUES(?,?,?,?,?,?,?)",
                        [("a1", "plaid", "Axos", "High Yield", "cash", 5000.0, "savings"), ("a2", "plaid", "Huntington", "Checking", "cash", 900.0, "checking"), ("a3", "plaid", "Ally", "Ally CD", "cash", 2000.0, "cd"),
                         ("a4", "plaid", "Cap One", "Card", "credit", 300.0, "credit card")]); db.conn.commit()
    names = lambda: {p["name"] for p in c.get("/api/goals", headers=H).json()["places"]}
    assert names() == {"High Yield", "Ally CD"}                                                         # by the bank's own account type
    assert c.put("/api/accounts/a2", headers=H, json={"savings_mode": "yes"}).status_code == 200 and "Checking" in names()
    c.put("/api/accounts/a1", headers=H, json={"savings_mode": "no"}); assert "High Yield" not in names()
    c.put("/api/accounts/a1", headers=H, json={"savings_mode": "auto"}); assert "High Yield" in names()
    assert c.put("/api/accounts/a1", headers=H, json={"savings_mode": "maybe"}).status_code == 422
    # a hand-tracked item that follows the account replaces the account's own row (no double counting)
    db.conn.execute("INSERT INTO savings(name,kind,balance,account_id,apy) VALUES('My HYSA','savings',0,'a1',4.5)"); db.conn.commit()
    v = c.get("/api/goals", headers=H).json()
    assert sorted(p["name"] for p in v["places"]) == ["Ally CD", "Checking", "My HYSA"] and v["total"] == 5000 + 2000 + 900


# ---------------------------------------------------------------- the monthly plan: income vs bills vs budgets vs goals
def test_plan_bills_sit_inside_their_budget():
    c, db = api(seeded=False)
    db.conn.execute("INSERT INTO incomes(name,amount,every,unit,next_date) VALUES('Pay',2000,2,'week','2026-11-13')")      # 2000 every 2 weeks
    db.conn.executemany("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,created) VALUES(?,?,?,?,?,?,?,?)",
                        [("Netflix", 15, "monthly", 1, "month", "2026-11-20", "Subscriptions", "x"), ("Gym", 40, "monthly", 1, "month", "2026-11-21", "Health", "x"), ("Water", 60, "monthly", 1, "month", "2026-11-22", "Bills", "x")])
    db.conn.executemany("INSERT INTO budgets VALUES(?,?)", [("Food", 400.0), ("Health", 30.0)])      # Health budget is BELOW its bill
    db.conn.execute("INSERT INTO goals(name,target,monthly,created) VALUES('House',9000,300,'x')"); db.conn.commit()
    p = c.get("/api/plan?month=2026-11", headers=H).json()
    by = {r["category"]: r for r in p["rows"]}
    assert p["income_month"] == round(2000 * 30.4375 / 14, 2) == 4348.21      # 2000 every 2 weeks, 365.25/14 pays a year
    assert by["Food"]["planned"] == 400 and by["Health"]["planned"] == 40 and by["Subscriptions"]["planned"] == 15 and by["Bills"]["planned"] == 60      # budget or bills, whichever is larger, never both
    assert p["outflow_planned"] == 400 + 40 + 15 + 60 and p["goals_month"] == 300
    assert p["left"] == round(4348.21 - 515 - 300, 2)
    assert any("Health budget" in w for w in p["warnings"])
    c.put("/api/budgets", headers=H, json={"category": "Food", "monthly_amount": 6000})
    p = c.get("/api/plan?month=2026-11", headers=H).json()
    assert p["left"] < 0 and any("spends" in w for w in p["warnings"])
    assert c.get("/api/plan?month=nov", headers=H).status_code == 422


def test_plan_actuals_and_income_detection(monkeypatch):
    c, db = api(seeded=True, seed_no=2)
    p = c.get("/api/plan?month=2026-10", headers=H).json()
    assert any("payroll" in s["name"].lower() for s in p["income_suggestions"])                              # steady deposits are offered as income
    s = next(x for x in p["income_suggestions"] if "payroll" in x["name"].lower())
    r = c.post("/api/incomes/accept", headers=H, json={"key": s["key"], "name": "My paycheck"})
    assert r.status_code == 200 and r.json()["label"] == "biweekly" and r.json()["monthly"] == pytest.approx(s["amount"] * 30.4375 / 14, abs=0.01)
    p = c.get("/api/plan?month=2026-10", headers=H).json()
    assert not p["income_suggestions"] and p["income_month"] == r.json()["monthly"]
    cats = {x["name"]: x["kind"] for x in c.get("/api/categories", headers=H).json()}
    want = db.one("SELECT ROUND(COALESCE(SUM(amount),0),2) s FROM transactions WHERE amount>0 AND category IN (SELECT name FROM categories WHERE kind='income') AND date BETWEEN '2026-10-01' AND '2026-10-31'")["s"]
    assert p["actual"]["income"] == want
    spent = db.one("SELECT ROUND(-SUM(t.amount),2) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount<0 AND c.kind='expense' AND t.date BETWEEN '2026-10-01' AND '2026-10-31'")["s"]
    assert p["actual"]["spent"] == spent


def test_income_crud_validation():
    c, _ = api()
    ok = {"name": "Pay", "amount": 100, "cycle": "weekly", "next_date": "2026-11-10"}
    i = c.post("/api/incomes", headers=H, json=ok).json()
    assert i["monthly"] == pytest.approx(100 * 30.4375 / 7, abs=0.01)
    for bad in [{**ok, "amount": 0}, {**ok, "name": " "}, {**ok, "cycle": "sometimes"}, {**ok, "next_date": "x"}, {**ok, "account_id": "ghost"}]:
        assert c.post("/api/incomes", headers=H, json=bad).status_code in (404, 422), bad
    assert c.put(f"/api/incomes/{i['id']}", headers=H, json={**ok, "active": False}).json()["monthly"] == 0
    assert c.delete(f"/api/incomes/{i['id']}", headers=H).status_code == 200 and c.put("/api/incomes/99", headers=H, json=ok).status_code == 404


# ---------------------------------------------------------------- random invariants
@pytest.mark.parametrize("seed_no", range(15))
def test_allocation_invariants_hold_under_random_operations(seed_no):
    rng = random.Random(seed_no)
    c, db = api()
    keys = [f"s:{add_place(db, f'P{i}', round(rng.uniform(200, 8000), 2))}" for i in range(rng.randint(1, 4))]
    gids = [c.post("/api/goals", headers=H, json={"name": f"G{i}", "target": round(rng.uniform(100, 9000), 2), "auto_allocate": rng.random() < .5, "priority": rng.randint(1, 3)}).json()["id"] for i in range(rng.randint(2, 6))]
    for _ in range(60):
        g, op = rng.choice(gids), rng.choice(["fund", "fund_pin", "release", "release_part", "allocate", "move", "autofill", "close", "reopen"])
        amt = round(rng.uniform(1, 3000), 2)
        if op == "fund": c.post(f"/api/goals/{g}/fund", headers=H, json={"amount": amt})
        elif op == "fund_pin": c.post(f"/api/goals/{g}/fund", headers=H, json={"amount": amt, "place": rng.choice(keys)})
        elif op == "release": c.post(f"/api/goals/{g}/release", headers=H, json={})
        elif op == "release_part": c.post(f"/api/goals/{g}/release", headers=H, json={"amount": amt, "place": rng.choice(["", *keys])})
        elif op == "allocate": c.post(f"/api/goals/{g}/allocate", headers=H, json={"place": rng.choice(["", *keys]), "amount": amt})
        elif op == "move": c.post("/api/goals/move", headers=H, json={"from_id": g, "to_id": rng.choice(gids), "amount": amt})
        elif op == "autofill": c.post("/api/goals/autofill", headers=H)
        elif op == "close": c.post(f"/api/goals/{g}/close", headers=H, json={"status": "done"})
        else: c.post(f"/api/goals/{g}/close", headers=H, json={"status": "active"})
        v = c.get("/api/goals", headers=H).json()
        # the books never break, whatever the sequence of operations was
        assert v["free"] >= -0.01 and not v["overallocated"], (op, v["free"])
        assert v["allocated"] == pytest.approx(sum(x["amount"] for a in v["items"] if a["status"] == "active" for x in a["allocations"]), abs=0.02)
        assert v["free"] == pytest.approx(v["total"] - v["allocated"], abs=0.02)
        for p in v["places"]:
            assert p["earmarked"] <= p["balance"] + 0.01 and p["free"] >= -0.01
        for gg in v["items"]:
            assert gg["shortfall"] == pytest.approx(max(0, gg["target"] - gg["allocated"]), abs=0.02)
            if gg["status"] != "active": assert gg["allocated"] == 0
