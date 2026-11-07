import json
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devtools.fake_llm import openai_server
from devtools.seed import seed
from goblinbank import goblin
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def stack(script=None, seeded=True, **prov):
    log = []
    db = DB(":memory:")
    if seeded:
        seed(db, 5, TODAY)
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=openai_server(script or [], log)))
    body = {"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": True, **prov}
    assert c.post("/api/agent/providers", headers=H, json=body).status_code == 200
    return c, db, log


def say(c, cid, text, **kw):
    return c.post(f"/api/chats/{cid}/messages", headers=H, json={"content": text, **kw})


def test_chats_are_saved_listed_renamed_deleted():
    c, db, _ = stack([[("calculate", {"expression": "2+2"})]])
    a = c.post("/api/chats", headers=H).json()["chat"]
    b = c.post("/api/chats", headers=H).json()["chat"]
    r = say(c, a["id"], "what is 2+2 plus my bills?")
    assert r.status_code == 200 and r.json()["messages"][-1]["role"] == "assistant"
    assert r.json()["chat"]["title"] == "what is 2+2 plus my bills?"                 # auto-titled from the first question
    assert [m["role"] for m in c.get(f"/api/chats/{a['id']}", headers=H).json()["messages"]] == ["user", "assistant"]
    assert c.get(f"/api/chats/{b['id']}", headers=H).json()["messages"] == []         # chats are separate
    listing = c.get("/api/chats", headers=H).json()
    assert [x["id"] for x in listing] == [a["id"], b["id"]] and listing[0]["messages"] == 2
    assert c.put(f"/api/chats/{b['id']}", headers=H, json={"title": "Budget talk"}).json()["chat"]["title"] == "Budget talk"
    assert c.put(f"/api/chats/{b['id']}", headers=H, json={"title": " "}).status_code == 422
    trace = c.get(f"/api/chats/{a['id']}", headers=H).json()["messages"][-1]["trace"]
    assert trace[0]["tool"] == "calculate" and trace[0]["ok"]                          # the trace is saved with the answer
    assert c.delete(f"/api/chats/{a['id']}", headers=H).status_code == 200
    assert c.get(f"/api/chats/{a['id']}", headers=H).status_code == 404
    assert db.one("SELECT COUNT(*) n FROM chat_messages WHERE chat_id=?", (a["id"],))["n"] == 0
    assert say(c, 999, "hi").status_code == 404 and say(c, b["id"], "  ").status_code == 422


def test_history_is_sent_back_so_the_chat_has_memory():
    c, _, log = stack()
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    say(c, cid, "my rent is 900"); say(c, cid, "and what did I say my rent was?")
    sent = [m["content"] for m in log[-1]["body"]["messages"] if m["role"] == "user"]
    assert sent == ["my rent is 900", "and what did I say my rent was?"]


def test_context_meter_and_auto_compaction():
    c, db, log = stack(tools=False, context_tokens=1024)       # tiny window so it fills fast
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    assert c.get(f"/api/chats/{cid}", headers=H).json()["usage"]["limit"] == 1024
    flags = []
    for i in range(5):
        r = say(c, cid, f"question number {i} " + "details " * 30)
        assert r.status_code == 200, r.text
        flags.append(r.json()["compacted"])
    view = c.get(f"/api/chats/{cid}", headers=H).json()
    assert any(flags) and view["chat"]["summary"].startswith("SUMMARY:")
    assert any(m["compacted"] for m in view["messages"]) and len(view["messages"]) == 10        # everything stays visible...
    live = [m for m in view["messages"] if not m["compacted"]]
    assert len(live) < 10 and live[-1]["role"] == "assistant"                                   # ...but less is sent to the model
    last_req = [x for x in log if not x["body"]["messages"][0]["content"].startswith("You write")][-1]["body"]["messages"]
    assert "compacted summary" in last_req[0]["content"] and "SUMMARY:" in last_req[0]["content"]
    assert len([m for m in last_req if m["role"] == "user"]) == len([m for m in live if m["role"] == "user"])        # only live turns are sent


def test_manual_compact_keeps_recent_turns():
    c, _, _ = stack()
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    for q in ("one", "two", "three"):
        say(c, cid, q)
    assert c.post(f"/api/chats/{cid}/compact", headers=H).json()["compacted"] is True
    msgs = c.get(f"/api/chats/{cid}", headers=H).json()["messages"]
    assert [m["compacted"] for m in msgs] == [1, 1, 1, 1, 0, 0]
    assert c.post(f"/api/chats/{cid}/compact", headers=H).json()["compacted"] is False        # nothing left worth compacting
    assert c.post("/api/chats/999/compact", headers=H).status_code == 404


def test_per_chat_tool_switch_and_model_switch():
    c, db, log = stack([[("calculate", {"expression": "1+1"})]])
    other = c.post("/api/agent/providers", headers=H, json={"name": "Big", "kind": "openai", "base_url": "http://localhost:8000/v1", "model": "big-70b", "tools": False}).json()
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    say(c, cid, "with tools")
    assert "tools" in log[0]["body"] and log[0]["body"]["model"] == "m"
    log.clear()
    r = say(c, cid, "no tools please", tools=False)                          # switch off tool calling for this message
    assert "tools" not in log[0]["body"] and "snapshot" in log[0]["body"]["messages"][0]["content"] and r.json()["tools_used"] is False
    log.clear()
    r = say(c, cid, "now the big model", provider_id=other["id"])           # switch model mid-chat
    assert log[0]["body"]["model"] == "big-70b" and r.json()["provider"]["name"] == "Big"
    assert c.get(f"/api/chats/{cid}", headers=H).json()["chat"]["provider_id"] == other["id"]     # remembered for next time


def test_failed_turn_leaves_nothing_behind_and_cloud_is_guarded():
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, llm_transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    c.post("/api/agent/providers", headers=H, json={"name": "L", "kind": "openai", "base_url": "http://localhost:1/v1", "model": "m"})
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    r = say(c, cid, "hello")
    assert r.status_code == 502 and db.one("SELECT COUNT(*) n FROM chat_messages")["n"] == 0
    c.post("/api/agent/providers", headers=H, json={"name": "Cloud", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "m", "api_key": "k"})
    r = say(c, cid, "hello", provider_id=2)
    assert r.status_code == 403 and "openrouter.ai" in r.json()["detail"] and db.one("SELECT COUNT(*) n FROM chat_messages")["n"] == 0
    assert TestClient(goblin.create_app(db=DB(":memory:"), today=lambda: TODAY)).post("/api/chats/1/messages", headers=H, json={"content": "x"}).status_code == 404


def test_provider_context_size_validation():
    c, _, _ = stack()
    base = {"name": "n", "kind": "openai", "base_url": "http://localhost:1/v1", "model": "m"}
    assert c.post("/api/agent/providers", headers=H, json={**base, "context_tokens": 100}).status_code == 422
    assert c.post("/api/agent/providers", headers=H, json={**base, "context_tokens": 32768}).json()["context_tokens"] == 32768
    assert c.get("/api/agent/providers", headers=H).json()["items"][0]["context_tokens"] == 8192


# ---------------------------------------------------------------- the agent can add subscriptions when asked
def test_agent_adds_a_subscription_and_it_shows_up():
    script = [[("add_subscription", {"name": "Gym", "amount": 29.99, "every": 6, "unit": "week", "account_id": "chk", "payer": "me", "next_date": "2026-11-20"})]]
    c, db, _ = stack(script)
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    r = say(c, cid, "please add my gym, $29.99 every 6 weeks from checking").json()
    t = r["messages"][-1]["trace"][0]
    assert t["tool"] == "add_subscription" and t["ok"] and t["result"]["added"] and t["result"]["repeats"] == "every 6 weeks" and t["result"]["account"] == "Checking"
    subs = c.get("/api/subscriptions", headers=H).json()["items"]
    assert [(s["name"], s["amount"], s["account_id"]) for s in subs] == [("Gym", 29.99, "chk")]
    assert c.delete(f"/api/subscriptions/{t['result']['id']}", headers=H).status_code == 200                # the UI's Undo


@pytest.mark.parametrize("args", [{"name": "x"}, {"name": "x", "amount": 5}, {"name": "x", "amount": 5, "cycle": "fortnightly"}, {"name": "x", "amount": 5, "cycle": "monthly", "account_id": "ghost"},
                                  {"name": "x", "amount": 5, "cycle": "monthly", "payer": "other"}, {"name": "x", "amount": -5, "cycle": "monthly"}, {"name": "x", "amount": 5, "cycle": "monthly", "category": "Nope"},
                                  {"name": "x", "amount": "lots", "cycle": "monthly"}, {"name": "x", "amount": 5, "cycle": "monthly", "next_date": "soon"}])
def test_agent_add_subscription_rejects_bad_input(args):
    c, db, _ = stack([[("add_subscription", args)]])
    cid = c.post("/api/chats", headers=H).json()["chat"]["id"]
    t = say(c, cid, "add it").json()["messages"][-1]["trace"][0]
    assert t["ok"] is False and "error" in t["result"] and db.one("SELECT COUNT(*) n FROM subscriptions")["n"] == 0


# ---------------------------------------------------------------- flags and tags
def test_untracked_recurring_alert_and_toggles():
    c, db, _ = stack(seeded=True)
    alerts = {a["kind"]: a for a in c.get("/api/alerts", headers=H).json()}
    assert alerts["untracked_recurring"]["count"] >= 3 and any(w in alerts["untracked_recurring"]["detail"].lower() for w in ("netflix", "farm", "water"))
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,account_id,created) VALUES('City Water',61.25,'monthly',1,'month','2026-11-10','chk','x')"); db.conn.commit()
    assert "missed_payment" in {a["kind"] for a in c.get("/api/alerts", headers=H).json()}
    assert c.put("/api/settings", headers=H, json={"alert_missed": "off"}).json()["alert_missed"] == "off"
    assert "missed_payment" not in {a["kind"] for a in c.get("/api/alerts", headers=H).json()}
    c.put("/api/settings", headers=H, json={"alert_untracked": "off"})
    assert "untracked_recurring" not in {a["kind"] for a in c.get("/api/alerts", headers=H).json()}
    assert c.put("/api/settings", headers=H, json={"alert_missed": "maybe"}).status_code == 422


def test_category_tag_controls_whether_it_counts_as_spending():
    c, db, _ = stack(seeded=True)
    cats = {x["name"]: x["kind"] for x in c.get("/api/categories", headers=H).json()}
    assert cats["Investing"] == "transfer" and cats["Food"] == "expense"
    tx = c.get("/api/transactions?limit=1&direction=out", headers=H).json()["items"][0]
    c.put(f"/api/transactions/{tx['id']}/category", headers=H, json={"category": "Investing"})
    spent = lambda: {r["category"]: r["spent"] for r in c.get("/api/spending", headers=H).json()}
    assert "Investing" not in spent()                                          # tagged 'not counted' by default
    c.put("/api/categories/Investing", headers=H, json={"kind": "expense"})
    assert spent()["Investing"] == pytest.approx(-tx["amount"], abs=0.01)      # flip the tag and it counts
    assert c.put("/api/categories/Nope", headers=H, json={"kind": "expense"}).status_code == 404
    assert c.put("/api/categories/Food", headers=H, json={"kind": "bogus"}).status_code == 422
