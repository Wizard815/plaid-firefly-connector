"""HTTP-level tests for the new pages: accounts, filters, categories, subscriptions, savings, planner."""
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.seed import seed
from goblinbank import goblin
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def api():
    db = DB(":memory:")
    seed(db, 4, TODAY)
    return TestClient(goblin.create_app(db=db, today=lambda: TODAY)), db


def sub(**kw):
    return {"name": "Gym", "amount": 40, "next_date": "2026-11-20", "cycle": "monthly", **kw}


def test_every_route_needs_the_internal_secret(api):
    c, _ = api
    for path in ["/api/overview", "/api/accounts", "/api/transactions", "/api/subscriptions", "/api/savings", "/api/notes", "/api/budgets", "/api/categories",
                 "/api/alerts", "/api/upcoming", "/api/suggestions", "/api/projection", "/api/spending", "/api/agent/providers"]:
        assert c.get(path).status_code == 401, path


# ---------------------------------------------------------------- subscriptions
def test_subscription_intervals_payer_and_account_binding(api):
    c, _ = api
    r = c.post("/api/subscriptions", headers=H, json=sub(cycle=None, every=45, unit="day"))
    assert r.status_code == 200 and r.json()["label"] == "every 45 days" and r.json()["monthly"] == pytest.approx(40 * 30.4375 / 45, abs=0.01)
    q = c.post("/api/subscriptions", headers=H, json=sub(name="Insurance", cycle="quarterly", amount=300, account_id="chk")).json()
    assert q["label"] == "quarterly" and q["monthly"] == 100 and q["account_name"] == "Checking"
    other = c.post("/api/subscriptions", headers=H, json=sub(name="Dad's Hulu", payer="other", payer_name="Dad", payment_method="Dad's Visa", amount=18)).json()
    assert other["monthly"] == 0                                                  # someone else pays: not in your totals
    t = c.get("/api/subscriptions", headers=H).json()
    assert t["monthly_total"] == pytest.approx(r.json()["monthly"] + 100, abs=0.01)
    assert c.post("/api/subscriptions", headers=H, json=sub(payer="other")).status_code == 422          # needs a name
    assert c.post("/api/subscriptions", headers=H, json=sub(payer="nobody")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(account_id="ghost")).status_code == 404
    assert c.post("/api/subscriptions", headers=H, json=sub(category="Nope")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(cycle=None)).status_code == 422             # no interval at all
    assert c.post("/api/subscriptions", headers=H, json=sub(cycle=None, every=0, unit="day")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(next_date="soon")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(amount=-1)).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(name="  ")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=sub(notify_days=99)).status_code == 422
    sid = other["id"]
    assert c.put(f"/api/subscriptions/{sid}", headers=H, json=sub(name="Dad's Hulu", payer="me")).json()["payer_name"] == ""   # payer name cleared when you take over
    assert c.put("/api/subscriptions/9999", headers=H, json=sub()).status_code == 404
    assert c.delete(f"/api/subscriptions/{sid}", headers=H).status_code == 200


def test_upcoming_window_is_adjustable(api):
    c, _ = api
    c.post("/api/subscriptions", headers=H, json=sub(name="Weekly", amount=10, cycle="weekly", next_date="2026-11-08"))
    c.post("/api/subscriptions", headers=H, json=sub(name="Monthly", amount=50, next_date="2026-11-20"))
    u7, u30, u365 = (c.get(f"/api/upcoming?days={d}", headers=H).json() for d in (7, 30, 365))
    assert u7["total"] == 10 and u30["total"] == 10 * 5 + 50 and u365["count"] > u30["count"]
    assert u30["by_category"] == {"Subscriptions": 100.0}
    assert c.get("/api/overview?days=7", headers=H).json()["upcoming"]["total"] == 10
    assert c.get("/api/upcoming?days=0", headers=H).json()["days"] == 1


def test_suggestion_accept_with_name_interval_account(api):
    c, db = api
    sug = {s["name"].lower(): s for s in c.get("/api/suggestions", headers=H).json()}
    farm = next(s for n, s in sug.items() if "farm box" in n)
    r = c.post("/api/suggestions/accept", headers=H, json={"key": farm["key"], "name": "Farm box (CSA)", "account_id": "crd", "every": 1, "unit": "week"})
    assert r.status_code == 200 and r.json()["name"] == "Farm box (CSA)" and r.json()["account_id"] == "crd" and r.json()["amount"] == 32.0
    assert not any("farm box" in s["name"].lower() for s in c.get("/api/suggestions", headers=H).json())      # gone once tracked
    sug2 = next(s for s in c.get("/api/suggestions", headers=H).json() if "netflix" in s["name"].lower())
    c.post("/api/suggestions/accept", headers=H, json={"key": sug2["key"], "name": "Streaming"})              # renamed to something unrelated...
    assert not any("netflix" in s["name"].lower() for s in c.get("/api/suggestions", headers=H).json())      # ...and the detector must not offer it again
    assert c.post("/api/suggestions/accept", headers=H, json={"key": farm["key"], "name": "x"}).status_code == 404
    other = next(s for s in c.get("/api/suggestions", headers=H).json() if "water" in s["name"].lower())
    c.post("/api/suggestions/dismiss", headers=H, json={"key": other["key"]})
    assert not any("water" in s["name"].lower() for s in c.get("/api/suggestions", headers=H).json())
    db.conn.execute("INSERT INTO proposals(name,amount,every,unit,reason) VALUES('Gym',30,1,'month','agent saw it')"); db.conn.commit()
    p = next(s for s in c.get("/api/suggestions", headers=H).json() if s["source"] == "agent")
    assert c.post("/api/suggestions/accept", headers=H, json={"key": p["key"], "name": "Gym"}).status_code == 200
    assert db.one("SELECT status FROM proposals")["status"] == "accepted"


# ---------------------------------------------------------------- accounts
def test_manual_accounts_and_detail(api):
    c, db = api
    a = c.post("/api/accounts", headers=H, json={"name": "Wallet", "kind": "cash", "balance": 120.5, "min_balance": 20}).json()
    assert a["source"] == "manual" and a["id"].startswith("own-")
    assert c.post("/api/accounts", headers=H, json={"name": "x", "kind": "crypto"}).status_code == 422
    assert c.post("/api/accounts", headers=H, json={}).status_code == 422
    t = c.post("/api/transactions", headers=H, json={"account_id": a["id"], "date": "2026-11-05", "name": "Lunch", "amount": -12.0}).json()
    assert t["category"] == "Misc" and c.post("/api/transactions", headers=H, json={"account_id": "ghost", "date": "2026-11-05", "name": "x", "amount": 1}).status_code == 404
    assert c.post("/api/transactions", headers=H, json={"account_id": a["id"], "date": "bad", "name": "x", "amount": 1}).status_code == 422
    c.put(f"/api/accounts/{a['id']}", headers=H, json={"balance": 15.0})
    d = c.get(f"/api/accounts/{a['id']}", headers=H).json()
    assert d["account"]["balance"] == 15.0 and d["last_30_days"]["out"] == -12.0 and [h["balance"] for h in d["history"]] == [120.5, 15.0]
    assert [x["kind"] for x in d["alerts"]] == ["low_balance"]                                 # 15 < minimum 20
    assert c.put("/api/accounts/chk", headers=H, json={"balance": 1}).status_code == 409        # linked: the bank owns the balance
    assert c.put("/api/accounts/chk", headers=H, json={"min_balance": 500}).json()["min_balance"] == 500
    assert c.put("/api/accounts/chk", headers=H, json={"min_balance": -1}).json()["min_balance"] is None   # negative clears it
    assert c.delete("/api/accounts/chk", headers=H).status_code == 409
    c.post("/api/subscriptions", headers=H, json=sub(account_id=a["id"]))
    assert c.delete(f"/api/accounts/{a['id']}", headers=H).status_code == 200
    assert c.get("/api/subscriptions", headers=H).json()["items"][0]["account_id"] is None      # binding released, subscription kept
    assert c.get("/api/accounts/ghost", headers=H).status_code == 404


def test_transactions_endpoint_filters_and_validation(api):
    c, db = api
    r = c.get("/api/transactions?direction=in&limit=500&sort=amount_desc", headers=H).json()
    assert r["total"] == db.one("SELECT COUNT(*) n FROM transactions WHERE amount>0")["n"] and all(t["amount"] > 0 for t in r["items"]) and r["sum_out"] == 0
    r = c.get("/api/transactions?q=payroll&account_id=chk&start=2026-10-01", headers=H).json()
    assert r["total"] >= 2 and all("PAYROLL" in t["name"] and t["date"] >= "2026-10-01" for t in r["items"])
    assert c.get("/api/transactions?direction=sideways", headers=H).status_code == 422
    assert c.get("/api/transactions?start=yesterday", headers=H).status_code == 422
    assert c.get("/api/transactions?limit=100000", headers=H).json()["items"].__len__() <= 500
    assert c.get("/api/transactions?q=%25%27%3B+DROP+TABLE+transactions%3B--", headers=H).json()["total"] == 0     # SQL-injection-shaped input is just text
    assert db.one("SELECT COUNT(*) n FROM transactions")["n"] > 100


def test_categories_rules_and_budgets(api):
    c, db = api
    assert {"Income", "Food", "Clothes", "Fun", "Misc", "Games", "Gas"} <= {x["name"] for x in c.get("/api/categories", headers=H).json()}
    cats = c.post("/api/categories", headers=H, json={"name": "Pets"}).json()
    assert "Pets" in {x["name"] for x in cats} and c.post("/api/categories", headers=H, json={"name": ""}).status_code == 422
    tx = c.get("/api/transactions?q=starbucks&limit=1", headers=H).json()["items"][0]
    r = c.put(f"/api/transactions/{tx['id']}/category", headers=H, json={"category": "Pets", "make_rule": True}).json()
    assert r["updated"] >= 1 and c.get("/api/category-rules", headers=H).json()[0]["category"] == "Pets"
    assert c.put(f"/api/transactions/{tx['id']}/category", headers=H, json={"category": "Zzz"}).status_code == 422
    assert c.put("/api/transactions/nope/category", headers=H, json={"category": "Pets"}).status_code == 404
    c.put("/api/budgets", headers=H, json={"category": "Pets", "monthly_amount": 100})
    assert c.put("/api/budgets", headers=H, json={"category": "Zzz", "monthly_amount": 1}).status_code == 422
    assert c.get("/api/budgets?month=2026-10", headers=H).json()["items"][0]["category"] == "Pets"
    assert c.get("/api/budgets?month=oct", headers=H).status_code == 422
    assert c.delete("/api/categories/Food", headers=H).status_code == 409                       # built-in
    assert c.delete("/api/categories/Pets", headers=H).status_code == 200
    assert c.get(f"/api/transactions?q={tx['name'].split()[0]}&limit=1", headers=H).json()["items"][0]["category"] == "Misc"
    assert c.get("/api/category-rules", headers=H).json() == [] and c.get("/api/budgets", headers=H).json()["items"] == []


# ---------------------------------------------------------------- savings and planner
def test_savings_crud_and_validation(api):
    c, _ = api
    cd = c.post("/api/savings", headers=H, json={"name": "Ally CD", "kind": "cd", "balance": 5000, "apy": 4.8, "opened": "2026-05-01", "maturity": "2027-05-01", "term_months": 12, "goal": 10000}).json()
    assert cd["days_to_maturity"] == 175 and cd["value_at_maturity"] > 5000 and cd["goal_progress"] == 0.5
    c.post("/api/savings", headers=H, json={"name": "HYSA", "balance": 2000, "apy": 4, "account_id": "sav"})
    s = c.get("/api/savings", headers=H).json()
    assert s["by_kind"].keys() == {"cd", "savings"} and next(i for i in s["items"] if i["linked"])["balance"] != 2000        # takes the account's balance
    for bad in [dict(kind="stocks"), dict(apy=150), dict(balance=-1), dict(maturity="2020-01-01", opened="2021-01-01"), dict(maturity="x"), dict(account_id="ghost")]:
        assert c.post("/api/savings", headers=H, json={"name": "x", **bad}).status_code in (404, 422), bad
    assert c.put(f"/api/savings/{cd['id']}", headers=H, json={"name": "Ally CD", "kind": "cd", "balance": 6000}).json()["balance"] == 6000
    assert c.put("/api/savings/999", headers=H, json={"name": "x"}).status_code == 404
    assert c.delete(f"/api/savings/{cd['id']}", headers=H).status_code == 200


def test_notes_roundtrip(api):
    c, _ = api
    n = c.post("/api/notes", headers=H, json={"title": "Budget ideas", "body": "Cut dining out", "pinned": True}).json()
    assert n["author"] == "me" and c.put(f"/api/notes/{n['id']}", headers=H, json={"title": "Budget ideas v2", "body": "x"}).json()["title"] == "Budget ideas v2"
    assert c.post("/api/notes", headers=H, json={"title": " "}).status_code == 422 and c.put("/api/notes/99", headers=H, json={"title": "x"}).status_code == 404
    assert [x["id"] for x in c.get("/api/notes", headers=H).json()] == [n["id"]] and c.delete(f"/api/notes/{n['id']}", headers=H).status_code == 200


def test_projection_endpoint_and_alerts_endpoint(api):
    c, _ = api
    c.post("/api/subscriptions", headers=H, json=sub(name="Rent", amount=900, account_id="chk", next_date="2026-11-10"))
    p = c.get("/api/projection?days=30&account_id=chk", headers=H).json()
    assert p["bills_total"] >= 900 and p["end"] == pytest.approx(p["start"] - p["bills_total"] + p["income_total"], abs=0.02)
    assert isinstance(c.get("/api/alerts", headers=H).json(), list)


def test_upgrade_from_old_database(tmp_path):
    """A database made by the first release (no new columns) must open, migrate and keep its data."""
    import sqlite3
    f = str(tmp_path / "old.db")
    con = sqlite3.connect(f)
    con.executescript("""CREATE TABLE subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', cycle TEXT NOT NULL,
        next_date TEXT NOT NULL, category TEXT DEFAULT '', url TEXT DEFAULT '', notes TEXT DEFAULT '', source TEXT DEFAULT 'manual', plaid_stream_id TEXT UNIQUE, active INTEGER DEFAULT 1);
        INSERT INTO subscriptions(name,amount,cycle,next_date) VALUES('Old Netflix',15,'monthly','2026-11-20');
        CREATE TABLE accounts(id TEXT PRIMARY KEY, source TEXT NOT NULL, institution TEXT, name TEXT, kind TEXT, balance REAL, currency TEXT DEFAULT 'USD', updated TEXT);
        CREATE TABLE transactions(id TEXT PRIMARY KEY, account_id TEXT, date TEXT, name TEXT, amount REAL, currency TEXT DEFAULT 'USD', category TEXT, pending INTEGER DEFAULT 0, source TEXT);""")
    con.commit(); con.close()
    c = TestClient(goblin.create_app(db=DB(f), today=lambda: TODAY))
    s = c.get("/api/subscriptions", headers=H).json()["items"][0]
    assert s["name"] == "Old Netflix" and s["every"] == 1 and s["unit"] == "month" and s["payer"] == "me" and s["next_due"] == "2026-11-20" and s["monthly"] == 15
