import httpx
import pytest
from fastapi.testclient import TestClient

from goblinbank import gateway, goblin
from goblinbank.connectors.base import make_connector_app
from goblinbank.connectors.rh_agent import AgentConnector
from goblinbank.connectors.rh_csv import CsvConnector
from goblinbank.connectors.rh_live import LiveConnector
from goblinbank.security import sha256

CSV = """Activity Date,Process Date,Settle Date,Instrument,Description,Trans Code,Quantity,Price,Amount
1/2/2026,1/2/2026,1/4/2026,AAPL,Apple,Buy,10,$100.00,($1000.00)
1/3/2026,1/3/2026,1/5/2026,AAPL,Apple,Sell,4,$110.00,$440.00
1/1/2026,1/1/2026,1/1/2026,,ACH Deposit,ACH,,,"$2,000.00"
"""


@pytest.fixture
def csv_dir(tmp_path):
    (tmp_path / "a.csv").write_text(CSV)
    (tmp_path / "b_overlap.csv").write_text(CSV)  # re-export must not double count
    return tmp_path


def test_csv_positions_dedupe(csv_dir):
    c = CsvConnector(str(csv_dir))
    assert c.positions() == [{"symbol": "AAPL", "quantity": 6.0, "net_cost": 560.0}]
    assert len(c.history()) == 3
    assert c.balances()["net_transfers"] == 2000.0


def test_live_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RH_LIVE_ENABLED", raising=False)
    with pytest.raises(PermissionError):
        LiveConnector(client=object()).positions()


def test_live_rate_limited(monkeypatch):
    monkeypatch.setenv("RH_LIVE_ENABLED", "1")
    calls = []

    class Fake:
        def balances(self): calls.append(1); return {"equity": 1}
        def positions(self): return []
        def history(self): return []

    t = [0.0]
    c = LiveConnector(client=Fake(), min_interval=100, clock=lambda: t[0])
    c.balances(); t[0] = 50; c.balances()
    assert len(calls) == 1          # cached inside the window
    t[0] = 200; c.balances()
    assert len(calls) == 2


def test_agent_allowlist_fails_closed(monkeypatch):
    monkeypatch.setenv("RH_MCP_URL", "http://mcp")
    monkeypatch.setenv("RH_MCP_TOOL_BALANCES", "place_order")  # a trade tool
    monkeypatch.setenv("RH_MCP_READ_TOOLS", "get_balance")
    with pytest.raises(PermissionError):
        AgentConnector(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))).balances()


def test_agent_calls_allowed_tool(monkeypatch):
    monkeypatch.setenv("RH_MCP_URL", "http://mcp")
    monkeypatch.setenv("RH_MCP_TOOL_BALANCES", "get_balance")
    monkeypatch.setenv("RH_MCP_READ_TOOLS", "get_balance")
    seen = {}

    def h(r):
        seen["body"] = r.read()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"cash": 5}})

    assert AgentConnector(transport=httpx.MockTransport(h)).balances() == {"cash": 5}
    assert b"get_balance" in seen["body"]


def test_connector_service_requires_secret_and_is_get_only(csv_dir):
    c = TestClient(make_connector_app(CsvConnector(str(csv_dir)), "s3"))
    assert c.get("/positions").status_code == 401
    assert c.get("/positions", headers={"X-Internal-Secret": "s3"}).status_code == 200
    assert c.post("/positions", headers={"X-Internal-Secret": "s3"}).status_code == 405


def _bridge(app):
    tc = TestClient(app)

    def handler(req: httpx.Request):
        path = req.url.raw_path.decode()
        r = tc.request(req.method, path, content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    return httpx.MockTransport(handler)


def test_end_to_end(csv_dir, monkeypatch):
    conn = make_connector_app(CsvConnector(str(csv_dir)), "conn")
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "CONNECTOR_SECRET": "conn",
                 "SHARE": "robinhood_main", "RH_CSV_URL": "http://csv", "GATEWAY_URL": "http://gw"}.items():
        monkeypatch.setenv(k, v)
    g = goblin.create_app(transport=_bridge(conn))
    cfg = {"goblins": [
        {"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("alice-tok")},
        {"name": "bob", "url": "http://bob", "internal_secret": "x", "user_token_sha256": sha256("bob-tok")}],
        "orb_keys": [{"sha256": sha256("orbkey"), "scopes": ["summary:read"]}]}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(
        lambda r: _bridge(g).handle_request(r) if r.url.host == "alice" else httpx.Response(503))))

    A = {"Authorization": "Bearer alice-tok"}
    assert gw.get("/g/alice/robinhood/main", headers=A).json()["positions"][0]["symbol"] == "AAPL"
    # isolation: bob's token can't open alice's goblin; unknown goblin looks identical
    assert gw.get("/g/alice/robinhood/main", headers={"Authorization": "Bearer bob-tok"}).status_code == 401
    assert gw.get("/g/nobody/health", headers=A).status_code == 401
    # orb: needs the key; sees only shared data; an unreachable goblin degrades, not crashes
    assert gw.get("/api/v1/summary").status_code == 401
    out = gw.get("/api/v1/summary", headers={"Authorization": "Bearer orbkey"}).json()["goblins"]
    assert out["alice"]["robinhood_main"]["source"] == "rh-main-csv"
    assert "robinhood_agent" not in out["alice"]
    assert out["bob"] == {"error": "unavailable"}


def test_plaid_webhook_fails_closed(monkeypatch):
    monkeypatch.delenv("GATEWAY_INSECURE_WEBHOOKS", raising=False)
    gw = TestClient(gateway.create_app({"goblins": []}))
    assert gw.post("/webhooks/plaid", json={"item_id": "i"}).status_code == 501
