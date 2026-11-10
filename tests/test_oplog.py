"""Logs say whether things are running and connected, and never anything personal."""
import logging

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin
from goblinbank.db import DB
from goblinbank.security import hash_password, sha256

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_gateway_logs_when_a_goblin_goes_down_and_comes_back_and_a_heartbeat_of_counts(caplog):
    caplog.set_level(logging.INFO, logger="goblinbank")
    state = {"up": True}

    def t(req: httpx.Request):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": True})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "s", "user_password_scrypt": hash_password(PW)}, {"name": "bob", "url": "http://bob", "internal_secret": "s", "user_token_sha256": sha256("tok")}], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(t)))
    chk = gw.app.state.check_goblins
    assert chk() == [] and "not answering" not in text(caplog)                      # all up at the start: nothing to say
    state["up"] = False
    assert chk() == ["goblin alice is not answering", "goblin bob is not answering"]
    assert chk() == []                                                              # still down: said once, not every minute
    state["up"] = True
    assert chk() == ["goblin alice is back up", "goblin bob is back up"]
    # traffic with personal-looking content, wrong sign-ins and a lockout
    tok = gw.post("/login", json={"name": "alice", "password": PW}).json()["token"]
    gw.get("/g/alice/api/transactions?q=starbucks&min_amount=42", headers={"Authorization": "Bearer " + tok})
    for i in range(8):
        gw.post("/login", json={"name": "alice", "password": f"wrong guess {i}"})
    hb = gw.app.state.heartbeat({})
    out = text(caplog)
    assert "gateway running:" in out and "failed sign-ins" in out and "sign-in is locked for" in out and "goblins up 2 of 2" in out
    for secret in (PW, tok, "starbucks", "min_amount", "Bearer", "wrong guess", "/api/transactions"):
        assert secret not in out, secret                                            # no password, token, search, path or guess ever reaches the log
    assert hb["calls"] >= 1


def test_goblin_logs_startup_sync_counts_webhooks_and_the_gateway_link_without_names_or_amounts(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="goblinbank")
    monkeypatch.setenv("PLAID_CLIENT_ID", "c"); monkeypatch.setenv("PLAID_SECRET", "s"); monkeypatch.setenv("GATEWAY_URL", "http://gateway")

    def plaid(req: httpx.Request):
        p = req.url.path
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-secret-1", "access_token": "tok-secret"})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False, "accounts": [{"account_id": "x", "name": "Secret Checking", "type": "depository", "balances": {"current": 123456.78}}]})
        return httpx.Response(200, json={"outflow_streams": []})
    down = {"v": True}
    gwt = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no")) if down["v"] else httpx.Response(200, json={}))
    c = TestClient(goblin.create_app(db=DB(":memory:"), plaid_transport=httpx.MockTransport(plaid), transport=gwt, ops_log=True))
    H = {"X-Internal-Secret": "isec"}
    assert "goblin started: database ok, Plaid configured (sandbox), 0 banks linked" in text(caplog)
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Secret Bank"})
    c.post("/sync", headers=H)
    c.post("/api/links/flush", headers=H)                                           # a background round: the gateway is unreachable
    down["v"] = False
    import time as _t
    c.app.router  # noqa: B018
    out = text(caplog)
    assert "sync finished: 1 banks pulled" in out and "the gateway is not reachable from here" in out
    for secret in ("Secret Bank", "Secret Checking", "123456", "item-secret", "tok-secret"):
        assert secret not in out, secret
    # a refused webhook is a warning with no detail
    assert c.post("/plaid/webhook", headers={**H, "Plaid-Verification": "x.y.z"}, content=b'{"item_id":"item-secret-1"}').status_code == 401
    assert "plaid webhook refused: signature did not verify" in text(caplog) and "item-secret" not in text(caplog)
