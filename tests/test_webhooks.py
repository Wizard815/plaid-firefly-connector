"""Plaid webhooks: Plaid tells us when there is news, so banks are pulled when something happened and polled only as a weekly safety net."""
import base64
import hashlib
import json
import time
from collections import Counter

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient

from plaidproxy import gateway, worker, ledger, plaidplan, sync
from plaidproxy.db import DB
from plaidproxy.security import sha256

H = {"X-Internal-Secret": "isec"}
KEY = ec.generate_private_key(ec.SECP256R1())
NUMS = KEY.public_key().public_numbers()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
JWK = {"kty": "EC", "crv": "P-256", "kid": "k1", "expired_at": None, "x": b64(NUMS.x.to_bytes(32, "big")), "y": b64(NUMS.y.to_bytes(32, "big"))}


def signed(body: bytes) -> str:
    h = b64(json.dumps({"alg": "ES256", "kid": "k1"}).encode())
    p = b64(json.dumps({"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()}).encode())
    r, s = utils.decode_dss_signature(KEY.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{h}.{p}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(), "PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


def rig():
    n = Counter()
    posted = []

    def h(req: httpx.Request):
        p = req.url.path
        n[p] += 1
        body = json.loads(req.content) if req.content else {}
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "tok"})
        if p == "/webhook_verification_key/get":
            return httpx.Response(200, json={"key": JWK})
        if p == "/transactions/sync":       # balances come with the sync: no separate accounts call
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False,
                                             "accounts": [{"account_id": "x", "name": "Checking", "type": "depository", "subtype": "checking", "balances": {"current": 77.0}}]})
        if p == "/transactions/recurring/get":
            return httpx.Response(200, json={"outflow_streams": []})
        if p == "/item/webhook/update":
            posted.append(body["webhook"])
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error_code": "X", "error_message": p})
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, plaid_transport=httpx.MockTransport(h), inline_webhooks=True))
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})
    return c, db, n, posted


def hook(c, body: dict, good=True):
    raw = json.dumps(body).encode()
    return c.post("/plaid/webhook", headers={**H, "Plaid-Verification": signed(raw) if good else "x.y.z"}, content=raw)


def test_balances_come_with_the_sync_so_a_pull_is_one_call():
    c, db, n, _ = rig()
    assert n["/accounts/get"] == 0 and n["/transactions/sync"] == 1                       # linking a bank needed no accounts call
    assert db.one("SELECT balance FROM accounts WHERE id='x'")["balance"] == 77.0


def test_a_transactions_webhook_pulls_that_bank_and_makes_it_live():
    c, db, n, _ = rig()
    assert hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"}, good=False).status_code == 401      # unsigned: refused
    assert n["/transactions/sync"] == 1
    db.conn.execute("UPDATE plaid_items SET last_sync=last_sync-120"); db.conn.commit()
    r = hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"})
    assert r.status_code == 200 and "synced" in r.json() and n["/transactions/sync"] == 2
    assert "synced" in hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"}).json()       # every webhook is honoured: one that arrives right after a pull may carry newer news
    row = db.one("SELECT * FROM plaid_items")
    assert plaidplan.hook_live(row, time.time())
    assert plaidplan.interval(db, row, 0) == plaidplan.SAFETY_NET                          # Automatic + live webhooks: weekly safety net only
    assert plaidplan.interval(db, {**row, "schedule": "daily"}, 0) == 86400                # a schedule you chose is still yours
    assert hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "nope"}).json() == {"ignored": "unknown item"}


def test_a_live_bank_is_not_polled_daily_and_recurring_arrives_by_webhook(monkeypatch):
    now = [5_000_000.0]
    monkeypatch.setattr(sync, "clock", lambda: now[0])
    c, db, n, _ = rig()
    db.conn.execute("UPDATE plaid_items SET last_sync=?, last_hook=?, last_slow=?", (now[0], now[0], now[0])); db.conn.commit()
    before = n["/transactions/sync"]
    for _ in range(96 * 6):                                                              # six days of the 15-minute timer: nothing is due
        now[0] += 900
        db.conn.execute("UPDATE plaid_items SET last_hook=?", (now[0],)); db.conn.commit()
        sync.sync_all(db, worker_plaid(n))
    assert n["/transactions/sync"] == before and n["/transactions/recurring/get"] == 1          # no polling, and no recurring call beyond the one made when the bank was linked
    r = hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "RECURRING_TRANSACTIONS_UPDATE", "item_id": "item-1"})
    assert r.json() == {"recurring": "updated"}


def worker_plaid(n):
    from plaidproxy.plaid import Plaid
    p = Plaid(httpx.MockTransport(lambda r: (n.update([r.url.path]), httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False, "accounts": [], "outflow_streams": []}))[1]))
    p.client_id, p.secret = "c", "s"
    return p


def test_a_login_problem_becomes_an_alert_and_clears_when_repaired():
    c, db, n, _ = rig()
    hook(c, {"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "item-1", "error": {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "the login details changed"}})
    al = [a for a in c.get("/api/alerts", headers=H).json() if a["kind"] == "bank_login"]
    assert len(al) == 1 and al[0]["severity"] == "critical" and "Huntington" in al[0]["title"] and "Relink" in al[0]["detail"]
    assert c.get("/api/plaid/items", headers=H).json()[0]["status"] == "error"
    hook(c, {"webhook_type": "ITEM", "webhook_code": "LOGIN_REPAIRED", "item_id": "item-1"})
    assert not [a for a in c.get("/api/alerts", headers=H).json() if a["kind"] == "bank_login"]
    hook(c, {"webhook_type": "ITEM", "webhook_code": "PENDING_EXPIRATION", "item_id": "item-1"})
    assert [a["severity"] for a in c.get("/api/alerts", headers=H).json() if a["kind"] == "bank_login"] == ["warning"]


def test_one_bank_failing_does_not_stop_the_others(monkeypatch):
    c, db, n, _ = rig()
    from plaidproxy.plaid import PlaidError

    class Boom:
        def accounts(self, t): raise PlaidError("ITEM_LOGIN_REQUIRED: the login details changed")
        def sync(self, t, c): raise PlaidError("ITEM_LOGIN_REQUIRED: the login details changed")
    db.conn.execute("INSERT INTO plaid_items(item_id,institution,token_enc) VALUES('item-2','Other',?)", (db.one("SELECT token_enc FROM plaid_items")["token_enc"],)); db.conn.commit()
    out = sync.sync_all(db, Boom(), force=True)
    assert len(out) == 2 and all("error" in o for o in out)
    assert db.one("SELECT status FROM plaid_items WHERE item_id='item-2'")["status"] == "error"
    # but our own cap is not a bank problem: no alert, and the round stops
    from plaidproxy.plaid import PlaidLimit
    db.conn.execute("UPDATE plaid_items SET status=NULL"); db.conn.commit()

    class Capped:
        def accounts(self, t): raise PlaidLimit("daily Plaid call cap reached")
        def sync(self, t, c): raise PlaidLimit("daily Plaid call cap reached")
    with pytest.raises(PlaidLimit):
        sync.sync_all(db, Capped(), force=True)
    assert db.one("SELECT COUNT(*) n FROM plaid_items WHERE status IS NOT NULL")["n"] == 0


def test_the_gateway_admin_public_address_sets_the_webhook_and_banks_learn_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    holder = {}

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    n = Counter(); posted = []

    def h(req: httpx.Request):
        p = req.url.path
        body = json.loads(req.content) if req.content else {}
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "tok"})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False, "accounts": []})
        if p == "/accounts/get":
            return httpx.Response(200, json={"accounts": [{"account_id": "x", "name": "C", "type": "depository", "balances": {"current": 1}}]})
        if p == "/transactions/recurring/get":
            return httpx.Response(200, json={"outflow_streams": []})
        if p == "/link/token/create":
            posted.append(("link", body.get("webhook"), body.get("redirect_uri")))
            return httpx.Response(200, json={"link_token": "l"})
        if p == "/item/webhook/update":
            posted.append(("update", body["webhook"]))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error_code": "X", "error_message": p})
    db = DB(":memory:")
    g = worker.create_app(db=db, plaid_transport=httpx.MockTransport(h), transport=httpx.MockTransport(to_gateway))
    tc = TestClient(g)

    def to_worker(req: httpx.Request):
        r = tc.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"people": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_worker)))
    holder["gw"] = gw
    A = {"Authorization": "Bearer adm"}
    tc.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})        # linked before any address existed
    assert gw.put("/admin/policy", headers=A, json={"default": {"public_url": "http://insecure.example"}}).status_code == 422
    assert gw.put("/admin/policy", headers=A, json={"default": {"public_url": "https://bank.example.com/"}}).status_code == 200
    assert tc.get("/api/settings", headers=H).status_code == 200                                         # opening Settings picks the policy up
    assert ("update", "https://bank.example.com/webhooks/plaid") in posted                                # the existing bank is told once
    posted.clear(); tc.get("/api/settings", headers=H)
    assert ("update", "https://bank.example.com/webhooks/plaid") not in posted                            # and not again
    tc.post("/plaid/link-token", headers=H)
    assert posted[-1] == ("link", "https://bank.example.com/webhooks/plaid", "https://bank.example.com/link")   # new links carry the webhook and the OAuth return address
    # the gateway forgets which bank is whose when it restarts; the worker tells it again
    gw2 = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_worker)))
    holder["gw"] = gw2
    assert gw2.post("/webhooks/plaid", json={"item_id": "item-1"}).json() == {"ignored": True}
    tc.post("/sync", headers=H)                                                               # any tick of the worker's background round re-registers its banks
    assert gw2.post("/webhooks/plaid", json={"item_id": "item-1", "webhook_type": "TRANSACTIONS"}).json() != {"ignored": True}     # now it finds its way home (and is refused there: no signature)


def test_the_webhook_is_answered_at_once_and_the_pull_happens_in_the_background():
    n = Counter()

    def h(req: httpx.Request):
        p = req.url.path
        n[p] += 1
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "tok"})
        if p == "/webhook_verification_key/get":
            return httpx.Response(200, json={"key": JWK})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False, "accounts": [{"account_id": "x", "name": "C", "type": "depository", "balances": {"current": 1.0}}]})
        return httpx.Response(200, json={"outflow_streams": []})
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, plaid_transport=httpx.MockTransport(h)))              # the default: not inline
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "B"})
    before = n["/transactions/sync"]
    r = hook(c, {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"})
    assert r.status_code == 200 and r.json() == {"accepted": True}
    for _ in range(50):
        if n["/transactions/sync"] > before:
            break
        time.sleep(0.05)
    assert n["/transactions/sync"] == before + 1


def test_the_plaid_api_version_is_pinned(monkeypatch):
    from plaidproxy.plaid import Plaid
    seen = []
    monkeypatch.setenv("PLAID_CLIENT_ID", "c"); monkeypatch.setenv("PLAID_SECRET", "s"); monkeypatch.delenv("PLAID_VERSION", raising=False)
    handler = lambda r: (seen.append(r.headers.get("plaid-version")), httpx.Response(200, json={}))[1]
    Plaid(httpx.MockTransport(handler)).post("/x", {})
    monkeypatch.setenv("PLAID_AUTH", "header")
    Plaid(httpx.MockTransport(handler)).post("/x", {})
    monkeypatch.setenv("PLAID_VERSION", "2020-09-14")
    assert seen == ["2020-09-14", "2020-09-14"]


def test_the_sandbox_test_webhook_button_reports_whether_it_arrived(monkeypatch):
    import threading
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    holder = {}

    def h(req: httpx.Request):
        p = req.url.path
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "tok"})
        if p == "/webhook_verification_key/get":
            return httpx.Response(200, json={"key": JWK})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [], "next_cursor": "c", "has_more": False, "accounts": [{"account_id": "x", "name": "C", "type": "depository", "balances": {"current": 1.0}}]})
        if p == "/sandbox/item/fire_webhook":
            if holder.get("deliver"):                                   # Plaid, a moment later, calls the public address
                threading.Timer(0.2, lambda: hook(holder["c"], {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"})).start()
            return httpx.Response(200, json={"webhook_fired": True})
        if p == "/item/webhook/update":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"outflow_streams": []})
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, plaid_transport=httpx.MockTransport(h), inline_webhooks=True))
    holder["c"] = c
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "B"})
    assert c.post("/plaid/test-webhook", headers=H).status_code == 409                     # no public address yet
    c2 = TestClient(worker.create_app(db=DB(":memory:"), plaid_transport=httpx.MockTransport(h), inline_webhooks=True))
    holder["c"] = c2
    c2.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "B"})
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://hooks.example.com/webhooks/plaid")
    c3 = TestClient(worker.create_app(db=DB(":memory:"), plaid_transport=httpx.MockTransport(h), inline_webhooks=True))
    holder["c"] = c3
    c3.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "B"})
    holder["deliver"] = True
    r = c3.post("/plaid/test-webhook?wait=5", headers=H).json()
    assert r["fired"] is True and r["arrived"] is True
    holder["deliver"] = False
    assert c3.post("/plaid/test-webhook?wait=1", headers=H).json()["arrived"] is False             # Plaid fired it but nothing reached us: the address is not public
    monkeypatch.setenv("PLAID_ENV", "production"); monkeypatch.setenv("PLAID_BASE_URL", "")
    c4 = TestClient(worker.create_app(db=DB(":memory:"), plaid_transport=httpx.MockTransport(h), inline_webhooks=True))
    c4.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "B"})
    assert c4.post("/plaid/test-webhook", headers=H).status_code == 409                     # production: a real update is the test
