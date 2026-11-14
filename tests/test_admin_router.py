"""The admin page is the only screen: it links banks and runs the Firefly III connection for each person, through an allowlist, and never sees a balance or a transaction."""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from plaidproxy import gateway, worker
from plaidproxy.db import DB
from plaidproxy.security import sha256
from tests.fake_firefly import FakeFirefly
from tests.test_firefly import approve, bank, d, H as GH

A = {"Authorization": "Bearer adm"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    ff, db = FakeFirefly(), DB(":memory:")
    plaid_calls = []

    def plaid(req: httpx.Request):
        plaid_calls.append(req.url.path)
        return httpx.Response(200, json={"link_token": "link-sandbox-x"} if req.url.path == "/link/token/create" else {})
    g = TestClient(worker.create_app(db=db, firefly_transport=ff.transport(), plaid_transport=httpx.MockTransport(plaid)))
    g.put("/api/settings", headers={"X-Internal-Secret": "isec"}, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token"})

    def to_worker(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"people": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    online = {"v": True}

    def route(req):
        if not online["v"]:
            raise httpx.ConnectError("down")
        return to_worker(req)
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


def test_a_joint_account_is_shared_with_a_second_person_who_must_approve_it_for_their_own_firefly(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    holder = {}

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    def person(name):
        monkeypatch.setenv("PERSON_NAME", name)
        ff, db = FakeFirefly(), DB(":memory:")
        g = TestClient(worker.create_app(db=db, firefly_transport=ff.transport(), transport=httpx.MockTransport(to_gateway)))
        g.put("/api/settings", headers={"X-Internal-Secret": "isec"}, json={"firefly_url": f"https://{name}.example.com", "firefly_token": "good-token"})
        return g, db, ff
    (ga, dba, ffa), (gb, dbb, ffb) = person("alice"), person("bob")
    bank(dba)
    apps = {"alice": ga, "bob": gb}

    def to_worker(req: httpx.Request):
        r = apps[req.url.host].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"people": [{"name": n, "url": f"http://{n}", "internal_secret": "isec", "user_token_sha256": sha256("t" + n)} for n in apps], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_worker)))
    holder["gw"] = gw
    approve(ga)
    assert gw.put("/admin/p/alice/api/shares/plaid-chk", json={"peer": "bob"}).status_code == 401
    assert gw.put("/admin/p/alice/api/shares/plaid-chk", headers=A, json={"peer": "bob"}).status_code == 200
    import time as _t
    for _ in range(50):                                                   # the share goes out in the background
        if dbb.one("SELECT COUNT(*) n FROM transactions")["n"]:
            break
        _t.sleep(0.1)
    # bob has the account and its two transactions, labelled as shared, and NOTHING has gone to bob's Firefly III yet
    acc = dbb.one("SELECT * FROM accounts")
    assert acc["id"] == "shared:alice:plaid-chk" and acc["source"] == "shared" and acc["institution"].startswith("Shared by alice")
    assert {r["id"] for r in dbb.all("SELECT id FROM transactions")} == {"shared:alice:t1", "shared:alice:t2", "shared:alice:t3"}
    assert gb.post("/api/firefly/sync", headers=GH).json()["created"] == 0 and ffb.accounts == {}
    lst = gw.get("/admin/p/bob/api/firefly/accounts", headers=A).json()["accounts"]
    assert [a["mode"] for a in lst] == ["pending"] and "balance" not in lst[0]
    assert {a["id"]: a["shared_with"] for a in gw.get("/admin/p/alice/api/firefly/accounts", headers=A).json()["accounts"]}["plaid-chk"] == "bob"
    # bob's admin approves it for bob's Firefly III: it arrives there once
    assert gb.put("/api/firefly/accounts/shared:alice:plaid-chk", headers=GH, json={"mode": "create"}).status_code == 200
    r = gb.post("/api/firefly/sync", headers=GH).json()
    assert r["accounts_created"] == 1 and r["created"] == 2 and len(ffb.accounts) == 1
    # a later change at the bank reaches bob: a new transaction and a removed one
    dba.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES('t9','plaid-chk',?,'NEW SHOP',-9.0,'Shopping',0,'plaid')", (d(1),))
    dba.conn.execute("DELETE FROM transactions WHERE id='t2'"); dba.conn.commit()
    assert ga.post("/sync", headers=GH).status_code == 200
    assert {r["id"] for r in dbb.all("SELECT id FROM transactions")} == {"shared:alice:t1", "shared:alice:t3", "shared:alice:t9"}
    # alice's own Firefly is untouched by any of this, and stopping the share tells bob's side to stop
    assert gw.put("/admin/p/alice/api/shares/plaid-chk", headers=A, json={"peer": ""}).status_code == 200
    for _ in range(50):
        if dbb.one("SELECT mode FROM ff_accounts WHERE account_id='shared:alice:plaid-chk'")["mode"] == "skip":
            break
        _t.sleep(0.1)
    assert dbb.one("SELECT mode FROM ff_accounts WHERE account_id='shared:alice:plaid-chk'")["mode"] == "skip"


def test_a_person_cannot_do_what_only_the_admin_may(tmp_path, monkeypatch):
    """Approving a bank account for Firefly III, and sharing a joint account, are the admin's decisions: a person's own login cannot do them."""
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    bank(db)
    P = {"Authorization": "Bearer t"}                                       # alice's own login token
    assert gw.get("/g/alice/api/firefly/accounts", headers=P).status_code == 200            # she can look
    assert gw.put("/g/alice/api/firefly/accounts/plaid-chk", headers=P, json={"mode": "create"}).status_code == 403
    assert gw.put("/g/alice/api/shares/plaid-chk", headers=P, json={"peer": "bob"}).status_code == 403
    assert gw.put("/g/alice/api/settings", headers=P, json={"firefly_url": "https://evil.example.com"}).status_code == 403
    assert db.one("SELECT mode FROM ff_accounts WHERE account_id='plaid-chk'")["mode"] == "pending"
    assert gw.put("/admin/p/alice/api/firefly/accounts/plaid-chk", headers=A, json={"mode": "create"}).status_code == 200      # the admin still can


@pytest.mark.parametrize("path", ["api/firefly/accounts/plaid-chk/", "api//firefly/accounts/plaid-chk", "api/firefly/./accounts/plaid-chk", "api/firefly/x/../accounts/plaid-chk",
                                  "api/firefly/accounts/plaid-chk%2F", "./api/firefly/accounts/plaid-chk", "api/firefly/accounts/plaid%2Dchk"])
def test_odd_spellings_do_not_get_round_the_admin_rule(tmp_path, monkeypatch, path):
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    bank(db)
    gw.get("/admin/p/alice/api/firefly/accounts", headers=A)                    # every account gets its 'pending' row
    gw.put("/g/alice/" + path, headers={"Authorization": "Bearer t"}, json={"mode": "create"})
    assert db.one("SELECT mode FROM ff_accounts WHERE account_id='plaid-chk'")["mode"] == "pending"


def test_a_person_cannot_reach_the_workers_internal_endpoints(tmp_path, monkeypatch):
    gw, db, ff, _, _ = world(tmp_path, monkeypatch)
    P = {"Authorization": "Bearer t"}
    r = gw.post("/g/alice/internal/share/inbox", headers=P, json={"type": "share-data", "sender": "bob", "payload": {"account": {"id": "x", "name": "Fake"}, "transactions": []}})
    assert r.status_code == 404 and not db.one("SELECT 1 FROM accounts WHERE id LIKE 'shared:%'")
    assert gw.get("/g/alice/internal/policy", headers=P).status_code == 404


def two_people(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    holder = {}

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    def person(name):
        monkeypatch.setenv("PERSON_NAME", name)
        ff, db = FakeFirefly(), DB(":memory:")
        g = TestClient(worker.create_app(db=db, firefly_transport=ff.transport(), transport=httpx.MockTransport(to_gateway)))
        g.put("/api/settings", headers={"X-Internal-Secret": "isec"}, json={"firefly_url": f"https://{name}.example.com", "firefly_token": "good-token"})
        return g, db, ff
    (ga, dba, ffa), (gb, dbb, ffb) = person("alice"), person("bob")
    bank(dba)
    apps = {"alice": ga, "bob": gb}

    def to_worker(req: httpx.Request):
        r = apps[req.url.host].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"people": [{"name": n, "url": f"http://{n}", "internal_secret": "isec", "user_token_sha256": sha256("t" + n)} for n in apps], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    holder["gw"] = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_worker)))
    return holder["gw"], (ga, dba, ffa), (gb, dbb, ffb)


def until(cond, n=60):
    import time as _t
    for _ in range(n):
        if cond():
            return True
        _t.sleep(0.05)
    return False


def test_people_ask_and_answer_joint_accounts_themselves(tmp_path, monkeypatch):
    gw, (ga, dba, ffa), (gb, dbb, ffb) = two_people(tmp_path, monkeypatch)
    PA, PB = {"Authorization": "Bearer talice"}, {"Authorization": "Bearer tbob"}          # each person's own login
    o = gw.get("/g/alice/api/joint", headers=PA).json()
    assert o["people"] == ["bob"] and "plaid-chk" in {a["id"] for a in o["my_accounts"]}
    assert gw.post("/g/alice/api/joint/plaid-chk/ask", headers=PA, json={"peer": "mallory"}).status_code == 422
    assert gw.post("/g/alice/api/joint/plaid-chk/ask", headers=PA, json={"peer": "alice"}).status_code == 422
    # alice asks: bob is asked, and only the account's name goes
    assert gw.post("/g/alice/api/joint/plaid-chk/ask", headers=PA, json={"peer": "bob"}).status_code == 200
    assert until(lambda: gw.get("/g/bob/api/joint", headers=PB).json()["incoming"])
    inc = gw.get("/g/bob/api/joint", headers=PB).json()["incoming"][0]
    assert inc["sender"] == "alice" and inc["account_id"] == "plaid-chk" and inc["state"] == "asked" and inc["mask"]
    assert not dbb.one("SELECT COUNT(*) n FROM transactions")["n"] and gw.get("/g/alice/api/joint", headers=PA).json()["mine"][0]["state"] == "requested"
    # bob accepts, choosing a new account in his Firefly III: the data comes and goes there without anyone else approving it
    assert gw.post("/g/bob/api/joint/from/alice/plaid-chk/accept", headers=PB, json={"mode": "link"}).status_code == 422      # link needs which account
    assert gw.post("/g/bob/api/joint/from/alice/plaid-chk/accept", headers=PB, json={"mode": "create"}).status_code == 200
    assert until(lambda: gw.get("/g/alice/api/joint", headers=PA).json()["mine"][0]["state"] == "active")
    ga.post("/sync", headers=GH)
    assert until(lambda: dbb.one("SELECT COUNT(*) n FROM transactions")["n"] == 3)
    assert dbb.one("SELECT mode FROM ff_accounts WHERE account_id='shared:alice:plaid-chk'")["mode"] == "create"
    gb.post("/api/firefly/sync", headers=GH)                                              # it also goes on its own as soon as it arrives
    assert until(lambda: len(ffb.accounts) == 1 and len(ffb.groups) == 2)
    assert gw.post("/g/bob/api/joint/from/alice/plaid-chk/accept", headers=PB, json={"mode": "create"}).status_code == 422   # already answered
    # bob leaves: alice stops sending, bob's Firefly III gets nothing more
    assert gw.post("/g/bob/api/joint/from/alice/plaid-chk/leave", headers=PB).status_code == 200
    assert until(lambda: not gw.get("/g/alice/api/joint", headers=PA).json()["mine"])
    assert dbb.one("SELECT mode FROM ff_accounts WHERE account_id='shared:alice:plaid-chk'")["mode"] == "skip"
    # alice asks again and bob says no: nothing moves
    gw.post("/g/alice/api/joint/plaid-chk/ask", headers=PA, json={"peer": "bob"})
    assert until(lambda: gw.get("/g/bob/api/joint", headers=PB).json()["incoming"])
    assert gw.post("/g/bob/api/joint/from/alice/plaid-chk/decline", headers=PB).status_code == 200
    assert until(lambda: not gw.get("/g/alice/api/joint", headers=PA).json()["mine"])
    # and alice can stop (or cancel) her own share herself
    gw.post("/g/alice/api/joint/plaid-chk/ask", headers=PA, json={"peer": "bob"})
    assert gw.delete("/g/alice/api/joint/plaid-chk", headers=PA).status_code == 200
    assert until(lambda: not gw.get("/g/bob/api/joint", headers=PB).json()["incoming"])
    # the admin's direct share is still the admin's alone
    assert gw.put("/g/alice/api/shares/plaid-chk", headers=PA, json={"peer": "bob"}).status_code == 403
