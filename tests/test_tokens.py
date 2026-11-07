"""API tokens for Home Assistant / scripts, through the gateway to the right goblin, with scopes, revoke and rate limits."""
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.seed import seed
from goblinbank import external, gateway, goblin
from goblinbank.db import DB
from goblinbank.security import sha256

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "API_RATE_PER_MIN": "5"}.items():
        monkeypatch.setenv(k, v)


def rig():
    db = DB(":memory:")
    seed(db, 3, TODAY)
    db.conn.execute("INSERT INTO savings(name,kind,balance,apy) VALUES('HYSA','savings',4000,4)"); db.conn.commit()
    g = TestClient(goblin.create_app(db=db, today=lambda: TODAY))
    g.post("/api/goals", headers=H, json={"name": "House", "target": 3000, "auto_allocate": True})

    def to_goblin(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("login")}], "orb_keys": []}
    return g, TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin))), db


def mint(g, scopes, name="Home Assistant"):
    r = g.post("/api/tokens", headers=H, json={"name": name, "scopes": scopes})
    assert r.status_code == 200, r.text
    return r.json()


def test_token_lifecycle_and_storage():
    g, gw, db = rig()
    t = mint(g, ["summary", "bills"])
    assert t["token"].startswith("gb_alice_") and external.goblin_of(t["token"]) == "alice" and "not shown again" in t["note"]
    assert t["token"] not in str(db.all("SELECT * FROM api_tokens"))                      # only a hash is stored
    listing = g.get("/api/tokens", headers=H).json()
    assert [(x["name"], x["scopes"], x["revoked"]) for x in listing["items"]] == [("Home Assistant", ["bills", "summary"], 0)] and "transactions" in listing["scopes"]
    assert "token" not in listing["items"][0] and "token_hash" not in listing["items"][0]
    for bad in [{"name": "x", "scopes": []}, {"name": " ", "scopes": ["summary"]}, {"name": "x", "scopes": ["root"]}]:
        assert g.post("/api/tokens", headers=H, json=bad).status_code == 422
    assert g.delete(f"/api/tokens/{t['id']}", headers=H).status_code == 200 and g.delete("/api/tokens/99", headers=H).status_code == 404
    assert gw.get("/api/v1/me/summary", headers={"Authorization": "Bearer " + t["token"]}).status_code == 401      # revoked means dead


def test_gateway_routes_a_token_to_its_goblin_and_enforces_scopes():
    g, gw, _ = rig()
    t = mint(g, ["summary", "goals"])
    A = {"Authorization": "Bearer " + t["token"]}
    s = gw.get("/api/v1/me/summary", headers=A).json()
    assert s["savings_total"] == 4000 and s["savings_free"] == 1000 and s["goals_active"] == 1 and s["net_worth"] > 0 and "bills_30d" in s and "alerts_total" in s
    ha = gw.get("/api/v1/me/ha", headers=A).json()
    assert all(not isinstance(v, (dict, list)) for v in ha.values()) and "next_bill_name" in ha and ha["savings_free"] == 1000          # flat values only: sensor-friendly
    assert gw.get("/api/v1/me/goals", headers=A).json()["goals"][0]["name"] == "House"
    assert gw.get("/api/v1/me/transactions", headers=A).status_code == 403 and "transactions" in gw.get("/api/v1/me/transactions", headers=A).json()["detail"]      # not granted
    assert gw.get("/api/v1/me/bills", headers=A).status_code == 403
    assert gw.get("/api/v1/me/nonsense", headers=A).status_code == 404
    assert set(gw.get("/api/v1/me", headers=A).json()["resources"]) == {"ha", "goals", "summary"}
    # a token never works as a login, and a login never works as a token
    assert gw.get("/g/alice/api/overview", headers=A).status_code == 401
    assert gw.get("/api/v1/me/summary", headers={"Authorization": "Bearer login"}).status_code == 401


def test_bad_tokens_all_look_the_same():
    g, gw, _ = rig()
    good = mint(g, ["summary"])["token"]
    for tok in ["", "nope", "gb_alice_wrong", "gb_bob_" + good.split("_", 2)[2], "gb__x", good + "x", "GB_alice_" + good.split("_", 2)[2]]:
        r = gw.get("/api/v1/me/summary", headers={"Authorization": "Bearer " + tok} if tok else {})
        assert r.status_code == 401 and r.json()["detail"] == "invalid token", tok
    assert gw.get("/api/v1/me/summary").status_code == 401
    assert g.get("/external/summary", headers={**H, "X-API-Token": "nope"}).status_code == 401
    assert g.get("/external/summary").status_code == 401                                            # the goblin itself needs the gateway's secret too


def test_each_scope_returns_only_its_data_and_transactions_stay_private_by_default(monkeypatch):
    monkeypatch.setenv("API_RATE_PER_MIN", "50")
    g, gw, db = rig()
    t = mint(g, ["bills", "alerts", "savings", "budgets", "transactions"])
    A = {"Authorization": "Bearer " + t["token"]}
    assert gw.get("/api/v1/me/bills?days=14", headers=A).json()["days"] == 14
    assert "alerts" in gw.get("/api/v1/me/alerts", headers=A).json()
    sv = gw.get("/api/v1/me/savings", headers=A).json()
    assert (sv["total"], sv["earmarked"], sv["free"]) == (4000, 3000, 1000) and "account_id" not in sv["places"][0]
    assert "income_month" in gw.get("/api/v1/me/budgets?month=2026-10", headers=A).json()
    tx = gw.get("/api/v1/me/transactions?limit=5&direction=out", headers=A).json()
    assert len(tx["items"]) == 5 and set(tx["items"][0]) == {"date", "name", "amount", "category", "account_name"} and tx["items"][0]["amount"] < 0
    assert len(gw.get("/api/v1/me/transactions?limit=100000", headers=A).json()["items"]) <= 100
    assert gw.get("/api/v1/me/summary", headers=A).status_code == 403
    assert gw.get("/api/v1/me/budgets?month=oops", headers=A).status_code == 422                       # bad input is a clean 422, not a crash


def test_rate_limit_and_last_used():
    g, gw, db = rig()
    t = mint(g, ["summary"])
    A = {"Authorization": "Bearer " + t["token"]}
    codes = [gw.get("/api/v1/me/summary", headers=A).status_code for _ in range(8)]
    assert codes == [200] * 5 + [429] * 3                                                           # 5 a minute in this test
    other = mint(g, ["summary"], "Second")
    assert gw.get("/api/v1/me/summary", headers={"Authorization": "Bearer " + other["token"]}).status_code == 200        # limits are per token
    assert db.one("SELECT last_used FROM api_tokens WHERE id=?", (t["id"],))["last_used"]
    lim = external.Limiter(2, clock=iter([0, 1, 2, 61.5, 62]).__next__)
    assert [lim.ok("k") for _ in range(5)] == [True, True, False, True, True]                       # the window slides


def test_orb_summary_shows_only_what_was_shared():
    g, gw, db = rig()
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("login")}], "orb_keys": [{"sha256": sha256("orb"), "scopes": ["summary:read"]}]}
    def to_goblin(req):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    orb = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin)))
    O = {"Authorization": "Bearer orb"}
    assert orb.get("/api/v1/summary", headers=O).json()["goblins"]["alice"] == {"goblin": "alice"}                     # nothing shared yet
    g.put("/api/settings", headers=H, json={"share": ["net_worth", "bills", "alerts", "savings", "goals"]})
    a = orb.get("/api/v1/summary", headers=O).json()["goblins"]["alice"]
    assert set(a) == {"goblin", "net_worth", "bills", "alerts", "savings", "goals"} and a["savings"]["free"] == 1000 and a["goals"][0]["name"] == "House"
    assert "transactions" not in str(a)
    assert g.put("/api/settings", headers=H, json={"share": ["transactions"]}).status_code == 422                          # the Orb can never be given raw transactions


# ---------------------------------------------------------------- freshness: auto-sync and on-demand refresh
import time as _time
import json as _json


def counting_plaid():
    calls = {"sync": 0}

    def h(req: httpx.Request):
        p = req.url.path
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "i1", "access_token": "a"})
        if p == "/accounts/get":
            return httpx.Response(200, json={"accounts": [{"account_id": "x", "name": "Chk", "type": "depository", "balances": {"current": 10.0}}]})
        if p == "/transactions/sync":
            calls["sync"] += 1
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False})
        if p == "/transactions/recurring/get":
            return httpx.Response(200, json={"outflow_streams": []})
        return httpx.Response(404, json={"error_code": "X", "error_message": p})
    return httpx.MockTransport(h), calls


def fresh_rig(monkeypatch, **kw):
    monkeypatch.setenv("API_RATE_PER_MIN", "100")
    monkeypatch.setenv("PLAID_CLIENT_ID", "c"); monkeypatch.setenv("PLAID_SECRET", "s")
    t, calls = counting_plaid()
    db = DB(":memory:")
    g = TestClient(goblin.create_app(db=db, today=lambda: TODAY, plaid_transport=t, **kw))
    g.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Bank"})
    calls["sync"] = 0

    def to_goblin(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("login")}], "orb_keys": []}
    return g, TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin))), calls


def test_refresh_on_demand_needs_its_own_scope_and_is_rate_limited(monkeypatch):
    g, gw, calls = fresh_rig(monkeypatch)
    plain = {"Authorization": "Bearer " + mint(g, ["summary"])["token"]}
    full = {"Authorization": "Bearer " + mint(g, ["summary", "bills", "refresh"], "HA full")["token"]}
    assert gw.get("/api/v1/me/ha?refresh=1", headers=plain).status_code == 403 and calls["sync"] == 0              # no refresh scope, no sync
    assert gw.get("/api/v1/me/ha", headers=plain).json()["refreshed"] is False and calls["sync"] == 0               # a normal poll never triggers a bank sync
    a = gw.get("/api/v1/me/ha?refresh=1", headers=full).json()
    assert a["refreshed"] is True and calls["sync"] == 1 and a["synced_at"] and "net_worth" in a
    b = gw.get("/api/v1/me/ha?refresh=1", headers=full).json()
    assert b["refreshed"] is False and calls["sync"] == 1 and b["synced_at"] == a["synced_at"]                      # asked again right away: served from the last sync
    assert gw.get("/api/v1/me/bills?days=14&refresh=1", headers=full).json()["days"] == 14                          # the refresh flag does not leak into other parameters
    assert "refresh" in g.get("/api/tokens", headers=H).json()["scopes"]


def test_refresh_allowed_again_after_the_window(monkeypatch):
    g, gw, calls = fresh_rig(monkeypatch)
    full = {"Authorization": "Bearer " + mint(g, ["summary", "refresh"])["token"]}
    gw.get("/api/v1/me/ha?refresh=1", headers=full)
    assert calls["sync"] == 1
    monkeypatch.setattr("goblinbank.external.REFRESH_MIN_SECONDS", 0)
    gw.get("/api/v1/me/ha?refresh=1", headers=full)
    assert calls["sync"] == 2


def test_autosync_runs_on_its_own_and_can_be_switched_off(monkeypatch):
    g, gw, calls = fresh_rig(monkeypatch, autosync=True, autosync_tick=0.05)
    deadline = _time.time() + 5
    while calls["sync"] < 3 and _time.time() < deadline:
        _time.sleep(0.05)
    assert calls["sync"] >= 3                                                                                       # nobody pressed Sync
    assert g.put("/api/settings", headers=H, json={"sync_minutes": "0"}).json()["sync_minutes"] == "0"
    _time.sleep(0.3)
    n = calls["sync"]
    _time.sleep(0.4)
    assert calls["sync"] == n                                                                                       # 0 means off
    assert g.put("/api/settings", headers=H, json={"sync_minutes": "x"}).status_code == 422


def test_autosync_off_by_default_in_tests_and_survives_errors(monkeypatch):
    g, gw, calls = fresh_rig(monkeypatch)
    _time.sleep(0.2)
    assert calls["sync"] == 0                                                                                       # no thread unless asked
    def boom(req): raise httpx.ConnectError("down")
    monkeypatch.setenv("PLAID_CLIENT_ID", "c"); monkeypatch.setenv("PLAID_SECRET", "s")
    g2 = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, plaid_transport=httpx.MockTransport(boom), autosync=True, autosync_tick=0.05))
    _time.sleep(0.3)
    assert g2.get("/health").status_code == 200                                                                     # the app stays up while syncs fail


def test_spending_projection_is_shareable_with_the_orb():
    g, gw, db = rig()
    g.put("/api/settings", headers=H, json={"share": ["spending"]})
    s = g.get("/summary", headers=H).json()
    assert set(s) == {"goblin", "spending"} and set(s["spending"]) == {"so_far", "projected", "income_avg", "pct_projected", "left_after", "confidence"}
    assert "transactions" not in str(s) and s["spending"]["projected"] >= s["spending"]["so_far"]


def test_itemised_bill_and_alert_lists_are_separate_opt_ins_in_the_orbs_format():
    g, gw, db = rig()
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,created) VALUES('Netflix',15.49,'monthly',1,'month','2026-11-12','Subscriptions','x')")
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,payer,payer_name,created) VALUES('Dads Hulu',9,'monthly',1,'month','2026-11-20','Subscriptions','other','Dad','x')")
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES('neg','manual','Checking X','cash',-20)"); db.conn.commit()
    g.put("/api/settings", headers=H, json={"share": ["bills", "alerts"]})                      # totals only
    s = g.get("/summary", headers=H).json()
    assert "bill_list" not in s and "alert_list" not in s and "bills" in s and "alerts" in s    # sharing the totals does not share the lists
    g.put("/api/settings", headers=H, json={"share": ["bill_list", "alert_list"]})
    s = g.get("/summary", headers=H).json()
    assert set(s) == {"goblin", "bill_list", "alert_list"}
    nf = next(b for b in s["bill_list"] if b["name"] == "Netflix")
    assert nf == {"date": "2026-11-12", "name": "Netflix", "amount": 15.49, "category": "Subscriptions", "paid_by": "me"}
    assert next(b for b in s["bill_list"] if b["name"] == "Dads Hulu")["paid_by"] == "Dad"
    assert [b["date"] for b in s["bill_list"]] == sorted(b["date"] for b in s["bill_list"]) and all(b["date"] <= "2026-12-07" for b in s["bill_list"])      # the next 30 days
    assert any(a == {"severity": "critical", "kind": "negative_balance", "title": "Checking X is overdrawn", "detail": "Balance -20.00"} for a in s["alert_list"]) and len(s["alert_list"]) <= 30
    assert set(s["alert_list"][0]) == {"severity", "kind", "title", "detail"}
