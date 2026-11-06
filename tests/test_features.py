import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient

from goblinbank import feed, gateway, goblin, importers, sync
from goblinbank.db import DB
from goblinbank.plaid import Plaid
from goblinbank.security import sha256


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode(),
                 "PLAID_CLIENT_ID": "cid", "PLAID_SECRET": "sec", "SHARE": "net_worth,subscriptions"}.items():
        monkeypatch.setenv(k, v)


H = {"X-Internal-Secret": "isec"}


def plaid_mock(calls=None):
    def h(r: httpx.Request):
        p, body = r.url.path, json.loads(r.content or b"{}")
        if calls is not None:
            calls.append((p, body))
        if p == "/link/token/create":
            return httpx.Response(200, json={"link_token": "link-1"})
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item-1", "access_token": "access-SECRET"})
        if p == "/accounts/get":
            return httpx.Response(200, json={"accounts": [
                {"account_id": "a1", "name": "Checking", "type": "depository", "balances": {"current": 1000.0, "iso_currency_code": "USD"}},
                {"account_id": "a2", "name": "Card", "type": "credit", "balances": {"current": 200.0}}]})
        if p == "/transactions/sync":
            return httpx.Response(200, json={"added": [
                {"transaction_id": "t1", "account_id": "a1", "date": "2026-11-01", "name": "NETFLIX", "merchant_name": "Netflix", "amount": 15.99},
                {"transaction_id": "t2", "account_id": "a1", "date": "2026-11-02", "name": "PAYROLL", "amount": -2000.0}],
                "modified": [], "removed": [], "next_cursor": "c1", "has_more": False})
        if p == "/transactions/recurring/get":
            return httpx.Response(200, json={"outflow_streams": [
                {"stream_id": "s1", "merchant_name": "Netflix", "description": "NETFLIX", "frequency": "MONTHLY", "is_active": True,
                 "last_amount": {"amount": 15.99}, "predicted_next_date": "2026-12-01", "last_date": "2026-11-01"}]})
        return httpx.Response(404, json={"error_code": "NOPE", "error_message": p})
    return httpx.MockTransport(h)


def mk(**kw):
    return goblin.create_app(db=kw.pop("db", DB(":memory:")), **kw)


def test_plaid_exchange_encrypts_token_and_syncs():
    db = DB(":memory:")
    c = TestClient(mk(db=db, plaid_transport=plaid_mock()))
    r = c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})
    assert r.status_code == 200 and r.json()["sync"]["added"] == 2
    stored = db.one("SELECT token_enc FROM plaid_items")["token_enc"]
    assert "access-SECRET" not in stored                       # encrypted at rest
    # signs flipped: spending negative, income positive
    amts = {t["id"]: t["amount"] for t in c.get("/api/transactions", headers=H).json()}
    assert amts == {"t1": -15.99, "t2": 2000.0}
    ov = c.get("/api/overview", headers=H).json()
    assert ov["net_worth"] == 800.0                            # 1000 cash - 200 card owed
    # recurring stream became a subscription
    subs = c.get("/api/subscriptions", headers=H).json()
    assert subs["items"][0]["name"] == "Netflix" and subs["items"][0]["source"] == "plaid"
    # second sync is idempotent (no duplicate events / subscriptions)
    c.post("/sync", headers=H)
    assert len(c.get("/api/subscriptions", headers=H).json()["items"]) == 1
    assert len([e for e in c.get("/api/events", headers=H).json() if e["kind"] == "transaction"]) == 2


def test_balance_change_emits_event():
    db = DB(":memory:")
    sync.record_balance(db, "a", "Savings", 100.0, "cash")
    sync.record_balance(db, "a", "Savings", 100.0, "cash")     # unchanged -> nothing
    sync.record_balance(db, "a", "Savings", 150.0, "cash")
    ev = db.all("SELECT * FROM events")
    assert len(ev) == 1 and ev[0]["amount"] == 50.0


def test_requires_internal_secret():
    c = TestClient(mk())
    for path in ("/api/accounts", "/api/subscriptions", "/feed.atom", "/api/overview"):
        assert c.get(path).status_code == 401


def test_subscriptions_crud_totals_ics_caldav(monkeypatch):
    monkeypatch.setenv("CALDAV_URL", "https://dav.example/cal")
    puts = []
    dav = httpx.MockTransport(lambda r: (puts.append((r.method, str(r.url), r.content)), httpx.Response(201))[1])
    c = TestClient(mk(caldav_transport=dav))
    mkb = lambda **o: {"name": "Gym", "amount": 120.0, "cycle": "yearly", "next_date": "2026-12-01", **o}
    s = c.post("/api/subscriptions", headers=H, json=mkb()).json()
    c.post("/api/subscriptions", headers=H, json=mkb(name="Spotify", amount=10.0, cycle="monthly"))
    t = c.get("/api/subscriptions", headers=H).json()
    assert t["monthly_total"] == 20.0 and t["yearly_total"] == 240.0
    assert c.post("/api/subscriptions", headers=H, json=mkb(cycle="daily")).status_code == 422
    assert c.post("/api/subscriptions", headers=H, json=mkb(next_date="soon")).status_code == 422
    assert c.put(f"/api/subscriptions/{s['id']}", headers=H, json=mkb(active=False)).json()["active"] == 0
    assert c.get("/api/subscriptions", headers=H).json()["monthly_total"] == 10.0   # paused excluded
    ics = c.get("/subscriptions.ics", headers=H).text
    assert "RRULE:FREQ=MONTHLY" in ics and "SUMMARY:$10.00 Spotify" in ics and "Gym" not in ics
    assert c.post("/api/subscriptions/export/caldav", headers=H).json() == {"pushed": 1, "failed": []}
    assert puts[0][0] == "PUT" and puts[0][1].startswith("https://dav.example/cal/goblin-sub-")
    assert c.delete(f"/api/subscriptions/{s['id']}", headers=H).status_code == 200
    assert c.put("/api/subscriptions/999", headers=H, json=mkb()).status_code == 404


def test_csv_import_paypal_venmo_cashapp():
    paypal = "Date,Time,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID\n11/03/2026,10:00,Etsy,Payment,Completed,USD,-25.00,0,-25.00,PP1\n11/04/2026,10:00,Bob,Payment,Pending,USD,5.00,0,5.00,PP2\n"
    venmo = "Account Statement,,,\nID,Datetime,Type,Status,Note,From,To,Amount (total)\n42,2026-11-05T12:00:00,Payment,Complete,Pizza,Me,Sam,- $12.50\n"
    cash = "Transaction ID,Date,Transaction Type,Currency,Amount,Fee,Net Amount,Asset Type,Account,Notes\nCA1,2026-11-06,Sent,USD,-$8.00,$0,-$8.00,Cash,Cash,Coffee\n"
    c = TestClient(mk())
    r = c.post("/import/csv", headers=H, content=paypal).json()
    assert r["provider"] == "paypal" and r["added"] == 1       # pending row skipped
    assert c.post("/import/csv", headers=H, content=paypal).json()["added"] == 0   # idempotent
    v = c.post("/import/csv?provider=venmo", headers=H, content=venmo).json()
    assert v["added"] == 1
    assert c.post("/import/csv", headers=H, content=cash).json()["provider"] == "cashapp"
    amts = sorted(t["amount"] for t in c.get("/api/transactions", headers=H).json())
    assert amts == [-25.0, -12.5, -8.0]
    assert c.post("/import/csv", headers=H, content="a,b\n1,2\n").status_code == 422


def test_atom_feed_is_valid_and_escaped():
    out = feed.atom([{"id": "1", "ts": "2026-11-01T00:00:00+00:00", "kind": "transaction", "title": "A & <B> -$5.00", "detail": "x"}], "t")
    from xml.etree import ElementTree as ET
    root = ET.fromstring(out)
    assert root.find("{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title").text == "A & <B> -$5.00"


def test_gateway_feed_token_and_webhook_relay(monkeypatch):
    db = DB(":memory:")
    g = mk(db=db, plaid_transport=plaid_mock())
    gtc = TestClient(g)
    gtc.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "X"})

    def to_goblin(req: httpx.Request):
        r = gtc.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec",
                        "user_token_sha256": sha256("tok"), "feed_token_sha256": sha256("feedtok")}], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin)))
    ok = gw.get("/feed/alice/feedtok/money.atom")
    assert ok.status_code == 200 and b"Netflix" in ok.content
    assert gw.get("/feed/alice/wrong/money.atom").status_code == 404
    assert gw.get("/feed/alice/tok/money.atom").status_code == 404   # login token is NOT the feed token
    assert b"BEGIN:VCALENDAR" in gw.get("/feed/alice/feedtok/subscriptions.ics").content
    # webhook: unknown item ignored; known item relayed but rejected without a valid signature
    assert gw.post("/webhooks/plaid", json={"item_id": "nope"}).json() == {"ignored": True}
    gw.post("/internal/items", json={"item_id": "item-1"}, headers={"X-Internal-Secret": "isec", "X-Goblin": "alice"})
    assert gw.post("/webhooks/plaid", json={"item_id": "item-1", "webhook_type": "TRANSACTIONS"}).status_code == 401


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_plaid_webhook_signature():
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "kid": "k1", "expired_at": None,
           "x": b64(nums.x.to_bytes(32, "big")), "y": b64(nums.y.to_bytes(32, "big"))}
    p = Plaid(httpx.MockTransport(lambda r: httpx.Response(200, json={"key": jwk})))
    p.client_id, p.secret = "c", "s"
    body = b'{"webhook_type":"TRANSACTIONS","item_id":"item-1"}'

    def token(iat=None, body_hash=None, alg="ES256"):
        h = b64(json.dumps({"alg": alg, "kid": "k1"}).encode())
        pl = b64(json.dumps({"iat": iat or int(time.time()), "request_body_sha256": body_hash or hashlib.sha256(body).hexdigest()}).encode())
        r, s = utils.decode_dss_signature(key.sign(f"{h}.{pl}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{h}.{pl}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    assert p.verify_webhook(body, token())
    assert not p.verify_webhook(b'{"tampered":1}', token())                 # body hash mismatch
    assert not p.verify_webhook(body, token(iat=int(time.time()) - 1000))   # stale
    assert not p.verify_webhook(body, token(alg="none"))
    assert not p.verify_webhook(body, None) and not p.verify_webhook(body, "a.b.c")


def test_link_token_and_unconfigured(monkeypatch):
    calls = []
    c = TestClient(mk(plaid_transport=plaid_mock(calls)))
    assert c.post("/plaid/link-token", headers=H).json() == {"link_token": "link-1"}
    assert calls[0][1]["products"] == ["transactions"]
    monkeypatch.delenv("PLAID_SECRET")
    assert TestClient(mk()).post("/plaid/link-token", headers=H).status_code == 502


def test_net_worth_history():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES('a','x','A','cash',0),('b','x','B','credit',0)")
        c.executemany("INSERT INTO snapshots VALUES(?,?,?)", [("2026-11-01T00:00:00", "a", 100.0), ("2026-11-01T01:00:00", "b", 30.0), ("2026-11-02T00:00:00", "a", 150.0)])
    out = TestClient(mk(db=db)).get("/api/history", headers=H).json()
    assert out == [{"date": "2026-11-01", "net_worth": 70.0}, {"date": "2026-11-02", "net_worth": 120.0}]
