"""One bank pulled by hand (quick or full), the pull log, and what the admin page gets back."""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from plaidproxy import gateway, worker
from plaidproxy.db import DB
from plaidproxy.security import sha256


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


H = {"X-Internal-Secret": "isec"}


def rig(fail=None):
    calls = []

    def plaid(req: httpx.Request):
        p = req.url.path
        calls.append((p, req.read()))
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "tok"})
        if fail and fail["on"] and p == "/transactions/sync":
            return httpx.Response(400, json={"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "the login details of this item have changed"})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [{"transaction_id": "t1", "account_id": "a1", "amount": 4.5, "date": "2026-11-17", "name": "Coffee Shop"}], "modified": [], "removed": [],
                                             "next_cursor": "c1", "has_more": False, "accounts": [{"account_id": "a1", "name": "Checking", "type": "depository", "balances": {"current": 9876.54}}]})
        return httpx.Response(200, json={"outflow_streams": []})
    c = TestClient(worker.create_app(db=DB(":memory:"), plaid_transport=httpx.MockTransport(plaid), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})
    return c, calls


def test_quick_and_full_update_of_one_bank_and_the_log():
    c, calls = rig()
    r = c.post("/api/plaid/items/item-1/pull", headers=H).json()
    assert r["ok"] and r["added"] == 0 and r["modified"] == 1                     # already pulled at link time: updated, not doubled
    assert "/transactions/refresh" not in [p for p, _ in calls]
    n = len(calls)
    r = c.post("/api/plaid/items/item-1/pull?full=true", headers=H).json()
    later = [p for p, _ in calls[n:]]
    assert r["ok"] and later[0] == "/transactions/refresh" and "/transactions/sync" in later
    assert b'"cursor"' not in [b for p, b in calls[n:] if p == "/transactions/sync"][0]       # full: the whole history again
    log = c.get("/api/plaid/log", headers=H).json()["log"]
    assert [l["what"] for l in log[:2]] == ["full update", "quick update"] and all(l["ok"] for l in log[:2])
    assert log[0]["institution"] == "Huntington" and "1 changed" in log[0]["detail"]
    assert all("9876" not in str(l) and "Coffee" not in str(l) for l in log)       # counts only
    assert c.post("/api/plaid/items/nope/pull", headers=H).status_code == 404


def test_a_failed_pull_is_logged_and_marked_on_the_bank():
    fail = {"on": False}
    c, _ = rig(fail)
    fail["on"] = True
    r = c.post("/api/plaid/items/item-1/pull", headers=H).json()
    assert r["ok"] is False and r["error"].startswith("ITEM_LOGIN_REQUIRED")
    item = c.get("/api/status", headers=H).json()["plaid"]["items"][0]
    assert item["status"] == "error"
    l = c.get("/api/plaid/log", headers=H).json()["log"][0]
    assert l["ok"] is False and l["detail"].startswith("ITEM_LOGIN_REQUIRED")
    fail["on"] = False                                                              # signed in again (relink), then a pull clears it
    assert c.post("/api/plaid/items/item-1/pull", headers=H).json()["ok"]
    assert c.get("/api/status", headers=H).json()["plaid"]["items"][0]["status"] is None


def test_admin_can_pull_one_bank_and_read_the_log_and_sync_answers_counts_only():
    seen = []

    def w(req: httpx.Request):
        seen.append((req.method, req.url.path))
        if req.url.path == "/sync":
            return httpx.Response(200, json={"plaid": [{"item": "i", "added": 2, "modified": 0, "removed": 0}], "robinhood": {"rh:main": 43210.99}, "firefly": {"created": 2, "updated": 0, "deleted": 0, "errors": 0}})
        return httpx.Response(200, json={"ok": True, "log": []})
    cfg = {"people": [{"name": "alice", "url": "http://alice", "internal_secret": "s", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("admintok")}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(w)))
    A = {"Authorization": "Bearer admintok"}
    assert gw.post("/admin/p/alice/api/plaid/items/item-1/pull?full=true", headers=A, json={}).status_code == 200
    assert gw.get("/admin/p/alice/api/plaid/log", headers=A).status_code == 200
    s = gw.post("/admin/p/alice/sync", headers=A, json={}).json()
    assert "43210" not in str(s) and s["plaid"][0]["added"] == 2 and s["firefly"]["created"] == 2
    assert ("POST", "/api/plaid/items/item-1/pull") in seen


def test_a_person_sees_balance_differs_and_may_match_it_but_not_change_mappings_or_release():
    seen = []

    def w(req: httpx.Request):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"ok": True})
    cfg = {"people": [{"name": "alice", "url": "http://alice", "internal_secret": "s", "user_token_sha256": sha256("t")}], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(w)))
    U = {"Authorization": "Bearer t"}
    assert gw.post("/g/alice/api/firefly/accounts/plaid-chk/rebalance", headers=U, json={}).status_code == 200
    assert gw.post("/g/alice/api/plaid/items/item-1/pull", headers=U, json={}).status_code == 200
    assert gw.put("/g/alice/api/firefly/accounts/plaid-chk", headers=U, json={"mode": "create"}).status_code == 403
    assert gw.post("/g/alice/api/firefly/accounts/plaid-chk/release", headers=U, json={}).status_code == 403


def test_the_worker_says_which_accounts_differ_with_cards_and_loans_as_owed():
    from fake_firefly import FakeFirefly
    ff = FakeFirefly()
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, firefly_transport=ff.transport()))
    c.put("/api/settings", headers=H, json={"firefly_url": "https://firefly.example.com", "firefly_token": "good-token"})
    for aid, kind, bal in (("chk", "cash", 100.0), ("crd", "credit", 40.0)):
        db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,mask,currency) VALUES(?,?,?,?,?,?,?,?)", (aid, "plaid", "Bank", aid, kind, bal, "1", "USD"))
    db.conn.commit()
    for aid in ("chk", "crd"):
        c.put(f"/api/firefly/accounts/{aid}", headers=H, json={"mode": "create"})
    c.post("/api/firefly/sync", headers=H)
    view = {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}
    assert view["chk"]["drift"] is False and view["crd"]["drift"] is False             # the card is -40 in Firefly III: that matches
    db.conn.execute("UPDATE accounts SET balance=90 WHERE id='chk'"); db.conn.commit()
    assert {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}["chk"]["drift"] is True
    assert c.post("/api/firefly/accounts/chk/rebalance", headers=H).json()["changed"] is True
    assert {a["id"]: a for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}["chk"]["drift"] is False
