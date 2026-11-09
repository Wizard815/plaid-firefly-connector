"""Two goblins linked through a gateway: pairing, shared goals, shared notes, what never crosses, and joint-account shares."""
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin, links
from goblinbank.db import DB
from goblinbank.security import sha256

TODAY = date(2026, 11, 7)
SECRETS = {"alice": "sa", "bob": "sb", "cara": "sc"}


def world(monkeypatch, names=("alice", "bob"), online=None):
    """Goblins + gateway wired together in memory. `online` is a set the test can empty to cut a goblin off."""
    online = online if online is not None else set(names)
    clients, dbs = {}, {}
    routes = {}

    def route(req: httpx.Request):
        host = req.url.host
        if host == "gateway":
            r = gw.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        elif host in clients and host in online:
            r = clients[host].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        else:
            raise httpx.ConnectError("offline")
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    cfg = {"goblins": [{"name": n, "url": f"http://{n}", "internal_secret": SECRETS[n], "user_token_sha256": sha256("t")} for n in names], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(route)))
    for n in names:
        for k, v in {"GOBLIN_NAME": n, "INTERNAL_SECRET": SECRETS[n], "FIELD_KEY": Fernet.generate_key().decode(), "GATEWAY_URL": "http://gateway"}.items():
            monkeypatch.setenv(k, v)
        dbs[n] = DB(":memory:")
        clients[n] = TestClient(goblin.create_app(transport=httpx.MockTransport(route), db=dbs[n], today=lambda: TODAY))
    return clients, dbs, online


def H(n):
    return {"X-Internal-Secret": SECRETS[n]}


def link(c):
    assert c["alice"].post("/api/links", headers=H("alice"), json={"peer": "bob"}).status_code == 200
    assert c["bob"].get("/api/links", headers=H("bob")).json()["links"] == [{"peer": "alice", "status": "pending", "created": c["bob"].get("/api/links", headers=H("bob")).json()["links"][0]["created"]}]
    assert c["bob"].post("/api/links/alice/accept", headers=H("bob")).status_code == 200


def goals(c, n):
    return c[n].get("/api/goals", headers=H(n)).json()["items"]


def put_savings(db, bal):
    db.conn.execute("INSERT INTO savings(name,kind,balance,apy) VALUES('Joint HYSA','savings',?,4)", (bal,)); db.conn.commit()


def test_invite_accept_and_unlink(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    link(c)
    assert [x["status"] for x in c["alice"].get("/api/links", headers=H("alice")).json()["links"]] == ["active"]
    assert c["alice"].post("/api/links", headers=H("alice"), json={"peer": "alice"}).status_code == 422          # not yourself
    assert c["alice"].post("/api/links", headers=H("alice"), json={"peer": "Not A Name!"}).status_code == 422
    assert c["alice"].delete("/api/links/bob", headers=H("alice")).status_code == 200
    assert c["bob"].get("/api/links", headers=H("bob")).json()["links"] == []                                  # they hear about it


def test_a_shared_goal_adds_both_peoples_amounts_and_shares_notes(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    link(c)
    put_savings(dbs["alice"], 1000); put_savings(dbs["bob"], 600)
    g = c["alice"].post("/api/goals", headers=H("alice"), json={"name": "Trip", "target": 1000, "note": "my private worry"}).json()
    assert c["alice"].post(f"/api/goals/{g['id']}/share", headers=H("alice"), json={"peers": ["bob"]}).status_code == 200
    bg = goals(c, "bob")
    assert len(bg) == 1 and bg[0]["name"] == "Trip" and bg[0]["shared_state"] == "offered" and bg[0]["note"] == ""           # offered, and the private note did not travel
    assert c["bob"].post(f"/api/goals/{bg[0]['id']}/allocate", headers=H("bob"), json={"place": "", "amount": 100}).status_code in (409, 422)   # not before accepting
    assert c["bob"].post(f"/api/goals/{bg[0]['id']}/accept", headers=H("bob")).status_code == 200
    c["alice"].post(f"/api/goals/{g['id']}/allocate", headers=H("alice"), json={"place": "", "amount": 300})
    c["bob"].post(f"/api/goals/{bg[0]['id']}/allocate", headers=H("bob"), json={"place": "", "amount": 200})
    a, b = goals(c, "alice")[0], goals(c, "bob")[0]
    for v in (a, b):
        assert v["team_allocated"] == 500 and v["shortfall"] == 500 and v["pct"] == 0.5 and not v["funded"]
    assert a["allocated"] == 300 and b["allocated"] == 200                      # each side's own free money only moves by its own amount
    assert a["partners"] == [{"peer": "bob", "amount": 200}] and b["partners"] == [{"peer": "alice", "amount": 300}]
    # the free-money maths on each side still uses only that side's own earmarks
    assert c["alice"].get("/api/goals", headers=H("alice")).json()["free"] == 700 and c["bob"].get("/api/goals", headers=H("bob")).json()["free"] == 400
    # notes travel both ways, and only the author can remove one
    n = c["bob"].post(f"/api/goals/{bg[0]['id']}/notes", headers=H("bob"), json={"text": "Booked the hotel?"}).json()
    assert [x["text"] for x in goals(c, "alice")[0]["notes"]] == ["Booked the hotel?"] and goals(c, "alice")[0]["notes"][0]["author"] == "bob"
    assert c["alice"].delete(f"/api/goals/{g['id']}/notes/{n['id']}", headers=H("alice")).status_code == 422
    c["bob"].delete(f"/api/goals/{bg[0]['id']}/notes/{n['id']}", headers=H("bob"))
    assert goals(c, "alice")[0]["notes"] == []


def test_edits_use_the_newest_revision_and_closing_is_shared(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    link(c)
    g = c["alice"].post("/api/goals", headers=H("alice"), json={"name": "Car", "target": 9000}).json()
    c["alice"].post(f"/api/goals/{g['id']}/share", headers=H("alice"), json={"peers": ["bob"]})
    bid = goals(c, "bob")[0]["id"]; c["bob"].post(f"/api/goals/{bid}/accept", headers=H("bob"))
    c["bob"].put(f"/api/goals/{bid}", headers=H("bob"), json={"target": 8500, "deadline": "2027-06-01"})
    assert goals(c, "alice")[0]["target"] == 8500 and goals(c, "alice")[0]["deadline"] == "2027-06-01"
    c["alice"].put(f"/api/goals/{g['id']}", headers=H("alice"), json={"name": "Used car"})
    assert goals(c, "bob")[0]["name"] == "Used car" and goals(c, "bob")[0]["target"] == 8500
    c["alice"].post(f"/api/goals/{g['id']}/close", headers=H("alice"), json={"status": "done"})
    assert goals(c, "bob")[0]["status"] == "done"


def test_offline_partner_catches_up_later(monkeypatch):
    c, dbs, online = world(monkeypatch)
    link(c)
    g = c["alice"].post("/api/goals", headers=H("alice"), json={"name": "Roof", "target": 5000}).json()
    online.discard("bob")
    c["alice"].post(f"/api/goals/{g['id']}/share", headers=H("alice"), json={"peers": ["bob"]})
    assert c["alice"].get("/api/links", headers=H("alice")).json()["waiting"] >= 1 and goals(c, "bob") == []
    online.add("bob")
    assert c["alice"].post("/api/links/flush", headers=H("alice")).json()["waiting"] == 0
    assert goals(c, "bob")[0]["name"] == "Roof"


def test_nothing_crosses_without_a_link_and_the_sender_cannot_be_faked(monkeypatch):
    c, dbs, _ = world(monkeypatch, names=("alice", "bob", "cara"))
    link(c)                                                     # alice <-> bob only
    msg = {"shared_id": "x" * 16, "rev": 1, "edited_by": "cara", "members": ["cara", "bob"], "goal": {"name": "Sneaky", "target": 10}, "contrib": 0, "notes": []}
    # cara was never linked to bob: the gateway stamps her name, bob's goblin drops it
    assert c["cara"].post("/internal/link/inbox", headers=H("cara"), json={"type": "goal", "payload": msg, "sender": "x"}).status_code == 200
    r = links.receive(dbs["bob"], "bob", "cara", "goal", msg)
    assert r == {"ignored": "not linked"} and goals(c, "bob") == []
    # a payload that claims to come from someone else's membership is malformed for the real sender
    r = links.receive(dbs["bob"], "bob", "alice", "goal", {**msg, "members": ["cara", "bob"]})
    assert r == {"ignored": "malformed"}
    # direct calls to the inbox need the goblin's own secret
    assert c["bob"].post("/internal/link/inbox", json={"type": "invite"}).status_code == 401


def test_unlinking_keeps_the_goal_as_a_private_one(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    link(c)
    g = c["alice"].post("/api/goals", headers=H("alice"), json={"name": "Boat", "target": 4000}).json()
    c["alice"].post(f"/api/goals/{g['id']}/share", headers=H("alice"), json={"peers": ["bob"]})
    bid = goals(c, "bob")[0]["id"]; c["bob"].post(f"/api/goals/{bid}/accept", headers=H("bob"))
    c["bob"].post(f"/api/goals/{bid}/allocate", headers=H("bob"), json={"place": "", "amount": 0})
    c["alice"].delete("/api/links/bob", headers=H("alice"))
    for n, gid in (("alice", g["id"]), ("bob", bid)):
        v = next(x for x in goals(c, n) if x["id"] == gid)
        assert not v.get("shared_id") and v["name"] == "Boat" and v["team_allocated"] == v["allocated"]
    # and declining an offered goal removes it and tells the other side
    c2, dbs2, _ = world(monkeypatch)
    link(c2)
    g2 = c2["alice"].post("/api/goals", headers=H("alice"), json={"name": "Pool", "target": 100}).json()
    c2["alice"].post(f"/api/goals/{g2['id']}/share", headers=H("alice"), json={"peers": ["bob"]})
    bid2 = goals(c2, "bob")[0]["id"]
    c2["bob"].post(f"/api/goals/{bid2}/decline", headers=H("bob"))
    assert goals(c2, "bob") == [] and not goals(c2, "alice")[0].get("shared_id")


def test_joint_account_counts_only_my_share(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    for n in ("alice", "bob"):
        dbs[n].conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,subtype) VALUES('j','plaid','Axos','Joint Savings','cash',10000,'savings')"); dbs[n].conn.commit()
    c["alice"].put("/api/accounts/j", headers=H("alice"), json={"joint_with": "bob", "my_share": 60})
    c["bob"].put("/api/accounts/j", headers=H("bob"), json={"joint_with": "alice", "my_share": 40})
    assert c["alice"].get("/api/goals", headers=H("alice")).json()["total"] == 6000 and c["bob"].get("/api/goals", headers=H("bob")).json()["total"] == 4000
    assert c["alice"].get("/api/overview", headers=H("alice")).json()["net_worth"] == 6000
    assert c["alice"].put("/api/accounts/j", headers=H("alice"), json={"my_share": 150}).status_code == 422
    c["alice"].put("/api/accounts/j", headers=H("alice"), json={"my_share": -1})                              # cleared: the whole balance again
    assert c["alice"].get("/api/goals", headers=H("alice")).json()["total"] == 10000


def test_a_link_alone_shares_nothing_and_each_grant_is_a_saved_permission(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    for n in ("alice", "bob"):
        dbs[n].conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('s1','plaid','Axos','HYSA','cash',5000)")
        dbs[n].conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('c1','plaid','Huntington','Checking','cash',900)"); dbs[n].conn.commit()
    assert c["alice"].get("/api/links/people", headers=H("alice")).json() == {"people": ["bob"]}
    assert c["alice"].put("/api/links/bob/shares", headers=H("alice"), json={"ref": "s1", "on": True}).status_code == 422       # not linked yet
    link(c)
    assert c["bob"].get("/api/sharing", headers=H("bob")).json()[0]["from_them"] == []                                         # linked, still nothing visible
    assert c["alice"].put("/api/links/bob/shares", headers=H("alice"), json={"ref": "s1", "on": True}).status_code == 200
    seen = c["bob"].get("/api/sharing", headers=H("bob")).json()[0]["from_them"]
    assert [(x["name"], x["balance"]) for x in seen] == [("HYSA", 5000)]                                                       # only the account she chose
    dbs["alice"].conn.execute("UPDATE accounts SET balance=5200, my_share=50 WHERE id='s1'"); dbs["alice"].conn.commit()
    c["alice"].post("/api/links/flush", headers=H("alice"))                                                                    # the next round carries the new balance, my part only
    assert c["bob"].get("/api/sharing", headers=H("bob")).json()[0]["from_them"][0]["balance"] == 2600
    mine = c["alice"].get("/api/sharing", headers=H("alice")).json()[0]["accounts"]                                           # saved on her side
    assert {a["id"]: a["shared"] for a in mine} == {"s1": True, "c1": False}
    c["alice"].put("/api/links/bob/shares", headers=H("alice"), json={"ref": "s1", "on": False})
    assert c["bob"].get("/api/sharing", headers=H("bob")).json()[0]["from_them"] == []                                         # revoked
    c["alice"].put("/api/links/bob/shares", headers=H("alice"), json={"ref": "s1", "on": True})
    c["alice"].delete("/api/links/bob", headers=H("alice"))
    assert dbs["bob"].all("SELECT * FROM shared_in") == [] and dbs["alice"].all("SELECT * FROM link_shares") == []              # unlinking removes every permission


def test_requests_wait_until_answered(monkeypatch):
    c, dbs, _ = world(monkeypatch)
    c["alice"].post("/api/links", headers=H("alice"), json={"peer": "bob"})
    assert c["bob"].get("/api/requests", headers=H("bob")).json() == {"links": ["alice"], "goals": []}
    c["bob"].post("/api/links/alice/accept", headers=H("bob"))
    g = c["alice"].post("/api/goals", headers=H("alice"), json={"name": "Trip", "target": 500}).json()
    c["alice"].post(f"/api/goals/{g['id']}/share", headers=H("alice"), json={"peers": ["bob"]})
    r = c["bob"].get("/api/requests", headers=H("bob")).json()
    assert r["links"] == [] and [(x["name"], x["from"]) for x in r["goals"]] == [("Trip", ["alice"])]
    c["bob"].post(f"/api/goals/{r['goals'][0]['id']}/accept", headers=H("bob"))
    assert c["bob"].get("/api/requests", headers=H("bob")).json() == {"links": [], "goals": []}
