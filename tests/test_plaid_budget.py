"""Keeping the Plaid bill small: throttled syncs, once-a-day slow endpoints, a hard monthly budget, and limits that the gateway admin sets."""
from collections import Counter

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin, settings, sync
from goblinbank.db import DB
from goblinbank.security import sha256

H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


def plaid(with_investments=False):
    n = Counter()

    def h(req: httpx.Request):
        p = req.url.path
        n[p] += 1
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": f"i{n[p]}", "access_token": "a"})
        if p == "/link/token/create":
            return httpx.Response(200, json={"link_token": "link-x"})
        if p == "/accounts/get":
            accts = [{"account_id": "x", "name": "Chk", "type": "depository", "balances": {"current": 10.0}}]
            if with_investments:
                accts.append({"account_id": "y", "name": "IRA", "type": "investment", "balances": {"current": 99.0}})
            return httpx.Response(200, json={"accounts": accts})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False})
        if p == "/transactions/recurring/get":
            return httpx.Response(200, json={"outflow_streams": []})
        if p == "/investments/holdings/get":
            return httpx.Response(200, json={"holdings": [], "securities": []})
        return httpx.Response(404, json={"error_code": "X", "error_message": p})
    return httpx.MockTransport(h), n


def rig(**kw):
    t, n = plaid(**kw)
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, plaid_transport=t))
    return c, db, n


def link(c, name="Bank"):
    return c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": name})


def test_a_bank_is_asked_at_most_every_three_hours_and_slow_endpoints_once_a_day(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(sync, "clock", lambda: now[0])
    c, db, n = rig(with_investments=True)
    assert link(c).status_code == 200
    first = dict(n)
    assert first["/transactions/sync"] == 1 and first["/transactions/recurring/get"] == 1 and first["/investments/holdings/get"] == 1
    n.clear()                                                             # ten hours of the 15-minute timer (rounds that are not forced):
    for _ in range(40):
        now[0] += 900
        sync.sync_all(db, goblin_plaid(c, db, n))
    assert n["/transactions/sync"] <= 4 and n["/transactions/recurring/get"] <= 1 and n["/investments/holdings/get"] <= 1


def goblin_plaid(c, db, n):
    from goblinbank.plaid import Plaid
    t, _ = plaid(with_investments=True)
    p = Plaid(httpx.MockTransport(lambda r: (n.update([r.url.path]), t.handler(r))[1]))
    p.client_id, p.secret = "c", "s"
    return p


def test_the_default_budget_is_five_dollars_and_blocks_a_bank_that_would_exceed_it():
    c, db, n = rig()
    assert settings.get(db, "plaid_budget_month") == "5"
    for i in range(11):                                                   # $0.45 each: eleven fit in $5.00
        assert link(c, f"B{i}").status_code == 200, i
    r = link(c, "B12")
    assert r.status_code == 409 and "budget" in r.json()["detail"]
    assert c.post("/plaid/link-token", headers=H).status_code == 409      # and it will not even open the sign-in window
    u = c.get("/api/plaid/usage", headers=H).json()
    assert u["budget"] == 5 and u["est_monthly_cost"] == 4.95 and u["room_for_items"] == 0
    c.put("/api/settings", headers=H, json={"plaid_budget_month": "6"})
    assert link(c, "B12").status_code == 200
    c.put("/api/settings", headers=H, json={"plaid_budget_month": ""})   # empty = no limit
    assert link(c, "B13").status_code == 200


def gateway_with_policy(tmp_path, monkeypatch, goblin_app):
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    tc = TestClient(goblin_app)

    def to_goblin(req: httpx.Request):
        r = tc.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    return TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin))), tc


def test_the_gateway_admin_sets_the_limits_and_the_goblin_obeys(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    holder = {}

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    t, n = plaid()
    db = DB(":memory:")
    g = goblin.create_app(db=db, plaid_transport=t, transport=httpx.MockTransport(to_gateway))
    gw, gtc = gateway_with_policy(tmp_path, monkeypatch, g)
    holder["gw"] = gw
    A = {"Authorization": "Bearer adm"}
    assert gw.get("/internal/policy", headers={"X-Goblin": "alice", "X-Internal-Secret": "isec"}).json() == {}
    assert gw.put("/admin/policy", json={"default": {}}).status_code == 401
    assert gw.put("/admin/policy", headers=A, json={"default": {"plaid_budget_month": "lots"}}).status_code == 422
    assert gw.put("/admin/policy", headers=A, json={"default": {"nonsense": 1}}).status_code == 422
    r = gw.put("/admin/policy", headers=A, json={"default": {"plaid_budget_month": 1, "plaid_daily_cap": 50, "plaid_min_minutes": 360, "plaid_use_recurring": "off"}})
    assert r.status_code == 200
    s = gtc.get("/api/settings", headers=H).json()                          # the goblin picks it up and shows it as managed
    assert s["plaid_budget_month"] == "1" and s["plaid_daily_cap"] == "50" and s["plaid_min_minutes"] == "360" and s["plaid_use_recurring"] == "off"
    assert set(s["managed"]) == {"plaid_budget_month", "plaid_daily_cap", "plaid_min_minutes", "plaid_use_recurring"}
    assert gtc.put("/api/settings", headers=H, json={"plaid_budget_month": "100"}).status_code == 409      # the person cannot raise it
    assert gtc.put("/api/settings", headers=H, json={"sync_minutes": "30"}).status_code == 200               # other settings are still theirs
    # $1 budget with recurring off ($0.30 per bank): three banks fit, a fourth does not
    for i in range(3):
        assert link(gtc, f"B{i}").status_code == 200
    assert link(gtc, "B4").status_code == 409
    # a per-person override beats the default, and the admin page sees counts only
    gw.put("/admin/policy", headers=A, json={"default": {"plaid_budget_month": 1}, "people": {"alice": {"plaid_budget_month": 3}}})
    status = gw.get("/admin/status", headers=A).json()["goblins"][0]
    assert status["plaid"]["items"] == 3 and "est_monthly_cost" in status["plaid"] and set(status["plaid"]) <= {"items", "est_monthly_cost", "budget", "calls_today", "daily_cap", "calls_month", "min_minutes"}
