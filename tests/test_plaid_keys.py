"""Plaid keys entered in the app (or once on the gateway admin page) instead of a file."""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin, settings
from goblinbank.db import DB
from goblinbank.security import sha256

H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)
    for k in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV", "PLAID_BASE_URL", "PLAID_AUTH"):
        monkeypatch.delenv(k, raising=False)


def mock(seen):
    def h(req: httpx.Request):
        seen.append((str(req.url), req.headers.get("plaid-client-id"), req.headers.get("plaid-secret"), req.content))
        if req.url.path == "/institutions/get":
            ok = (req.headers.get("plaid-secret") or "").startswith("goodsecret") or b"goodsecret" in req.content
            return httpx.Response(200 if ok else 400, json={"institutions": []} if ok else {"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"})
        return httpx.Response(200, json={})
    return httpx.MockTransport(h)


def test_keys_saved_in_the_app_configure_plaid_and_the_secret_is_never_returned():
    seen = []
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, plaid_transport=mock(seen)))
    assert c.get("/api/status", headers=H).json()["plaid"]["configured"] is False
    assert c.post("/plaid/test", headers=H).status_code == 409                                   # nothing entered yet
    assert c.put("/api/settings", headers=H, json={"plaid_client_id": "has spaces"}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"plaid_env": "moon"}).status_code == 422
    r = c.put("/api/settings", headers=H, json={"plaid_client_id": "abc12345client", "plaid_secret": "badsecret123", "plaid_env": "sandbox"})
    assert r.status_code == 200 and r.json()["plaid_secret"] == {"set": True} and "badsecret123" not in r.text
    assert c.get("/api/status", headers=H).json()["plaid"]["configured"] is True
    t = c.post("/plaid/test", headers=H)
    assert t.status_code == 502 and "INVALID_API_KEYS" in t.json()["detail"]                       # a wrong secret says so right here
    c.put("/api/settings", headers=H, json={"plaid_secret": "goodsecret123", "plaid_env": "production"})
    t = c.post("/plaid/test", headers=H)
    assert t.json() == {"ok": True, "env": "production"} and seen[-1][0].startswith("https://production.plaid.com")        # the environment switches too, no restart
    assert db.one("SELECT value FROM settings WHERE key='plaid_secret'")["value"] != "goodsecret123"                            # encrypted at rest


def test_keys_from_the_gateway_admin_reach_every_goblin_and_the_secret_stays_off_the_admin_page(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    holder, seen = {}, []

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    db = DB(":memory:")
    g = TestClient(goblin.create_app(db=db, plaid_transport=mock(seen), transport=httpx.MockTransport(to_gateway)))

    def to_goblin(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    plaid_calls = []

    def to_plaid(req: httpx.Request):
        plaid_calls.append((str(req.url), req.headers.get("plaid-client-id"), req.headers.get("plaid-secret")))
        return httpx.Response(200, json={"institutions": []})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(lambda r: to_goblin(r) if r.url.host == "alice" else to_plaid(r))))
    holder["gw"] = gw
    A = {"Authorization": "Bearer adm"}
    assert gw.put("/admin/policy", headers=A, json={"default": {"plaid_client_id": "bad id"}}).status_code == 422
    assert gw.post("/admin/plaid/test", headers=A).status_code == 409
    r = gw.put("/admin/policy", headers=A, json={"default": {"plaid_client_id": "abc12345client", "plaid_secret": "goodsecret123", "plaid_env": "production", "plaid_budget_month": 4}})
    assert r.status_code == 200 and "goodsecret123" not in r.text and r.json()["plaid_secret_set"] is True
    assert "goodsecret123" not in gw.get("/admin/policy", headers=A).text                                    # never shown again
    gw.put("/admin/policy", headers=A, json={"default": {"plaid_budget_month": 3}})                             # saving other limits keeps the secret
    assert gw.get("/admin/policy", headers=A).json()["plaid_secret_set"] is True
    assert gw.post("/admin/plaid/test", headers=A).json() == {"ok": True, "env": "production"} and plaid_calls[-1] == ("https://production.plaid.com/institutions/get", "abc12345client", "goodsecret123")
    s = g.get("/api/settings", headers=H).json()                                                               # the goblin picks it up and treats it as managed
    assert {"plaid_client_id", "plaid_secret", "plaid_env"} <= set(s["managed"]) and s["plaid_client_id"] == "abc12345client" and s["plaid_secret"] == {"set": True}
    assert g.get("/api/status", headers=H).json()["plaid"]["configured"] is True
    assert g.put("/api/settings", headers=H, json={"plaid_secret": "mine"}).status_code == 409                  # a person cannot override the admin's keys
    assert g.post("/plaid/test", headers=H).json() == {"ok": True, "env": "production"}
    assert b"abc12345client" in seen[-1][3] and b"goodsecret123" in seen[-1][3]                                  # the goblin really calls Plaid with the admin's keys
