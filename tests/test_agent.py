import json
import random
from datetime import date, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.fake_llm import anthropic_server, openai_server
from devtools.seed import seed
from goblinbank import agent, goblin, ledger
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def make(seed_no=3, llm=None, caldav=None):
    db = DB(":memory:")
    truth = seed(db, seed_no, TODAY)
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=llm, caldav_transport=caldav))
    return c, db, truth


def add_provider(c, **kw):
    body = {"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True, **kw}
    r = c.post("/api/agent/providers", headers=H, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def ask(c, q, **kw):
    return c.post("/api/agent/chat", headers=H, json={"messages": [{"role": "user", "content": q}], **kw})


def results(reply: str) -> list:
    return [json.loads(x) for x in reply.removeprefix("RESULTS:").split("||") if x.startswith(("{", "["))]


# ---------------------------------------------------------------- the agent sees everything, and the numbers are right
@pytest.mark.parametrize("seed_no", [1, 2, 3, 4, 5])
def test_every_tool_runs_and_matches_the_database(seed_no):
    log = []
    c, db, truth = make(seed_no, llm=None)
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('Netflix',15.49,'monthly',1,'month','2026-11-20','chk','x')")
    db.conn.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES('CD',  'cd', 8000, 5, '2027-01-01')")
    db.conn.execute("INSERT INTO budgets VALUES('Food', 250)")
    db.conn.commit()
    c.app.router  # noqa
    script = [[("get_overview", {"days": 14}), ("list_accounts", {}), ("search_transactions", {"q": "netflix", "direction": "out", "limit": 5}),
               ("spending_by_category", {"start": "2026-10-01", "end": "2026-10-31"}), ("list_subscriptions", {}), ("upcoming_bills", {"days": 45}),
               ("find_recurring_payments", {}), ("savings_summary", {}), ("get_alerts", {}), ("project_balance", {"days": 30, "account_id": "chk"}),
               ("budget_status", {"month": "2026-10"}), ("list_notes", {}), ("calculate", {"expression": "1200 / 4 + 15.49 * 3"}),
               ("write_note", {"title": "Plan", "body": "Save $200 a month."}), ("propose_subscription", {"name": "Farm Box", "amount": 32, "every": 1, "unit": "week", "reason": "seen weekly"})]]
    c2 = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server(script, log)))
    add_provider(c2)
    r = ask(c2, "show me everything")
    assert r.status_code == 200, r.text
    out = r.json()
    assert [t["tool"] for t in out["trace"]] == [s[0] for s in script[0]] and all(t["ok"] for t in out["trace"]), out["trace"]
    got = {t["tool"]: json.loads(m["content"]) for t, m in zip(out["trace"], [m for m in log[1]["body"]["messages"] if m["role"] == "tool"])}
    # independent oracle: raw SQL / arithmetic, not the ledger functions
    assert got["list_accounts"] and {a["id"] for a in got["list_accounts"]} == {r["id"] for r in db.all("SELECT id FROM accounts")}
    n = db.one("SELECT COUNT(*) n, ROUND(SUM(amount),2) s FROM transactions WHERE lower(name) LIKE '%netflix%' AND amount<0")
    assert got["search_transactions"]["total"] == n["n"] and got["search_transactions"]["sum_out"] == n["s"] and len(got["search_transactions"]["items"]) == min(5, n["n"])
    sp = db.one("SELECT ROUND(-SUM(t.amount),2) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount<0 AND c.kind='expense' AND t.date BETWEEN '2026-10-01' AND '2026-10-31'")["s"] or 0
    assert got["spending_by_category"]["total_spent"] == sp
    assert got["calculate"]["result"] == 1200 / 4 + 15.49 * 3
    assert got["get_overview"]["net_worth"] == ledger.net_worth(db) == round(sum((a["balance"] or 0) * (-1 if a["kind"] == "credit" else 1) for a in db.all("SELECT * FROM accounts")) + 8000, 2)
    assert got["upcoming_bills"]["days"] == 45 and any(i["name"] == "Netflix" for i in got["upcoming_bills"]["items"])
    assert got["savings_summary"]["total_savings"] == 8000 and got["savings_summary"]["places"][0]["days_to_maturity"] == (date(2027, 1, 1) - TODAY).days and got["savings_summary"]["free_savings"] == 8000
    assert got["budget_status"]["items"][0]["category"] == "Food"
    assert any("farm box" in s["name"].lower() for s in got["find_recurring_payments"]) and got["project_balance"]["days"] == 30
    assert got["write_note"]["saved"] and db.one("SELECT author FROM notes")["author"] == "agent"
    assert got["propose_subscription"]["proposed"] and db.one("SELECT COUNT(*) n FROM subscriptions WHERE name='Farm Box'")["n"] == 0     # a proposal, not a subscription
    assert any(s["source"] == "agent" for s in c2.get("/api/suggestions", headers=H).json())
    assert out["reply"].startswith("RESULTS:") and out["provider"]["local"] is True


def test_tool_list_covers_every_data_area():
    c, _, _ = make()
    names = {t["name"] for t in c.get("/api/agent/tools", headers=H).json()}
    assert names >= {"list_goals", "can_i_afford", "monthly_plan", "add_goal", "fund_goal", "add_subscription", "get_overview", "list_accounts", "search_transactions", "spending_by_category", "list_subscriptions", "upcoming_bills", "find_recurring_payments",
                     "savings_summary", "get_alerts", "project_balance", "budget_status", "calendar_events", "list_notes", "read_note", "write_note", "propose_subscription", "calculate"}
    for t in c.get("/api/agent/tools", headers=H).json():
        assert t["parameters"]["type"] == "object" and t["description"]


def test_agent_reads_calendar_and_notes():
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART;VALUE=DATE:20261112\r\nSUMMARY:Concert tickets $85.50\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART;VALUE=DATE:20261115\r\nSUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR"
    xml = f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop><c:calendar-data><![CDATA[{ics}]]></c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>'
    dav = httpx.MockTransport(lambda r: httpx.Response(207, content=xml.encode()) if r.method == "REPORT" else httpx.Response(404))
    log = []
    c, db, _ = make(llm=None, caldav=dav)
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("calendar_events", {"days_ahead": 30}), ("list_notes", {})]], log), caldav_transport=dav))
    c.put("/api/settings", headers=H, json={"caldav_url": "https://dav/cal", "caldav_user": "u", "caldav_password": "p"})
    db.conn.execute("INSERT INTO notes(title,body,author,created,updated) VALUES('Goals','x','me','a','b')"); db.conn.commit()
    add_provider(c)
    tools = [json.loads(m["content"]) for m in log and [] or []]
    out = ask(c, "what is on my calendar?").json()
    cal, notes = results(out["reply"])
    assert [(e["date"], e["amount"]) for e in cal["events"]] == [("2026-11-12", 85.5), ("2026-11-15", None)]
    assert notes[0]["title"] == "Goals"


def test_calendar_tool_without_caldav_says_so():
    log = []
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("calendar_events", {})]], log)))
    add_provider(c)
    assert "not set up" in results(ask(c, "calendar?").json()["reply"])[0]["error"]


# ---------------------------------------------------------------- formats: OpenAI-style and Anthropic
def test_anthropic_format_roundtrip():
    log = []
    c, db, _ = make(llm=anthropic_server([[("calculate", {"expression": "2+2"}), ("list_accounts", {})]], log))
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=anthropic_server([[("calculate", {"expression": "2+2"}), ("list_accounts", {})]], log)))
    p = add_provider(c, kind="anthropic", base_url="https://api.anthropic.com", model="claude-x", api_key="sk-ant-secret", allow_cloud=True)
    assert p["has_key"] and "sk-ant-secret" not in json.dumps(p) and p["local"] is False
    out = ask(c, "2+2?").json()
    first, second = log[0], log[1]
    assert first["key"] == "sk-ant-secret" and first["version"] and first["url"].endswith("/v1/messages")
    assert first["body"]["system"].startswith("You are Goblin") and {t["name"] for t in first["body"]["tools"]} >= {"calculate", "list_accounts"}
    last = second["body"]["messages"][-1]
    assert last["role"] == "user" and [b["type"] for b in last["content"]] == ["tool_result", "tool_result"]      # both results in ONE user turn
    assert second["body"]["messages"][-2]["content"][-1]["type"] == "tool_use"
    assert results(out["reply"])[0]["result"] == 4


def test_openai_format_and_key_header():
    log = []
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("calculate", {"expression": "10/4"})]], log)))
    add_provider(c, base_url="http://192.168.1.50:1234/v1", api_key="lm-key")
    ask(c, "hi")
    assert log[0]["auth"] == "Bearer lm-key" and log[0]["url"] == "http://192.168.1.50:1234/v1/chat/completions"
    assert log[0]["body"]["messages"][0]["role"] == "system" and log[0]["body"]["tools"][0]["type"] == "function"
    tm = [m for m in log[1]["body"]["messages"] if m["role"] == "tool"][0]
    assert tm["tool_call_id"] == "call_0_0" and json.loads(tm["content"])["result"] == 2.5


def test_context_mode_for_models_without_tool_calling():
    log = []
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("calculate", {})]], log)))
    add_provider(c, tools=False)
    out = ask(c, "how am I doing?").json()
    assert "tools" not in log[0]["body"] and len(log) == 1 and out["trace"] == []
    sysmsg = log[0]["body"]["messages"][0]["content"]
    assert "snapshot" in sysmsg and "net_worth" in sysmsg and "Checking" in sysmsg and "Netflix" in sysmsg.replace("NETFLIX", "Netflix") or "list_subscriptions" in sysmsg


# ---------------------------------------------------------------- privacy guard and providers
def test_cloud_provider_blocked_until_allowed():
    log = []
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([], log)))
    p = add_provider(c, name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="or-key")
    r = ask(c, "net worth?")
    assert r.status_code == 403 and "openrouter.ai" in r.json()["detail"] and log == []           # nothing left the machine
    c.put(f"/api/agent/providers/{p['id']}", headers=H, json={"name": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "m", "allow_cloud": True})
    assert ask(c, "net worth?").status_code == 200 and len(log) == 1
    assert c.get("/api/agent/providers", headers=H).json()["items"][0]["has_key"] is True            # key kept when omitted on update


@pytest.mark.parametrize("url,local", [("http://localhost:11434/v1", True), ("http://host.docker.internal:11434/v1", True), ("http://ollama:11434/v1", True), ("http://10.0.0.5:8000/v1", True),
                                       ("http://192.168.0.2/v1", True), ("http://nas.local:1234/v1", True), ("https://api.openai.com/v1", False), ("https://openrouter.ai/api/v1", False),
                                       ("http://8.8.8.8/v1", False), ("https://localhost.evil.com/v1", False)])
def test_is_local(url, local):
    assert agent.is_local(url) is local


def test_provider_validation_and_secret_storage():
    c, db, _ = make()
    for bad in [dict(kind="grpc"), dict(base_url="ftp://x"), dict(base_url="not a url"), dict(model="")]:       # a blank name is now fine: it is generated
        body = {"name": "n", "kind": "openai", "base_url": "http://localhost:1/v1", "model": "m", **bad}
        assert c.post("/api/agent/providers", headers=H, json=body).status_code == 422, bad
    p = add_provider(c, api_key="topsecret")
    assert "topsecret" not in db.one("SELECT key_enc FROM providers")["key_enc"]
    assert c.get("/api/agent/presets", headers=H).json()[0]["local"] is True
    assert c.delete(f"/api/agent/providers/{p['id']}", headers=H).status_code == 200
    r = ask(c, "hello")
    assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_chat_errors_are_clean():
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    add_provider(c)
    r = ask(c, "hi")
    assert r.status_code == 502 and "500" in r.json()["detail"]

    def refuse(req):
        raise httpx.ConnectError("refused")
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=httpx.MockTransport(refuse)))
    r = ask(c, "hi")
    assert r.status_code == 502 and "Is it running" in r.json()["detail"]
    assert c.post("/api/agent/chat", headers=H, json={"messages": [{"role": "assistant", "content": "x"}]}).status_code == 422
    assert c.post("/api/agent/chat", json={"messages": []}).status_code == 401


def test_bad_tool_calls_from_the_model_do_not_crash():
    log = []
    c, db, _ = make()
    script = [[("no_such_tool", {}), ("search_transactions", {"limit": "banana"}), ("calculate", {"expression": "__import__('os').system('id')"}), ("read_note", {"id": 999}),
               ("propose_subscription", {"name": "x"}), ("project_balance", {"days": "soon"}), ("upcoming_bills", {"days": -5})]]
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server(script, log)))
    add_provider(c)
    out = ask(c, "break things").json()
    ok = {t["tool"]: t["ok"] for t in out["trace"]}
    assert ok["no_such_tool"] is False and ok["search_transactions"] is False and ok["calculate"] is False and ok["read_note"] is False and ok["propose_subscription"] is False
    assert ok["upcoming_bills"] is True and out["reply"].startswith("RESULTS")


def test_step_limit_stops_runaway_loops():
    log = []
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("list_accounts", {})]] * 50, log)))
    add_provider(c)
    out = ask(c, "loop forever").json()
    assert "step limit" in out["reply"] and len(log) == 8


def test_large_tool_output_is_truncated():
    log = []
    c, db, _ = make()
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server([[("search_transactions", {"limit": 500})]], log)))
    add_provider(c)
    ask(c, "everything")
    tool_msg = [m for m in log[1]["body"]["messages"] if m["role"] == "tool"][0]["content"]
    assert len(tool_msg) <= 12100


# ---------------------------------------------------------------- calculator
@pytest.mark.parametrize("expr,val", [("2+2*3", 8), ("(4200-3*89.99)/4", 982.5075), ("round(10/3, 2)", 3.33), ("$1,200 / 12", 100), ("max(3, 9, 4)", 9), ("sum([1,2,3.5])", 6.5), ("-5 + +2", -3), ("2**10", 1024), ("7 % 4", 3)])
def test_calculator(expr, val):
    assert agent.calculate(expr) == pytest.approx(val)


@pytest.mark.parametrize("expr", ["__import__('os')", "2**99999", "1/0 and 1", "x + 1", "open('f')", "'a'*5", "(lambda: 1)()", "[i for i in range(3)]", "1" * 400, "round(1,2,3,4)", ""])
def test_calculator_rejects(expr):
    with pytest.raises((ValueError, ZeroDivisionError, TypeError)):
        agent.calculate(expr)


# ---------------------------------------------------------------- the model dropdown + refresh
def models_server(routes, log=None):
    def h(req: httpx.Request):
        if log is not None:
            log.append((req.method, str(req.url), req.headers.get("authorization"), req.headers.get("x-api-key")))
        for (method, path), resp in routes.items():
            if req.method == method and req.url.path == path:
                return resp if isinstance(resp, httpx.Response) else httpx.Response(200, json=resp)
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_refresh_lists_models_from_an_openai_compatible_server():
    log = []
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=models_server({("GET", "/v1/models"): {"data": [{"id": "qwen2.5:32b"}, {"id": "llama3.1:8b"}]}}, log)))
    r = c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "http://localhost:11434/v1"}).json()
    assert [m["id"] for m in r["models"]] == ["llama3.1:8b", "qwen2.5:32b"] and r["local"] is True                 # sorted, and tagged local
    assert log[0][1] == "http://localhost:11434/v1/models" and log[0][2] is None                                    # no key sent when none given


def test_refresh_falls_back_to_ollama_native_list_and_reads_context_sizes():
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=models_server({("GET", "/api/tags"): {"models": [{"name": "mistral:7b"}]}, ("POST", "/api/show"): {"model_info": {"llama.context_length": 131072}}})))
    assert [m["id"] for m in c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "http://host.docker.internal:11434/v1"}).json()["models"]] == ["mistral:7b"]
    info = c.post("/api/agent/model-info", headers=H, json={"kind": "openai", "base_url": "http://host.docker.internal:11434/v1", "model": "mistral:7b"}).json()
    assert info["context_tokens"] == 32768 and info["model_max"] == 131072 and info["preset"] is False         # no num_ctx preset: capped, because Ollama only uses what num_ctx says
    c2 = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=models_server({})))
    assert c2.post("/api/agent/model-info", headers=H, json={"kind": "openai", "base_url": "http://x:1/v1", "model": "m"}).json() == {"context_tokens": None}     # unknown is fine


def test_refresh_openrouter_with_a_saved_key_and_anthropic_headers():
    log = []
    t = models_server({("GET", "/api/v1/models"): {"data": [{"id": "anthropic/claude-sonnet-4.5", "context_length": 200000}]}, ("GET", "/v1/models"): {"data": [{"id": "claude-sonnet-4-5"}]}}, log)
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=t))
    # cloud servers can be listed without 'allow cloud': only the key goes out, never your data
    r = c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key": "or-key"}).json()
    assert r["models"] == [{"id": "anthropic/claude-sonnet-4.5", "context": 200000}] and r["local"] is False and log[0][2] == "Bearer or-key"
    p = c.post("/api/agent/providers", headers=H, json={"kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-x", "api_key": "sk-saved"}).json()
    r = c.post("/api/agent/models", headers=H, json={"kind": "anthropic", "base_url": "https://api.anthropic.com", "provider_id": p["id"]}).json()       # refresh with the stored key
    assert [m["id"] for m in r["models"]] == ["claude-sonnet-4-5"] and log[-1][3] == "sk-saved"


def test_refresh_errors_are_readable():
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=models_server({("GET", "/v1/models"): httpx.Response(401), ("GET", "/api/tags"): httpx.Response(404)})))
    r = c.post("/api/agent/models", headers=H, json={"kind": "anthropic", "base_url": "https://api.anthropic.com"})
    assert r.status_code == 502 and "401" in r.json()["detail"] and "API key" in r.json()["detail"]
    def refuse(req): raise httpx.ConnectError("no")
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=httpx.MockTransport(refuse)))
    assert "Is it running" in c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "http://localhost:1/v1"}).json()["detail"]
    assert c.post("/api/agent/models", headers=H, json={"kind": "grpc", "base_url": "http://x"}).status_code == 422
    assert c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "file:///etc/passwd"}).status_code == 422


def test_models_get_a_generated_name():
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY))
    a = c.post("/api/agent/providers", headers=H, json={"kind": "openai", "base_url": "http://host.docker.internal:11434/v1", "model": "qwen2.5:32b"}).json()
    b = c.post("/api/agent/providers", headers=H, json={"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "x/y", "api_key": "k"}).json()
    d = c.post("/api/agent/providers", headers=H, json={"kind": "openai", "base_url": "http://nas.local:9000/v1", "model": "big"}).json()
    assert (a["name"], b["name"], d["name"]) == ("Ollama · qwen2.5:32b", "OpenRouter · x/y", "nas.local · big")
    assert c.post("/api/agent/providers", headers=H, json={"kind": "openai", "base_url": "http://x:1/v1", "model": " "}).status_code == 422


# ---------------------------------------------------------------- a model's own preset context is used
def ctx_server(routes):
    return models_server(routes)


def info_for(routes, model="m", base="http://localhost:11434/v1"):
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=ctx_server(routes)))
    return c.post("/api/agent/model-info", headers=H, json={"kind": "openai", "base_url": base, "model": model}).json()


def test_ollama_num_ctx_preset_beats_the_model_maximum():
    i = info_for({("POST", "/api/show"): {"parameters": "stop \"<|eot|>\"\nnum_ctx                        16384\ntemperature 0.7", "model_info": {"llama.context_length": 131072}}})
    assert i == {"context_tokens": 16384, "preset": True, "source": "the model's num_ctx preset (Ollama)"}


def test_other_servers_report_their_own_context():
    lm = info_for({("GET", "/api/v0/models"): {"data": [{"id": "other", "max_context_length": 4096}, {"id": "m", "max_context_length": 32768, "loaded_context_length": 8192}]}})
    assert lm["context_tokens"] == 8192 and lm["preset"] is True and lm["model_max"] == 32768                    # what is actually loaded
    lm2 = info_for({("GET", "/api/v0/models"): {"data": [{"id": "m", "max_context_length": 32768}]}})
    assert lm2["context_tokens"] == 32768 and lm2["preset"] is False
    cpp = info_for({("GET", "/props"): {"default_generation_settings": {"n_ctx": 12288}}}, base="http://localhost:8080/v1")
    assert cpp["context_tokens"] == 12288 and cpp["preset"] is True
    assert info_for({("GET", "/props"): {"n_ctx": 6000}}, base="http://localhost:8080/v1")["context_tokens"] == 6000
    assert info_for({}) == {"context_tokens": None}                                                              # a server that cannot say: no guess


def test_listing_picks_up_context_from_openrouter_and_vllm_style_fields():
    t = models_server({("GET", "/v1/models"): {"data": [{"id": "a", "max_model_len": 32000}, {"id": "b", "context_window": 64000}, {"id": "c", "context_length": 8000}, {"id": "d"}]}})
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=t))
    got = {m["id"]: m["context"] for m in c.post("/api/agent/models", headers=H, json={"kind": "openai", "base_url": "http://localhost:8000/v1"}).json()["models"]}
    assert got == {"a": 32000, "b": 64000, "c": 8000, "d": None}
    t2 = models_server({("GET", "/v1/models"): {"data": [{"id": "claude-x", "max_input_tokens": 200000}]}})
    c2 = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=t2))
    assert c2.post("/api/agent/models", headers=H, json={"kind": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "k"}).json()["models"][0]["context"] == 200000


def test_saving_a_model_without_a_context_asks_the_server_first():
    routes = {("POST", "/api/show"): {"parameters": "num_ctx 24576", "model_info": {}}}
    c = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=ctx_server(routes)))
    base = {"kind": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen"}
    p = c.post("/api/agent/providers", headers=H, json=base).json()
    assert p["context_tokens"] == 24576                                                                         # the preset was used
    typed = c.post("/api/agent/providers", headers=H, json={**base, "model": "other", "context_tokens": 4096}).json()
    assert typed["context_tokens"] == 4096                                                                      # what you typed always wins
    assert c.put(f"/api/agent/providers/{typed['id']}", headers=H, json={**base, "model": "other", "context_tokens": 16384}).json()["context_tokens"] == 16384
    assert c.put(f"/api/agent/providers/{p['id']}", headers=H, json=base).json()["context_tokens"] == 24576
    quiet = TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY, llm_transport=ctx_server({})))
    assert quiet.post("/api/agent/providers", headers=H, json=base).json()["context_tokens"] == 8192            # nothing known: the default
    cloud = quiet.post("/api/agent/providers", headers=H, json={"kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-x", "api_key": "k"}).json()
    assert cloud["context_tokens"] == 8192
    assert quiet.post("/api/agent/providers", headers=H, json={**base, "context_tokens": 100}).status_code == 422
