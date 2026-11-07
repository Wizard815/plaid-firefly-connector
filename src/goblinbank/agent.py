"""The agent: a tool-using assistant over your own data. Works with any OpenAI-compatible endpoint (Ollama, LM Studio,
llama.cpp, OpenRouter, OpenAI...) or Anthropic. It can READ through tools; the only things it can write are planner notes
and *proposals* you must approve. It cannot move money or change a subscription by itself."""
import ast
import ipaddress
import json
import operator
import re
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

from goblinbank import crypto, feed, ledger, recur, settings as cfg, sync

PRESETS = [
    {"name": "Ollama (this machine)", "kind": "openai", "base_url": "http://host.docker.internal:11434/v1", "model": "llama3.1", "tools": True},
    {"name": "LM Studio", "kind": "openai", "base_url": "http://host.docker.internal:1234/v1", "model": "local-model", "tools": True},
    {"name": "llama.cpp server", "kind": "openai", "base_url": "http://host.docker.internal:8080/v1", "model": "local", "tools": False},
    {"name": "OpenRouter (cloud)", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "tools": True},
    {"name": "OpenAI (cloud)", "kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o", "tools": True},
    {"name": "Anthropic Claude (cloud)", "kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5", "tools": True},
]


def is_local(base_url: str) -> bool:
    """True when the endpoint is on this machine or your own network, so your data never leaves it."""
    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal", "host.containers.internal") or host.endswith((".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return "." not in host and bool(host)        # bare names are docker/LAN hosts, e.g. http://ollama:11434


# ------------------------------------------------------------------ safe calculator
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"round": round, "min": min, "max": max, "abs": abs, "sum": sum}


def calculate(expr: str) -> float:
    """Arithmetic only: + - * / // % ** ( ) numbers and round/min/max/abs/sum. No names, no attributes, no imports."""
    if len(expr) > 300:
        raise ValueError("expression too long")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e9):
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x) for x in n.elts]
        raise ValueError("only arithmetic is allowed")
    try:
        cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expr.replace("$", ""))      # 1,200 -> 1200, but keep commas between arguments
        return ev(ast.parse(cleaned.strip(), mode="eval"))
    except SyntaxError:
        raise ValueError("could not parse the expression")


# ------------------------------------------------------------------ tools
def S(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


NUM, STR, INT = {"type": "number"}, {"type": "string"}, {"type": "integer"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}


def build_tools(ctx):
    store, today = ctx.store, ctx.today

    def trim(rows, n=40):
        return rows[:n], max(0, len(rows) - n)

    def t_overview(a):
        o = ledger.overview(store, int(a.get("days", 30)), today())
        return {"today": today().isoformat(), "net_worth": o["net_worth"], "by_kind": o["by_kind"], "subscriptions_monthly": o["subscriptions_monthly"], "savings_total": o["savings_total"],
                "next_days": {k: o["upcoming"][k] for k in ("days", "total", "count", "others_total", "by_category")}, "alerts": o["alerts"]}

    def t_accounts(a):
        return [{k: r[k] for k in ("id", "institution", "name", "kind", "balance", "min_balance", "source")} for r in store.all("SELECT * FROM accounts ORDER BY kind, name")]

    def t_search(a):
        r = ledger.query_transactions(store, a.get("q"), a.get("account_id"), a.get("category"), a.get("min_amount"), a.get("max_amount"), a.get("start"), a.get("end"),
                                      a.get("direction"), min(int(a.get("limit", 25)), 50), 0, a.get("sort", "date_desc"))
        r["items"] = [{k: t[k] for k in ("id", "date", "name", "amount", "category", "account_name")} for t in r["items"]]
        r["note"] = f"showing {len(r['items'])} of {r['total']} matches; the totals cover all matches"
        return r

    def t_spending(a):
        rows = ledger.spending_by_category(store, a.get("start"), a.get("end"))
        return {"by_category": rows, "total_spent": round(sum(r["spent"] for r in rows), 2), "note": "transfers excluded"}

    def t_subs(a):
        return [{k: s[k] for k in ("id", "name", "amount", "label", "next_due", "monthly", "category", "payer", "payer_name", "account_name", "payment_method", "active")} for s in ledger.subscriptions(store, today())]

    def t_upcoming(a):
        u = ledger.upcoming(store, int(a.get("days", 30)), today())
        u["items"], more = trim(u["items"], 60)
        u["more_not_shown"] = more
        return u

    def t_recurring(a):
        return ledger.suggestions(store, today())

    def t_savings(a):
        return ledger.savings_summary(store, today())

    def t_alerts(a):
        return ledger.alerts(store, today())

    def t_project(a):
        p = ledger.project_balance(store, int(a.get("days", 30)), today(), [a["account_id"]] if a.get("account_id") else None, a.get("include_income", True))
        p["events"], p["more_not_shown"] = trim(p["events"], 40)
        return p

    def t_budget(a):
        m = a.get("month") or today().strftime("%Y-%m")
        return {"month": m, "items": ledger.budget_status(store, m, today())}

    def t_calendar(a):
        url, user, pw = (cfg.get(store, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
        if not url:
            return {"error": "CalDAV is not set up (Settings > Calendar)"}
        t = today()
        try:
            ev = feed.read_caldav(url, user, pw, t - timedelta(days=int(a.get("days_back", 0))), t + timedelta(days=int(a.get("days_ahead", 30))), ctx.caldav_transport)
        except httpx.HTTPError as e:
            return {"error": f"calendar unreachable: {type(e).__name__}"}
        ev, more = trim(ev, 60)
        return {"events": ev, "more_not_shown": more, "note": "amount is parsed from a $ figure in the title; null means none found"}

    def t_notes(a):
        return [{"id": n["id"], "title": n["title"], "author": n["author"], "updated": n["updated"]} for n in store.all("SELECT * FROM notes ORDER BY updated DESC")]

    def t_read_note(a):
        n = store.one("SELECT * FROM notes WHERE id=?", (a.get("id"),))
        return n or {"error": "note not found"}

    def t_write_note(a):
        title, body = str(a.get("title", "")).strip(), str(a.get("body", ""))
        if not title:
            return {"error": "title required"}
        ts = sync.now()
        with store.tx() as c:
            cur = c.execute("INSERT INTO notes(title,body,author,pinned,created,updated) VALUES(?,?,?,?,?,?)", (title[:120], body[:20000], "agent", 0, ts, ts))
        return {"saved": True, "id": cur.lastrowid, "author": "agent"}

    def t_propose(a):
        try:
            every, unit = recur.resolve(a.get("every"), a.get("unit"), a.get("cycle"))
            amount = float(a["amount"])
            name = str(a["name"]).strip()
            assert name and amount >= 0
        except (ValueError, KeyError, AssertionError, TypeError):
            return {"error": "need name, amount and a valid interval (every + unit, or cycle)"}
        with store.tx() as c:
            cur = c.execute("INSERT INTO proposals(name,amount,every,unit,account_id,reason) VALUES(?,?,?,?,?,?)", (name, amount, every, unit, a.get("account_id") or None, str(a.get("reason", ""))[:500]))
        return {"proposed": True, "id": cur.lastrowid, "note": "The user must approve it on the Subscriptions page. Nothing was added yet."}

    def t_calc(a):
        try:
            return {"expression": a.get("expression"), "result": calculate(str(a.get("expression", "")))}
        except (ValueError, ZeroDivisionError, TypeError, OverflowError) as e:
            return {"error": str(e)}

    return [
        ("get_overview", "Net worth, balances by type, monthly subscription cost, the bills due in the next N days, and active alerts.", S({"days": {**INT, "description": "window for upcoming bills, default 30"}}), t_overview),
        ("list_accounts", "Every account with id, institution, type (cash/credit/investment/loan), balance and minimum-balance alert threshold.", S({}), t_accounts),
        ("search_transactions", "Search and total transactions. Amounts are signed: negative = money out. Totals cover ALL matches even when the list is shorter.",
         S({"q": {**STR, "description": "words in the description"}, "account_id": STR, "category": {**STR, "description": "one or more category names, comma separated"}, "min_amount": {**NUM, "description": "magnitude, >= 0"}, "max_amount": NUM,
            "start": DATE, "end": DATE, "direction": {"type": "string", "enum": ["in", "out"]}, "limit": INT, "sort": {"type": "string", "enum": ["date_desc", "date_asc", "amount_desc", "amount_asc"]}}), t_search),
        ("spending_by_category", "Money spent per category between two dates (transfers excluded).", S({"start": DATE, "end": DATE}), t_spending),
        ("list_subscriptions", "Every subscription/recurring bill: amount, repeat interval, next due date, monthly cost, who pays, bound account.", S({}), t_subs),
        ("upcoming_bills", "Bills due in the next N days with each occurrence dated, the total you pay, and what others pay.", S({"days": INT}), t_upcoming),
        ("find_recurring_payments", "Payments that repeat at a steady interval and amount in the transaction history but are not tracked yet, plus pending proposals.", S({}), t_recurring),
        ("savings_summary", "Savings, CDs and similar: balances, APY, yearly interest, maturity dates and values, goals.", S({}), t_savings),
        ("get_alerts", "Active alerts: overdrawn or low accounts, expected payments not seen, CDs maturing, bills due soon.", S({}), t_alerts),
        ("project_balance", "Estimate cash balance over the next N days from today's balances, upcoming bills and detected steady income. Excludes everyday spending.",
         S({"days": INT, "account_id": STR, "include_income": {"type": "boolean"}}), t_project),
        ("budget_status", "Budget vs actual spending per category for a month (YYYY-MM, default this month).", S({"month": STR}), t_budget),
        ("calendar_events", "Read events from the user's CalDAV calendar, with a dollar amount when the title has one.", S({"days_ahead": INT, "days_back": INT}), t_calendar),
        ("list_notes", "Titles of planner notes.", S({}), t_notes),
        ("read_note", "Read one planner note by id.", S({"id": INT}, ["id"]), t_read_note),
        ("write_note", "Save a note in the user's Planner (marked as written by the agent). Use it for plans, budgets and summaries the user asks for.", S({"title": STR, "body": STR}, ["title", "body"]), t_write_note),
        ("propose_subscription", "Suggest tracking a repeating payment. It only creates a proposal the user must approve.", S({"name": STR, "amount": NUM, "every": INT, "unit": {"type": "string", "enum": list(recur.UNITS)}, "account_id": STR, "reason": STR}, ["name", "amount", "every", "unit"]), t_propose),
        ("calculate", "Exact arithmetic for any math question. Use it instead of doing sums in your head.", S({"expression": {**STR, "description": "e.g. (4200 - 3*89.99) / 4"}}, ["expression"]), t_calc),
    ]


# ------------------------------------------------------------------ provider adapters
def system_prompt(name: str, today: date) -> str:
    return (f"You are Goblin, the finance assistant for {name}'s private bank dashboard. Today is {today.isoformat()}.\n"
            "You can only see data through the tools. Use them for every figure; never guess or invent numbers. Use calculate for arithmetic and show the working briefly.\n"
            "Amounts are signed: positive is money in, negative is money out. Projections are estimates that leave out everyday spending, so say so.\n"
            "You cannot move money or edit subscriptions. You may save planner notes and propose subscriptions, which the user approves. Be concise.")


def _call_openai(http, prov, key, system, messages, tools):
    msgs = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            msgs.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["args"])}} for t in m["tool_calls"]]})
        elif m["role"] == "tool":
            msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    body = {"model": prov["model"], "messages": msgs, "temperature": 0.2}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, d, s, _ in tools]
    r = http.post(prov["base_url"].rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    calls = []
    for t in m.get("tool_calls") or []:
        try:
            args = json.loads(t["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append({"id": t.get("id") or t["function"]["name"], "name": t["function"]["name"], "args": args if isinstance(args, dict) else {}})
    return {"content": m.get("content") or "", "tool_calls": calls}


def _call_anthropic(http, prov, key, system, messages, tools):
    msgs, pending = [], []
    for m in messages:
        if m["role"] == "tool":
            pending.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]})
            continue
        if pending:
            msgs.append({"role": "user", "content": pending}); pending = []
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]} for t in m["tool_calls"]]
            msgs.append({"role": "assistant", "content": blocks})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    if pending:
        msgs.append({"role": "user", "content": pending})
    body = {"model": prov["model"], "max_tokens": 2048, "system": system, "messages": msgs}
    if tools:
        body["tools"] = [{"name": n, "description": d, "input_schema": s} for n, d, s, _ in tools]
    r = http.post(prov["base_url"].rstrip("/") + "/v1/messages", json=body, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return {"content": "".join(b.get("text", "") for b in blocks if b["type"] == "text"),
            "tool_calls": [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}} for b in blocks if b["type"] == "tool_use"]}


def run_chat(ctx, prov: dict, key: str, history: list[dict], max_steps: int = 8) -> dict:
    tools = build_tools(ctx)
    by_name = {n: f for n, _, _, f in tools}
    system = system_prompt(ctx.name, ctx.today())
    call = _call_anthropic if prov["kind"] == "anthropic" else _call_openai
    use_tools = bool(prov["tools"])
    if not use_tools:        # models without function calling get a snapshot of the key facts instead
        snap = {n: by_name[n]({}) for n in ("get_overview", "list_accounts", "list_subscriptions", "upcoming_bills", "savings_summary", "find_recurring_payments", "budget_status")}
        system += "\nYou have no tools. Here is a snapshot of the user's data as JSON:\n" + json.dumps(snap, default=str)[:14000]
    msgs, trace = [dict(m) for m in history], []
    with httpx.Client(timeout=300, transport=ctx.llm_transport) as http:
        for step in range(max_steps):
            out = call(http, prov, key, system, msgs, tools if use_tools else None)
            if not out["tool_calls"]:
                return {"reply": out["content"].strip(), "trace": trace, "steps": step + 1}
            msgs.append({"role": "assistant", "content": out["content"], "tool_calls": out["tool_calls"]})
            for t in out["tool_calls"]:
                fn = by_name.get(t["name"])
                try:
                    res = fn(t["args"]) if fn else {"error": f"unknown tool {t['name']}"}
                except Exception as e:                      # a bad argument from the model must not crash the chat
                    res = {"error": f"{type(e).__name__}: {e}"}
                text = json.dumps(res, default=str)
                if len(text) > 12000:
                    text = text[:12000] + '..."(truncated)"'
                trace.append({"tool": t["name"], "args": t["args"], "ok": "error" not in res if isinstance(res, dict) else True, "preview": text[:160]})
                msgs.append({"role": "tool", "tool_call_id": t["id"], "content": text})
    return {"reply": "I hit my step limit before finishing. Try a narrower question.", "trace": trace, "steps": max_steps}


# ------------------------------------------------------------------ routes
class ProviderIn(BaseModel):
    name: str
    kind: str = "openai"
    base_url: str
    model: str
    api_key: str | None = None          # omit = unchanged, "" = clear
    tools: bool = True
    allow_cloud: bool = False


class ChatIn(BaseModel):
    messages: list[dict]
    provider_id: int | None = None


def register(app, ctx):
    store, auth = ctx.store, ctx.auth

    def view(p):
        return {"id": p["id"], "name": p["name"], "kind": p["kind"], "base_url": p["base_url"], "model": p["model"], "tools": bool(p["tools"]),
                "allow_cloud": bool(p["allow_cloud"]), "has_key": bool(p["key_enc"]), "local": is_local(p["base_url"])}

    def check(p: ProviderIn):
        if p.kind not in ("openai", "anthropic"):
            raise HTTPException(422, "kind must be openai (any OpenAI-compatible server) or anthropic")
        if not p.name.strip() or not p.model.strip():
            raise HTTPException(422, "name and model required")
        if urlparse(p.base_url).scheme not in ("http", "https") or not urlparse(p.base_url).hostname:
            raise HTTPException(422, "base_url must be an http(s) address")

    @app.get("/api/agent/presets")
    def presets(request: Request):
        auth(request)
        return [{**p, "local": is_local(p["base_url"])} for p in PRESETS]

    @app.get("/api/agent/providers")
    def providers(request: Request):
        auth(request)
        return {"items": [view(p) for p in store.all("SELECT * FROM providers ORDER BY id")], "default": cfg.get(store, "agent_provider")}

    @app.post("/api/agent/providers")
    def provider_add(request: Request, p: ProviderIn):
        auth(request)
        check(p)
        with store.tx() as c:
            cur = c.execute("INSERT INTO providers(name,kind,base_url,model,key_enc,tools,allow_cloud) VALUES(?,?,?,?,?,?,?)",
                            (p.name.strip(), p.kind, p.base_url.strip(), p.model.strip(), crypto.encrypt(p.api_key) if p.api_key else "", int(p.tools), int(p.allow_cloud)))
        return view(store.one("SELECT * FROM providers WHERE id=?", (cur.lastrowid,)))

    @app.put("/api/agent/providers/{pid}")
    def provider_update(request: Request, pid: int, p: ProviderIn):
        auth(request)
        check(p)
        row = store.one("SELECT * FROM providers WHERE id=?", (pid,))
        if not row:
            raise HTTPException(404, "not found")
        key = row["key_enc"] if p.api_key is None else (crypto.encrypt(p.api_key) if p.api_key else "")
        with store.tx() as c:
            c.execute("UPDATE providers SET name=?,kind=?,base_url=?,model=?,key_enc=?,tools=?,allow_cloud=? WHERE id=?",
                      (p.name.strip(), p.kind, p.base_url.strip(), p.model.strip(), key, int(p.tools), int(p.allow_cloud), pid))
        return view(store.one("SELECT * FROM providers WHERE id=?", (pid,)))

    @app.delete("/api/agent/providers/{pid}")
    def provider_delete(request: Request, pid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM providers WHERE id=?", (pid,))
        return {"deleted": pid}

    @app.get("/api/agent/tools")
    def tool_list(request: Request):
        auth(request)
        return [{"name": n, "description": d, "parameters": s} for n, d, s, _ in build_tools(ctx)]

    @app.post("/api/agent/chat")
    def chat(request: Request, body: ChatIn):
        auth(request)
        pid = body.provider_id or (int(cfg.get(store, "agent_provider")) if cfg.get(store, "agent_provider") else None)
        row = store.one("SELECT * FROM providers WHERE id=?", (pid,)) if pid else store.one("SELECT * FROM providers ORDER BY id LIMIT 1")
        if not row:
            raise HTTPException(409, "No model set up yet. Add one in Settings > Agent.")
        local = is_local(row["base_url"])
        if not local and not row["allow_cloud"]:
            raise HTTPException(403, f"'{row['name']}' is not on your machine or network. Your financial data would be sent to {urlparse(row['base_url']).hostname}. Tick 'allow cloud' on that provider if you really want this.")
        hist = [{"role": m["role"], "content": str(m.get("content", ""))} for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
        if not hist or hist[-1]["role"] != "user":
            raise HTTPException(422, "last message must be from the user")
        try:
            out = run_chat(ctx, row, crypto.decrypt(row["key_enc"]) if row["key_enc"] else "", hist[-30:])
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"model server answered {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"could not reach the model at {row['base_url']} ({type(e).__name__}). Is it running?")
        return {**out, "provider": {"id": row["id"], "name": row["name"], "model": row["model"], "local": local}}
