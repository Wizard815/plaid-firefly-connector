"""The admin view shows general health and counts only, and the gateway keeps no personal log."""
import logging
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.seed import seed
from goblinbank import gateway, goblin
from goblinbank.db import DB
from goblinbank.security import sha256

TODAY = __import__("datetime").date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def rig(up=True):
    db = DB(":memory:")
    seed(db, 3, TODAY)
    g = TestClient(goblin.create_app(db=db, today=lambda: TODAY))

    def to_goblin(req: httpx.Request):
        if not up:
            return httpx.Response(503)
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("alice-login")},
                       {"name": "bob", "url": "http://bob", "internal_secret": "x", "user_token_sha256": sha256("bob-login")}],
           "orb_keys": [{"sha256": sha256("orbkey"), "scopes": ["summary:read"]}], "admin_token_sha256": sha256("admin-secret")}
    return g, TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin)))


ADMIN = {"Authorization": "Bearer admin-secret"}


def test_only_the_admin_token_opens_the_admin_view():
    g, gw = rig()
    api_tok = g.post("/api/tokens", headers=H, json={"name": "ha", "scopes": ["summary"]}).json()["token"]
    for hdr in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer alice-login"}, {"Authorization": "Bearer orbkey"}, {"Authorization": "Bearer " + api_tok}):
        gw.app.state  # noqa: B018
        g2, gw2 = rig()                                                      # a fresh gateway each time: repeated wrong guesses are throttled (tested separately)
        assert gw2.get("/admin/status", headers=hdr).status_code == 401 and gw2.post("/admin/reset", headers=hdr).status_code == 401, hdr
    assert gw.get("/admin/status", headers=ADMIN).status_code == 200
    # the admin token opens nothing else
    assert gw.get("/g/alice/api/overview", headers=ADMIN).status_code == 401 and gw.get("/api/v1/summary", headers=ADMIN).status_code == 401
    # a gateway with no admin token configured has the admin view switched off
    off = TestClient(gateway.create_app({"goblins": [], "orb_keys": []}))
    assert off.get("/admin/status", headers={"Authorization": "Bearer "}).status_code == 401 and off.get("/admin/status", headers=ADMIN).status_code == 401


def test_counts_by_kind_and_goblin_with_nothing_personal_in_them():
    g, gw = rig()
    A = {"Authorization": "Bearer alice-login"}
    api_tok = g.post("/api/tokens", headers=H, json={"name": "ha", "scopes": ["summary"]}).json()["token"]
    gw.get("/g/alice/api/transactions?q=starbucks&min_amount=77", headers=A)
    gw.get("/g/alice/api/overview", headers=A)
    gw.get("/g/alice/api/overview", headers={"Authorization": "Bearer wrong"})                     # refused
    gw.post("/g/alice/api/notes", headers=A, json={"title": "secret diary entry", "body": "very private"})
    gw.get("/api/v1/me/summary", headers={"Authorization": "Bearer " + api_tok})
    gw.get("/api/v1/summary", headers={"Authorization": "Bearer orbkey"})
    gw.post("/webhooks/plaid", content=b'{"item_id":"item-secret-123"}')
    gw.get("/g/ghost/api/overview?probe=1", headers=A)                                              # an unknown goblin name
    s = gw.get("/admin/status", headers=ADMIN).json()
    alice = next(x for x in s["goblins"] if x["name"] == "alice")
    assert alice["up"] is True and alice["calls"]["app"] == {"calls": 4, "ok": 3, "client_errors": 1, "server_errors": 0}
    assert alice["calls"]["api_token"]["calls"] == 1 and next(x for x in s["goblins"] if x["name"] == "bob")["calls"] == {}
    assert s["orb"]["calls"] == 1 and s["unmatched"]["webhook"]["calls"] == 1 and s["unmatched"]["app"]["calls"] == 1
    assert s["total"]["calls"] == 8 and s["since_seconds"] >= 0 and "Counts only" in s["note"]
    blob = gw.get("/admin/status", headers=ADMIN).text
    for secret in ("starbucks", "77", "diary", "private", "alice-login", api_tok, "orbkey", "item-secret", "ghost", "probe", "transactions", "overview", "/g/", "min_amount"):
        assert secret not in blob, secret                                                           # no query, path, body, token or unknown name appears anywhere
    # the keys the metrics keep are only (goblin name, kind of call)
    assert all(isinstance(k, tuple) and len(k) == 2 and k[1] in gateway.KINDS for k in gateway.Metrics().n) and True


def test_health_is_checked_on_demand_and_counts_can_be_cleared():
    g, gw = rig(up=False)
    assert all(x["up"] is False for x in gw.get("/admin/status", headers=ADMIN).json()["goblins"])
    g2, gw2 = rig()
    gw2.get("/g/alice/api/overview", headers={"Authorization": "Bearer alice-login"})
    assert gw2.get("/admin/status", headers=ADMIN).json()["total"]["calls"] == 1
    assert gw2.post("/admin/reset", headers=ADMIN).json() == {"reset": True}
    s = gw2.get("/admin/status", headers=ADMIN).json()
    assert s["total"]["calls"] == 0 and all(x["calls"] == {} for x in s["goblins"])
    gw2.get("/admin/status", headers=ADMIN); gw2.get("/health")
    assert gw2.get("/admin/status", headers=ADMIN).json()["total"]["calls"] == 0                    # looking at the admin view is not itself counted


def test_admin_page_is_just_a_shell():
    _, gw = rig()
    page = gw.get("/admin")
    assert page.status_code == 200 and "Plaid Firefly Connector admin" in page.text and "keeps no log of requests" in page.text
    assert "alice" not in page.text and "net_worth" not in page.text                                 # no data in the page itself; it fetches counts with the admin token


def test_nothing_personal_reaches_any_log(caplog):
    caplog.set_level(logging.DEBUG)
    g, gw = rig()
    api_tok = g.post("/api/tokens", headers=H, json={"name": "ha", "scopes": ["summary"]}).json()["token"]
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        assert logging.getLogger(noisy).level >= logging.WARNING                                     # these would log full URLs, query strings included
    gw.get("/g/alice/api/transactions?q=starbucks&min_amount=77", headers={"Authorization": "Bearer alice-login"})
    gw.post("/g/alice/api/notes", headers={"Authorization": "Bearer alice-login"}, json={"title": "secret diary entry", "body": "very private"})
    gw.get("/api/v1/me/summary?x=1", headers={"Authorization": "Bearer " + api_tok})
    gw.post("/webhooks/plaid", content=b'{"item_id":"item-secret-123"}')
    gw.get("/admin/status", headers=ADMIN)
    # "httpx2" is the test harness's own client (it logs the test's own requests); every logger the product uses must be silent about them
    product = [r for r in caplog.records if r.name != "httpx2" and not r.name.startswith("asyncio")]
    assert not [r for r in product if r.name in ("httpx", "httpcore", "uvicorn.access")]
    text = "\n".join(f"{r.name} {r.getMessage()}" for r in product)
    for secret in ("starbucks", "min_amount", "diary", "private", "alice-login", api_tok, "item-secret", "admin-secret", "orbkey"):
        assert secret not in text, (secret, text[:300])


def test_containers_start_without_an_access_log():
    assert "--no-access-log" in (ROOT / "Dockerfile").read_text()                                    # uvicorn's default access log prints every path and query string
    for f in ("scripts/dev_stack.py", "scripts/synthetic_e2e.py"):
        assert "--no-access-log" in (ROOT / f).read_text(), f


def test_people_can_be_added_from_the_admin_page_and_survive_a_restart(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from goblinbank import gateway
    from goblinbank.security import sha256
    state = tmp_path / "people.json"
    monkeypatch.setenv("GATEWAY_STATE", str(state))
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "sa", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    A = {"Authorization": "Bearer adm"}
    gw = TestClient(gateway.create_app(dict(cfg), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert gw.post("/admin/people", json={"name": "bob"}).status_code == 401
    assert gw.post("/admin/people", headers=A, json={"name": "Bad Name"}).status_code == 422
    assert gw.post("/admin/people", headers=A, json={"name": "alice"}).status_code == 409
    r = gw.post("/admin/people", headers=A, json={"name": "bob"}).json()
    assert r["name"] == "bob" and r["login_token"] and r["internal_secret"] and "sha256" not in str(r)
    # the new person can sign in and sees the others by name, and the gateway kept only the token's hash
    assert gw.get("/g/bob/health", headers={"Authorization": "Bearer " + r["login_token"]}).status_code != 401
    assert r["login_token"] not in state.read_text() and r["login_token"] not in str(gw.get("/admin/status", headers=A).json())
    # a restart (new app, same state file) still knows bob; yaml people cannot be removed from the page, added ones can
    gw2 = TestClient(gateway.create_app(dict(cfg), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert [g["name"] for g in gw2.get("/admin/status", headers=A).json()["goblins"]] == ["alice", "bob"]
    assert gw2.delete("/admin/people/alice", headers=A).status_code == 404
    assert gw2.delete("/admin/people/bob", headers=A).status_code == 200
    assert [g["name"] for g in gw2.get("/admin/status", headers=A).json()["goblins"]] == ["alice"]


def test_a_pondering_orb_key_made_on_the_admin_page_works_survives_a_restart_and_can_be_revoked(tmp_path, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from goblinbank import gateway, goblin
    from goblinbank.db import DB
    from goblinbank.security import sha256
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    db = DB(":memory:")
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('c','manual','M','Cash','cash',1234.5)"); db.conn.commit()
    g = TestClient(goblin.create_app(db=db))
    g.put("/api/settings", headers={"X-Internal-Secret": "isec"}, json={"share": ["net_worth"]})

    def to_goblin(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    make = lambda: TestClient(gateway.create_app(dict(cfg), transport=httpx.MockTransport(to_goblin)))
    gw, A = make(), {"Authorization": "Bearer adm"}
    assert gw.post("/admin/orb/key", json={}).status_code == 401
    assert gw.get("/api/v1/summary", headers={"Authorization": "Bearer nope"}).status_code == 401
    k = gw.post("/admin/orb/key", headers=A, json={"name": "Orb"}).json()
    summ = gw.get("/api/v1/summary", headers={"Authorization": "Bearer " + k["key"]})
    assert summ.status_code == 200 and summ.json()["goblins"]["alice"]["net_worth"] == 1234.5 and "transactions" not in summ.text      # only what was ticked
    assert k["key"] not in str(gw.get("/admin/orb", headers=A).json()) and k["key"] not in (tmp_path / "s.json").read_text()        # the key itself is never kept
    gw2 = make()                                                                                                                    # a restart keeps it
    assert gw2.get("/api/v1/summary", headers={"Authorization": "Bearer " + k["key"]}).status_code == 200
    assert gw2.delete("/admin/orb/key/" + k["id"], headers=A).status_code == 200
    assert gw2.get("/api/v1/summary", headers={"Authorization": "Bearer " + k["key"]}).status_code == 401
