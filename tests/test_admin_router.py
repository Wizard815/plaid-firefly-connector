"""The admin page is the only screen: it links banks and runs the Firefly III connection for each person, through an allowlist, and never sees a balance or a transaction."""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin
from goblinbank.db import DB
from goblinbank.security import sha256
from tests.fake_firefly import FakeFirefly
from tests.test_firefly import approve, bank, d, H as GH

A = {"Authorization": "Bearer adm"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    ff, db = FakeFirefly(), DB(":memory:")
    plaid_calls = []

    def plaid(req: httpx.Request):
        plaid_calls.append(req.url.path)
        return httpx.Response(200, json={"link_token": "link-sandbox-x"} if req.url.path == "/link/token/create" else {})
    g = TestClient(goblin.create_app(db=db, firefly_transport=ff.transport(), plaid_transport=httpx.MockTransport(plaid)))
    g.put("/api/settings", headers={"X-Internal-Secret": "isec"}, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token"})

    def to_goblin(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    online = {"v": True}

    def route(req):
        if not online["v"]:
            raise httpx.ConnectError("down")
        return to_goblin(req)
    return TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(route))), db, ff, plaid_calls, online


def test_the_root_goes_to_the_admin_page_and_only_the_admin_can_operate_a_person(tmp_path, monkeypatch):
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    r = gw.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/admin"
    assert gw.get("/admin/p/alice/api/firefly/status").status_code == 401
    assert gw.get("/admin/p/alice/api/firefly/status", headers={"Authorization": "Bearer alice-login"}).status_code == 401
    assert gw.get("/admin/p/nobody/api/firefly/status", headers=A).status_code == 404


def test_only_allowlisted_calls_pass_and_nothing_personal_comes_back(tmp_path, monkeypatch):
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    bank(db)
    # the allowlist: bank links and Firefly III are reachable; transactions, goals, notes, the agent are not
    for method, path in (("GET", "api/transactions"), ("GET", "api/overview"), ("GET", "api/goals"), ("POST", "api/agent/chat"), ("GET", "api/accounts"), ("PUT", "api/settings"), ("DELETE", "api/notes/1")):
        assert gw.request(method, f"/admin/p/alice/{path}", headers=A).status_code == 403, path
    st = gw.get("/admin/p/alice/api/status", headers=A)
    assert st.status_code == 200 and set(st.json()) == {"plaid"}
    text = st.text + gw.get("/admin/p/alice/api/firefly/accounts", headers=A).text
    for personal in ("1000.0", "5000.0", "250.0", "KROGER", "ACME PAYROLL", "balance"):
        assert personal not in text, personal                                         # no amounts and no transactions
    accts = gw.get("/admin/p/alice/api/firefly/accounts", headers=A).json()
    assert {a["label"] for a in accts["accounts"]} >= {"Huntington - Everyday Checking ••4821", "Axos - High Yield ••0099"} and all("drift" in a for a in accts["accounts"])


def test_the_admin_can_run_the_firefly_side_and_the_bank_link_calls_for_a_person(tmp_path, monkeypatch):
    gw, db, ff, plaid_calls, online = world(tmp_path, monkeypatch)
    bank(db)
    assert gw.post("/admin/p/alice/api/firefly/test", headers=A).json()["version"] == "6.7.9"
    r = gw.post("/admin/p/alice/api/firefly/sync", headers=A).json()
    assert r["created"] == 0 and r["accounts_created"] == 0                                                      # nothing goes anywhere until the admin approves each account
    accts = gw.get("/admin/p/alice/api/firefly/accounts", headers=A).json()["accounts"]
    assert {a["mode"] for a in accts} == {"pending"}
    for aid, mode in (("plaid-chk", "create"), ("plaid-crd", "create"), ("plaid-sav", "create"), ("plaid-ira", "skip")):
        assert gw.put(f"/admin/p/alice/api/firefly/accounts/{aid}", headers=A, json={"mode": mode}).status_code == 200
    r = gw.post("/admin/p/alice/api/firefly/sync", headers=A).json()
    assert r["accounts_created"] == 3 and r["created"] == 3
    assert gw.get("/admin/p/alice/api/firefly/status", headers=A).json()["sent"] == 3
    assert gw.put("/admin/p/alice/api/firefly/accounts/plaid-sav", headers=A, json={"mode": "skip"}).status_code == 200
    assert gw.post("/admin/p/alice/api/firefly/accounts/plaid-chk/rebalance", headers=A).status_code == 200
    # linking a bank for them: a link token comes back for Plaid Link to open
    lt = gw.post("/admin/p/alice/plaid/link-token", headers=A)
    assert lt.status_code == 200 and lt.json()["link_token"] == "link-sandbox-x" and "/link/token/create" in plaid_calls
    online["v"] = False
    assert gw.get("/admin/p/alice/api/firefly/status", headers=A).status_code == 502         # their container is down: said plainly


def test_a_temporary_code_lets_one_person_link_their_own_banks_and_nothing_else(tmp_path, monkeypatch):
    import time as _t
    gw, db, ff, plaid_calls, _ = world(tmp_path, monkeypatch)
    bank(db)
    assert gw.post("/admin/people/alice/invite", json={"minutes": 30}).status_code == 401
    assert gw.post("/admin/people/alice/invite", headers=A, json={"minutes": 1}).status_code == 422
    assert gw.post("/admin/people/nobody/invite", headers=A, json={"minutes": 30}).status_code == 404
    inv = gw.post("/admin/people/alice/invite", headers=A, json={"minutes": 30}).json()
    assert inv["page"] == "/link" and len(inv["code"]) >= 12
    assert gw.get("/link").status_code == 200 and "Link my banks" in gw.get("/link").text
    assert "code" not in str(gw.get("/admin/invites", headers=A).json())                                         # the code is shown once, never listed
    assert gw.post("/link/session", json={"code": "wrong-code"}).status_code == 401
    ses = gw.post("/link/session", json={"code": inv["code"]}).json()
    L = {"Authorization": "Bearer " + ses["token"]}
    assert ses["name"] == "alice" and 28 <= ses["minutes_left"] <= 31
    # what a person with a code can do: see their banks (no amounts), link or relink, pull
    st = gw.get("/link/p/api/status", headers=L)
    assert st.status_code == 200 and "balance" not in st.text and gw.get("/link/p/api/firefly/status", headers=L).status_code == 200
    assert gw.post("/link/p/plaid/link-token", headers=L, json={}).json()["link_token"] == "link-sandbox-x"
    # and what they cannot: decide where their accounts go, read anything else, or touch the admin page
    for method, path in (("PUT", "api/firefly/accounts/plaid-chk"), ("POST", "api/firefly/sync"), ("GET", "api/transactions"), ("GET", "api/firefly/accounts"), ("DELETE", "api/plaid/items/i1"), ("POST", "api/firefly/test")):
        assert gw.request(method, f"/link/p/{path}", headers=L, json={"mode": "create"} if method == "PUT" else None).status_code in (403, 404), path
    assert gw.get("/admin/status", headers=L).status_code == 401 and gw.get("/admin/p/alice/api/firefly/status", headers=L).status_code == 401
    assert gw.get("/link/p/api/status").status_code == 401 and gw.get("/link/p/api/status", headers={"Authorization": "Bearer lnk_made-up"}).status_code == 401
    # it runs out
    real = _t.time
    monkeypatch.setattr(_t, "time", lambda: real() + 31 * 60)
    assert gw.get("/link/p/api/status", headers=L).status_code == 401 and gw.post("/link/session", json={"code": inv["code"]}).status_code == 401
    monkeypatch.setattr(_t, "time", real)
    # revoking cuts a live session off at once
    inv2 = gw.post("/admin/people/alice/invite", headers=A, json={"minutes": 30}).json()
    L2 = {"Authorization": "Bearer " + gw.post("/link/session", json={"code": inv2["code"]}).json()["token"]}
    iid = gw.get("/admin/invites", headers=A).json()["invites"][0]["id"]
    assert gw.delete(f"/admin/invites/{iid}", headers=A).status_code == 200 and gw.get("/link/p/api/status", headers=L2).status_code == 401


def test_a_firefly_account_takes_one_bank_account_and_a_person_cannot_approve_their_own(tmp_path, monkeypatch):
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    bank(db)
    mine = ff.add_account("Shared firefly account")
    r = gw.put("/admin/p/alice/api/firefly/accounts/plaid-chk", headers=A, json={"mode": "link", "ff_id": mine})
    assert r.status_code == 200
    r = gw.put("/admin/p/alice/api/firefly/accounts/plaid-sav", headers=A, json={"mode": "link", "ff_id": mine})
    assert r.status_code == 422 and "takes one bank account" in r.json()["detail"]                       # one Firefly III account does not collect everything
    assert gw.put("/admin/p/alice/api/firefly/accounts/plaid-sav", headers=A, json={"mode": "pending"}).status_code == 200
    gw.post("/admin/p/alice/api/firefly/sync", headers=A)
    sent = {t["external_id"] for g in ff.groups.values() for t in g["transactions"]}
    assert sent == {"t1", "t2"}                                                                          # only the approved account's transactions went to that Firefly account


def test_each_persons_firefly_address_and_token_are_attached_from_their_own_panel(tmp_path, monkeypatch):
    gw, *_ = world(tmp_path, monkeypatch)
    assert gw.put("/admin/people/alice/firefly", json={"url": "https://ff.example.com", "token": "T"}).status_code == 401
    assert gw.put("/admin/people/nobody/firefly", headers=A, json={"url": "https://ff.example.com"}).status_code == 404
    r = gw.put("/admin/people/alice/firefly", headers=A, json={"url": "https://ff.example.com", "token": "eyJ" + "a" * 60}).json()
    assert r == {"ok": True, "url": "https://ff.example.com", "token_set": True}
    pol = gw.get("/admin/policy", headers=A).json()
    assert pol["people"]["alice"]["firefly_url"] == "https://ff.example.com" and pol["firefly_token_set"] == {"alice": True} and "eyJ" + "a" * 60 not in str(pol)
    assert gw.put("/admin/people/alice/firefly", headers=A, json={"url": "https://ff2.example.com", "token": ""}).json()["token_set"] is True   # blank token keeps the saved one
    assert gw.put("/admin/people/alice/firefly", headers=A, json={"url": "", "token": ""}).json() == {"ok": True, "url": "", "token_set": False}   # no address: disconnected
    assert "alice" not in gw.get("/admin/policy", headers=A).json()["firefly_token_set"]
