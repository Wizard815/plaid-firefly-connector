from datetime import date
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
    amts = {t["id"]: t["amount"] for t in c.get("/api/transactions?limit=500", headers=H).json()["items"]}
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
    for path in ("/api/accounts", "/api/subscriptions", "/subscriptions.ics", "/api/overview"):
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
    assert c.post("/api/subscriptions", headers=H, json=mkb(cycle="fortnightly")).status_code == 422
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
    amts = sorted(t["amount"] for t in c.get("/api/transactions?limit=500", headers=H).json()["items"])
    assert amts == [-25.0, -12.5, -8.0]
    assert c.post("/import/csv", headers=H, content="a,b\n1,2\n").status_code == 422


def test_gateway_webhook_relay(monkeypatch):
    db = DB(":memory:")
    g = mk(db=db, plaid_transport=plaid_mock())
    gtc = TestClient(g)
    gtc.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "X"})

    def to_goblin(req: httpx.Request):
        r = gtc.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec",
                        "user_token_sha256": sha256("tok")}], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin)))
    # webhook: unknown item ignored; known item relayed but rejected without a valid signature
    assert gw.post("/webhooks/plaid", json={"item_id": "nope"}).json() == {"ignored": True}
    gw.post("/internal/items", json={"item_id": "item-1"}, headers={"X-Internal-Secret": "isec", "X-Goblin": "alice"})
    assert gw.post("/webhooks/plaid", json={"item_id": "item-1", "webhook_type": "TRANSACTIONS"}).status_code == 401
    assert gw.get("/feed/alice/x/money.atom").status_code == 404 and gw.get("/g/alice/feed.atom", headers={"Authorization": "Bearer tok"}).status_code == 404


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


def test_gateway_proxies_json_put_delete_and_csv():
    gtc = TestClient(mk())

    def to_goblin(req: httpx.Request):
        r = gtc.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})

    cfg = {"goblins": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("tok")}], "orb_keys": []}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_goblin)))
    A = {"Authorization": "Bearer tok"}
    sub = {"name": "Gym", "amount": 10, "cycle": "monthly", "next_date": "2026-12-01"}
    s = gw.post("/g/alice/api/subscriptions", headers=A, json=sub)
    assert s.status_code == 200, s.text
    sid = s.json()["id"]
    assert gw.put(f"/g/alice/api/subscriptions/{sid}", headers=A, json={**sub, "amount": 12}).json()["amount"] == 12
    csv = "Transaction ID,Date,Transaction Type,Currency,Amount,Fee,Net Amount,Asset Type,Account,Notes\nCA1,2026-11-06,Sent,USD,-$8.00,$0,-$8.00,Cash,Cash,Coffee\n"
    assert gw.post("/g/alice/import/csv", headers={**A, "Content-Type": "text/csv"}, content=csv).json()["provider"] == "cashapp"
    assert gw.delete(f"/g/alice/api/subscriptions/{sid}", headers=A).status_code == 200


# ---------------- settings, status, csv upload, plaid holdings ----------------
def test_settings_secrets_never_returned_and_encrypted():
    db = DB(":memory:")
    c = TestClient(mk(db=db))
    r = c.put("/api/settings", headers=H, json={"caldav_url": "https://dav.example/cal", "caldav_user": "me", "caldav_password": "hunter2",
                                                 "share": ["net_worth", "robinhood_agent"], "rh_main_source": "csv"}).json()
    assert r["caldav_password"] == {"set": True} and "hunter2" not in str(r)
    assert "hunter2" not in db.one("SELECT value FROM settings WHERE key='caldav_password'")["value"]   # encrypted at rest
    assert r["share"] == ["net_worth", "robinhood_agent"] and r["rh_main_source"] == "csv"
    assert c.put("/api/settings", headers=H, json={"caldav_user": "you"}).json()["caldav_password"] == {"set": True}   # omitted = unchanged
    assert c.put("/api/settings", headers=H, json={"caldav_password": ""}).json()["caldav_password"] == {"set": False}  # "" clears
    # validation: nothing is written if any key is bad
    assert c.put("/api/settings", headers=H, json={"caldav_user": "x", "share": ["everything"]}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"rh_main_source": "magic"}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"bogus": 1}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"caldav_url": "ftp://x"}).status_code == 422
    assert c.get("/api/settings", headers=H).json()["caldav_user"] == "you"


def test_share_setting_controls_summary():
    c = TestClient(mk())
    assert "net_worth" in c.get("/summary", headers=H).json()         # env default from the fixture
    c.put("/api/settings", headers=H, json={"share": []})
    assert c.get("/summary", headers=H).json() == {"goblin": "alice"}


def test_bill_and_alert_lists_are_separate_opt_ins():
    c = TestClient(mk())
    c.post("/api/subscriptions", headers=H, json={"name": "Netflix", "amount": 15.49, "cycle": "monthly", "next_date": date.today().isoformat()})
    c.put("/api/settings", headers=H, json={"share": ["bills", "alerts"]})
    s = c.get("/summary", headers=H).json()
    assert "bills" in s and "alerts" in s and "bill_list" not in s and "alert_list" not in s      # totals don't share the lists
    c.put("/api/settings", headers=H, json={"share": ["bill_list", "alert_list"]})
    s = c.get("/summary", headers=H).json()
    assert s["bill_list"][0] == {"date": date.today().isoformat(), "name": "Netflix", "amount": 15.49, "category": "Subscriptions", "paid_by": "me"}
    assert any(a["kind"] == "due_soon" and "Netflix" in a["title"] for a in s["alert_list"])
    assert set(s["alert_list"][0]) - {"date"} == {"severity", "kind", "title", "detail"}          # "date" rides along on dated alerts (a bill due, a CD maturing)
    assert "bills" not in s and "net_worth" not in s


def test_caldav_from_settings_and_test_button():
    dav = httpx.MockTransport(lambda r: httpx.Response(207 if r.method == "PROPFIND" else 201))
    c = TestClient(mk(caldav_transport=dav))
    assert c.post("/api/settings/test-caldav", headers=H).status_code == 503
    c.put("/api/settings", headers=H, json={"caldav_url": "https://dav.example/cal", "caldav_user": "u", "caldav_password": "p"})
    assert c.post("/api/settings/test-caldav", headers=H).json() == {"ok": True, "status": 207}
    c.post("/api/subscriptions", headers=H, json={"name": "A", "amount": 1, "cycle": "monthly", "next_date": "2026-12-01"})
    assert c.post("/api/subscriptions/export/caldav", headers=H).json()["pushed"] == 1


def test_agent_config_endpoint_and_connector_fetches_it(monkeypatch):
    from goblinbank.connectors.rh_agent import AgentConnector
    monkeypatch.setenv("CONNECTOR_SECRET", "csec")
    c = TestClient(mk())
    assert c.get("/internal/connector-config/rh-agent", headers=H).status_code == 401       # internal secret is NOT enough
    c.put("/api/settings", headers=H, json={"rh_mcp_url": "http://mcp", "rh_mcp_token": "tok", "rh_mcp_read_tools": "get_bal",
                                             "rh_mcp_tool_balances": "get_bal"})
    cfg = c.get("/internal/connector-config/rh-agent", headers={"X-Internal-Secret": "csec"}).json()
    assert cfg["token"] == "tok" and cfg["read_tools"] == "get_bal"

    seen = []

    def t(req: httpx.Request):
        if req.url.host == "goblin":
            r = c.get(req.url.raw_path.decode(), headers=dict(req.headers))
            return httpx.Response(r.status_code, json=r.json())
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json={"result": {"cash": 1}})

    monkeypatch.delenv("RH_MCP_URL", raising=False)
    a = AgentConnector(transport=httpx.MockTransport(t), config_url="http://goblin/internal/connector-config/rh-agent", config_secret="csec")
    assert a.balances() == {"cash": 1} and seen == ["Bearer tok"]
    with pytest.raises(PermissionError):
        a.positions()          # no positions tool configured -> refuses


def test_csv_upload_validates_and_connector_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_CSV_DIR", str(tmp_path))
    c = TestClient(mk())
    good = "Activity Date,Instrument,Description,Trans Code,Quantity,Price,Amount\n1/2/2026,AAPL,Apple,Buy,2,$10.00,($20.00)\n"
    r = c.post("/robinhood/csv-upload", headers=H, content=good)
    assert r.status_code == 200 and (tmp_path / r.json()["saved"]).exists()
    assert c.post("/robinhood/csv-upload", headers=H, content="a,b\n1,2\n").status_code == 422
    from goblinbank.connectors.rh_csv import CsvConnector
    assert CsvConnector(str(tmp_path)).positions()[0]["symbol"] == "AAPL"
    assert c.get("/api/status", headers=H).json()["robinhood"]["csv_files"] == [r.json()["saved"]]


def plaid_robinhood_mock():
    base = plaid_mock()

    def h(r: httpx.Request):
        p = r.url.path
        if p == "/accounts/get":
            return httpx.Response(200, json={"accounts": [{"account_id": "rh1", "name": "Individual", "type": "investment", "balances": {"current": 5000.0}}]})
        if p == "/investments/holdings/get":
            return httpx.Response(200, json={"securities": [{"security_id": "s1", "ticker_symbol": "VTI", "name": "Vanguard"}],
                                             "holdings": [{"account_id": "rh1", "security_id": "s1", "quantity": 10, "institution_price": 250.0, "institution_value": 2500.0}]})
        return base.handle_request(r)
    return httpx.MockTransport(h)


def test_plaid_robinhood_holdings_feed_main_portfolio():
    db = DB(":memory:")
    c = TestClient(mk(db=db, plaid_transport=plaid_robinhood_mock()))
    r = c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Robinhood"}).json()
    assert r["sync"]["holdings"] == 1
    m = c.get("/robinhood/main", headers=H).json()
    assert m["source"] == "plaid" and m["balances"]["equity"] == 5000.0 and m["positions"][0]["symbol"] == "VTI"
    c.put("/api/settings", headers=H, json={"rh_main_source": "csv"})                 # force another source
    assert "error" in c.get("/robinhood/main", headers=H).json()


def test_plaid_item_removal_deletes_data():
    db = DB(":memory:")
    c = TestClient(mk(db=db, plaid_transport=plaid_mock()))
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})
    assert c.get("/api/plaid/items", headers=H).json() == [{"item_id": "item-1", "institution": "Huntington"}]
    assert c.delete("/api/plaid/items/item-1", headers=H).json()["accounts"] == 2
    assert c.get("/api/accounts", headers=H).json() == [] and c.get("/api/transactions?limit=500", headers=H).json()["items"] == []
    assert c.delete("/api/plaid/items/item-1", headers=H).status_code == 404


def test_plaid_usage_counts_cap_and_cost(monkeypatch):
    db = DB(":memory:")
    c = TestClient(mk(db=db, plaid_transport=plaid_mock()))
    c.post("/plaid/exchange", headers=H, json={"public_token": "p", "institution": "Huntington"})
    u = c.get("/api/plaid/usage", headers=H).json()
    assert u["calls_month"] >= 4 and u["by_endpoint"]["/accounts/get"] == 1 and u["est_monthly_cost"] is None   # no rate entered -> no guess
    c.put("/api/settings", headers=H, json={"plaid_rate_item_month": "1.50", "plaid_budget_month": "1"})
    u = c.get("/api/plaid/usage", headers=H).json()
    assert u["est_monthly_cost"] == 1.5 and u["over_budget"] is True
    assert c.put("/api/settings", headers=H, json={"plaid_rate_item_month": "free"}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"plaid_daily_cap": "2.5"}).status_code == 422
    # daily cap blocks further calls
    c.put("/api/settings", headers=H, json={"plaid_daily_cap": "3"})
    r = c.post("/plaid/link-token", headers=H)
    assert r.status_code == 502 and "cap" in r.json()["detail"]


def test_drop_folder_import_moves_files(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_DIR", str(tmp_path))
    cash = "Transaction ID,Date,Transaction Type,Currency,Amount,Fee,Net Amount,Asset Type,Account,Notes\nCA1,2026-11-06,Sent,USD,-$8.00,$0,-$8.00,Cash,Cash,Coffee\n"
    (tmp_path / "cashapp_oct.csv").write_text(cash)
    (tmp_path / "junk.csv").write_text("a,b\n1,2\n")
    (tmp_path / "notes.txt").write_text("ignore me")
    c = TestClient(mk())
    r = c.post("/import/scan", headers=H).json()["imports"]
    assert {"file": "cashapp_oct.csv", "provider": "cashapp", "rows": 1, "added": 1} in r
    assert any(x["file"] == "junk.csv" and "error" in x for x in r)
    assert not (tmp_path / "cashapp_oct.csv").exists() and (tmp_path / "junk.csv").exists() and (tmp_path / "notes.txt").exists()
    assert c.post("/import/scan", headers=H).json()["imports"][0]["file"] == "junk.csv"      # nothing re-imported
    st = c.get("/api/status", headers=H).json()["imports"]
    assert st == [{"provider": "cashapp", "transactions": 1, "last_transaction": "2026-11-06"}]
