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

from goblinbank import crypto, feed, ledger, plan, recur, settings as cfg, sync

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
        gv, sv = plan.goals_view(store, today()), ledger.savings_summary(store, today())
        return {"places": gv["places"], "total_savings": gv["total"], "earmarked_for_goals": gv["allocated"], "free_savings": gv["free"], "overallocated": gv["overallocated"],
                "weighted_apy": sv["weighted_apy"], "yearly_interest": sv["yearly_interest"], "maturing_soon": sv["maturing_soon"],
                "goals": [{k: g[k] for k in ("id", "name", "target", "allocated", "shortfall", "pct", "deadline", "priority", "monthly", "status") if k in g} for g in gv["items"]]}

    def t_goals(a):
        gv = plan.goals_view(store, today())
        return {"free_savings": gv["free"], "total_savings": gv["total"], "earmarked": gv["allocated"], "still_needed_for_goals": gv["needed"],
                "goals": [{k: v for k, v in g.items() if k != "allocations"} | {"earmarks": g["allocations"]} for g in gv["items"]]}

    def t_add_goal(a):
        try:
            g = plan.add_goal(store, str(a.get("name", "")), float(a["target"]), today(), a.get("deadline") or None, int(a.get("priority", 2)), float(a.get("monthly", 0) or 0), str(a.get("note", "")), bool(a.get("use_free_savings", False)))
        except (plan.PlanError, KeyError, TypeError, ValueError) as e:
            return {"error": f"could not add the goal: {e}"}
        return {"added": True, "id": g["id"], "name": g["name"], "target": g["target"], "earmarked_now": g["allocated"], "shortfall": g["shortfall"], "funded": g["funded"],
                "ready_date": g.get("ready_date"), "monthly_needed": g.get("monthly_needed")}

    def t_fund_goal(a):
        try:
            gid, amt = int(a["goal_id"]), float(a["amount"])
            before = {x["place"]: x["amount"] for x in next(g for g in plan.goals_view(store, today())["items"] if g["id"] == gid)["allocations"]}.get(a.get("place") or "", 0.0)
            g = plan.fund(store, gid, amt, today(), a.get("place") or "")
        except (plan.PlanError, KeyError, StopIteration, TypeError, ValueError) as e:
            return {"error": f"could not earmark that: {e}"}
        return {"funded": True, "goal_id": g["id"], "name": g["name"], "added": amt, "place": a.get("place") or "", "earmarked_before": before, "earmarked_now": g["allocated"], "shortfall": g["shortfall"]}

    def t_maturities(a):
        return plan.maturity_outlook(store, today(), int(a.get("days", 365) or 365))

    def t_afford(a):
        try:
            return plan.afford(store, float(a["amount"]), today())
        except (KeyError, TypeError, ValueError):
            return {"error": "amount is required"}

    def t_forecast(a):
        f = ledger.spending_forecast(store, today(), int(a.get("months", 6) or 6))
        f["categories"] = f["categories"][:12]
        return f

    def t_plan(a):
        m = a.get("month") or today().strftime("%Y-%m")
        try:
            return plan.monthly_plan(store, m, today())
        except ValueError:
            return {"error": "month must be YYYY-MM"}

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

    def t_add_sub(a):
        keep = {k: a[k] for k in ("name", "amount", "next_date", "cycle", "every", "unit", "category", "account_id", "payment_method", "payer", "payer_name", "notify_days", "end_date", "notes", "url") if a.get(k) not in (None, "")}
        keep.setdefault("next_date", today().isoformat())
        try:
            sub = ctx.create_sub(keep)
        except HTTPException as e:
            return {"error": str(e.detail)}
        return {"added": True, "id": sub["id"], "name": sub["name"], "amount": sub["amount"], "repeats": sub["label"], "next_due": sub["next_due"], "account": sub.get("account_name")}

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
        ("savings_summary", "Where the savings are (accounts, CDs...), total, how much is earmarked for goals and how much is free, APY and maturities.", S({}), t_savings),
        ("list_goals", "Savings goals (things to save for): target, earmarked amount, shortfall, deadline, monthly needed. Also the free savings.", S({}), t_goals),
        ("maturity_outlook", "For each CD/bond with a maturity date: value then, what is pinned to goals, what is free, and which goals the free money would reach in priority order.", S({"days": INT}), t_maturities),
        ("can_i_afford", "Check whether a purchase fits in FREE savings (not earmarked) and how long saving would take at the monthly plan's leftover.", S({"amount": NUM}, ["amount"]), t_afford),
        ("spending_forecast", "Projected spending for this month from past months (per category), what is spent so far, and how both compare to average monthly income.", S({"months": INT}), t_forecast),
        ("monthly_plan", "Monthly income vs bills, budgets and goal contributions, with what is left over. month is YYYY-MM (default this month).", S({"month": STR}), t_plan),
        ("add_goal", "Create a savings goal. ONLY when the user asks. It starts unfunded; set use_free_savings true ONLY if the user says to take the free money now. Optional deadline (YYYY-MM-DD), priority 1 high..3 low, monthly contribution.",
         S({"name": STR, "target": NUM, "deadline": DATE, "priority": INT, "monthly": NUM, "note": STR, "use_free_savings": {"type": "boolean"}}, ["name", "target"]), t_add_goal),
        ("fund_goal", "Earmark FREE savings for a goal (goal_id from list_goals). ONLY when the user asks. Optional place = a savings place key like s:3 or a:abc to take it from one account.",
         S({"goal_id": INT, "amount": NUM, "place": STR}, ["goal_id", "amount"]), t_fund_goal),
        ("get_alerts", "Active alerts: overdrawn or low accounts, expected payments not seen, CDs maturing, bills due soon.", S({}), t_alerts),
        ("project_balance", "Estimate cash balance over the next N days from today's balances, upcoming bills and detected steady income. Excludes everyday spending.",
         S({"days": INT, "account_id": STR, "include_income": {"type": "boolean"}}), t_project),
        ("budget_status", "Budget vs actual spending per category for a month (YYYY-MM, default this month).", S({"month": STR}), t_budget),
        ("calendar_events", "Read events from the user's CalDAV calendar, with a dollar amount when the title has one.", S({"days_ahead": INT, "days_back": INT}), t_calendar),
        ("list_notes", "Titles of planner notes.", S({}), t_notes),
        ("read_note", "Read one planner note by id.", S({"id": INT}, ["id"]), t_read_note),
        ("write_note", "Save a note in the user's Planner (marked as written by the agent). Use it for plans, budgets and summaries the user asks for.", S({"title": STR, "body": STR}, ["title", "body"]), t_write_note),
        ("propose_subscription", "Suggest tracking a repeating payment. It only creates a proposal the user must approve.", S({"name": STR, "amount": NUM, "every": INT, "unit": {"type": "string", "enum": list(recur.UNITS)}, "account_id": STR, "reason": STR}, ["name", "amount", "every", "unit"]), t_propose),
        ("add_subscription", "Add a subscription/recurring bill to the user's Subscriptions. ONLY call this when the user has clearly asked you to add it; otherwise use propose_subscription. "
         "Needs name, amount and an interval (cycle like monthly/weekly/quarterly/yearly, or every + unit). Optional: next_date, account_id (from list_accounts), payer 'me' or 'other' (then payer_name), category, payment_method.",
         S({"name": STR, "amount": NUM, "cycle": STR, "every": INT, "unit": {"type": "string", "enum": list(recur.UNITS)}, "next_date": DATE, "account_id": STR, "payer": {"type": "string", "enum": ["me", "other"]},
            "payer_name": STR, "category": STR, "payment_method": STR, "end_date": DATE, "notes": STR}, ["name", "amount"]), t_add_sub),
        ("calculate", "Exact arithmetic for any math question. Use it instead of doing sums in your head.", S({"expression": {**STR, "description": "e.g. (4200 - 3*89.99) / 4"}}, ["expression"]), t_calc),
    ]


# ------------------------------------------------------------------ provider adapters
def system_prompt(name: str, today: date) -> str:
    return (f"You are Goblin, the finance assistant for {name}'s private bank dashboard. Today is {today.isoformat()}.\n"
            "You can only see data through the tools. Use them for every figure; never guess or invent numbers. Use calculate for arithmetic and show the working briefly.\n"
            "Amounts are signed: positive is money in, negative is money out. Projections are estimates that leave out everyday spending, so say so.\n"
            "You cannot move money. You may save planner notes. Add a subscription, add a goal or earmark savings only when the user explicitly asks (add_subscription, add_goal, fund_goal); otherwise propose it. "
            "Savings model: savings sit in places (accounts, CDs); goals are what they are for; earmarked money is reserved for a goal and free money is the rest. Before saying someone can afford something, call can_i_afford. Be concise.")


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


WRITE_TOOLS = {"add_subscription", "write_note", "propose_subscription", "add_goal", "fund_goal"}


def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def context_usage(ctx, prov: dict, summary: str, msgs: list[dict], use_tools: bool, extra: str = "") -> dict:
    """Rough token count (about 4 characters per token) against the model's context window."""
    base = est_tokens(system_prompt(ctx.name, ctx.today()) + (json.dumps([(n, d, s) for n, d, s, _ in build_tools(ctx)]) if use_tools else "") + (summary or "") + (extra or ""))
    used = base + (0 if use_tools else 3500) + sum(est_tokens(m["content"]) + 4 for m in msgs)       # no-tool mode carries a data snapshot (up to 14k chars)
    limit = int(prov.get("context_tokens") or 8192)
    return {"used": used, "limit": limit, "pct": round(used / limit, 3)}


def run_chat(ctx, prov: dict, key: str, history: list[dict], max_steps: int = 8, summary: str = "", use_tools: bool | None = None, extra: str = "") -> dict:
    tools = build_tools(ctx)
    by_name = {n: f for n, _, _, f in tools}
    system = system_prompt(ctx.name, ctx.today())
    if extra:
        system += "\nThe user linked these items to this chat. Treat them as what the conversation is about:\n" + extra
    if summary:
        system += "\nEarlier in this conversation (compacted summary):\n" + summary
    call = _call_anthropic if prov["kind"] == "anthropic" else _call_openai
    use_tools = bool(prov["tools"]) if use_tools is None else use_tools
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
                entry = {"tool": t["name"], "args": t["args"], "ok": "error" not in res if isinstance(res, dict) else True, "preview": text[:160]}
                if t["name"] in WRITE_TOOLS:
                    entry["result"] = res          # lets the UI offer an Undo
                trace.append(entry)
                msgs.append({"role": "tool", "tool_call_id": t["id"], "content": text})
    return {"reply": "I hit my step limit before finishing. Try a narrower question.", "trace": trace, "steps": max_steps}


# ------------------------------------------------------------------ discovering which models a server offers
def _origin(base_url: str) -> str:
    u = urlparse(base_url)
    return f"{u.scheme}://{u.netloc}"


def list_models(kind: str, base_url: str, key: str, transport=None) -> list[dict]:
    """Ask the server what it has. OpenAI-compatible servers answer GET /models (Ollama and LM Studio included);
    Anthropic answers GET /v1/models. Ollama's own /api/tags is the fallback. Returns [{id, context}]."""
    base = base_url.rstrip("/")
    with httpx.Client(timeout=15, transport=transport) as http:
        if kind == "anthropic":
            r = http.get(base + "/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, params={"limit": 1000})
            r.raise_for_status()
            return [{"id": m["id"], "context": m.get("max_input_tokens")} for m in r.json().get("data", [])]
        hdr = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            r = http.get(base + "/models", headers=hdr)
            r.raise_for_status()
            body = r.json()
            rows = body.get("data") or body.get("models") or []
            out = [{"id": m.get("id") or m.get("name"), "context": m.get("context_length") or m.get("max_context_length") or m.get("max_model_len") or m.get("context_window")} for m in rows if isinstance(m, dict) and (m.get("id") or m.get("name"))]
            if out:
                return out
        except (httpx.HTTPError, ValueError):
            pass
        r = http.get(_origin(base_url) + "/api/tags")                  # Ollama's native listing
        r.raise_for_status()
        return [{"id": m["name"], "context": None} for m in r.json().get("models", []) if m.get("name")]


def model_context(base_url: str, model: str, transport=None) -> dict:
    """The context window to use for this model on this server, preferring what the server itself says it is running.

    Ollama: the Modelfile preset (PARAMETER num_ctx) is what it really uses, so that wins; otherwise the model's maximum, capped at 32768
    because Ollama only uses that much when num_ctx is set. LM Studio: the loaded context. llama.cpp server: n_ctx (the server's own setting).
    Returns {"context_tokens": None} when the server cannot say."""
    origin = _origin(base_url)
    with httpx.Client(timeout=15, transport=transport) as http:
        try:
            r = http.post(origin + "/api/show", json={"model": model, "name": model})
            r.raise_for_status()
            body = r.json()
            m = re.search(r"^\s*num_ctx\s+(\d+)", body.get("parameters") or "", re.M)
            if m:
                return {"context_tokens": int(m.group(1)), "preset": True, "source": "the model's num_ctx preset (Ollama)"}
            mx = next((v for k, v in (body.get("model_info") or {}).items() if k.endswith(".context_length") and isinstance(v, int)), None)
            if mx:
                return {"context_tokens": min(mx, 32768), "model_max": mx, "preset": False, "source": "the model's maximum (Ollama only uses this much when num_ctx is set, so capped at 32768)"}
        except (httpx.HTTPError, ValueError):
            pass
        try:                                                    # LM Studio's native listing knows what is loaded
            r = http.get(origin + "/api/v0/models")
            r.raise_for_status()
            row = next((x for x in r.json().get("data", []) if x.get("id") == model), None)
            if row and (row.get("loaded_context_length") or row.get("max_context_length")):
                loaded = row.get("loaded_context_length")
                return {"context_tokens": int(loaded or row["max_context_length"]), "preset": bool(loaded), "model_max": row.get("max_context_length"),
                        "source": "the context LM Studio has loaded" if loaded else "the model's maximum (LM Studio)"}
        except (httpx.HTTPError, ValueError):
            pass
        try:                                                    # llama.cpp's server reports the context it was started with
            r = http.get(origin + "/props")
            r.raise_for_status()
            j = r.json()
            n = (j.get("default_generation_settings") or {}).get("n_ctx") or j.get("n_ctx")
            if n:
                return {"context_tokens": int(n), "preset": True, "source": "the server's own context size (llama.cpp n_ctx)"}
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    return {"context_tokens": None}


def auto_name(base_url: str, model: str) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    label = next((p["name"].split(" (")[0] for p in PRESETS if (urlparse(p["base_url"]).hostname or "") == host and urlparse(p["base_url"]).port == urlparse(base_url).port), None)
    return f"{label or host} · {model}"


# ------------------------------------------------------------------ routes
class ProviderIn(BaseModel):
    name: str = ""                      # optional: filled in from the address and model when blank
    kind: str = "openai"
    base_url: str
    model: str
    api_key: str | None = None          # omit = unchanged, "" = clear
    tools: bool = True
    allow_cloud: bool = False
    context_tokens: int | None = None   # the model's context window; chats compact before it fills. Blank = ask the server (its preset wins), else 8192


class ChatIn(BaseModel):
    messages: list[dict]
    provider_id: int | None = None


def register(app, ctx):
    store, auth = ctx.store, ctx.auth

    def view(p):
        return {"id": p["id"], "name": p["name"], "kind": p["kind"], "base_url": p["base_url"], "model": p["model"], "tools": bool(p["tools"]),
                "allow_cloud": bool(p["allow_cloud"]), "has_key": bool(p["key_enc"]), "local": is_local(p["base_url"]), "context_tokens": p["context_tokens"] or 8192}

    def check(p: ProviderIn):
        if p.kind not in ("openai", "anthropic"):
            raise HTTPException(422, "kind must be openai (any OpenAI-compatible server) or anthropic")
        if not p.model.strip():
            raise HTTPException(422, "pick or type a model")
        if p.context_tokens is not None and not 1024 <= p.context_tokens <= 4_000_000:
            raise HTTPException(422, "context_tokens must be between 1024 and 4000000")
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

    def context_for(p: ProviderIn, keep: int | None = None) -> int:
        """What you typed wins; otherwise the server's own preset for that model; otherwise what the provider had; otherwise 8192."""
        if p.context_tokens is not None:
            return p.context_tokens
        found = model_context(p.base_url, p.model.strip(), ctx.llm_transport).get("context_tokens") if p.kind == "openai" else None
        return found or keep or 8192

    @app.post("/api/agent/providers")
    def provider_add(request: Request, p: ProviderIn):
        auth(request)
        check(p)
        p.context_tokens = context_for(p)
        with store.tx() as c:
            cur = c.execute("INSERT INTO providers(name,kind,base_url,model,key_enc,tools,allow_cloud,context_tokens) VALUES(?,?,?,?,?,?,?,?)",
                            ((p.name.strip() or auto_name(p.base_url, p.model.strip())), p.kind, p.base_url.strip(), p.model.strip(), crypto.encrypt(p.api_key) if p.api_key else "", int(p.tools), int(p.allow_cloud), p.context_tokens))
        return view(store.one("SELECT * FROM providers WHERE id=?", (cur.lastrowid,)))

    @app.put("/api/agent/providers/{pid}")
    def provider_update(request: Request, pid: int, p: ProviderIn):
        auth(request)
        check(p)
        row = store.one("SELECT * FROM providers WHERE id=?", (pid,))
        if not row:
            raise HTTPException(404, "not found")
        p.context_tokens = context_for(p, row["context_tokens"])
        key = row["key_enc"] if p.api_key is None else (crypto.encrypt(p.api_key) if p.api_key else "")
        with store.tx() as c:
            c.execute("UPDATE providers SET name=?,kind=?,base_url=?,model=?,key_enc=?,tools=?,allow_cloud=?,context_tokens=? WHERE id=?",
                      ((p.name.strip() or auto_name(p.base_url, p.model.strip())), p.kind, p.base_url.strip(), p.model.strip(), key, int(p.tools), int(p.allow_cloud), p.context_tokens, pid))
        return view(store.one("SELECT * FROM providers WHERE id=?", (pid,)))

    @app.delete("/api/agent/providers/{pid}")
    def provider_delete(request: Request, pid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM providers WHERE id=?", (pid,))
        return {"deleted": pid}

    class Discover(BaseModel):
        kind: str = "openai"
        base_url: str
        api_key: str | None = None
        provider_id: int | None = None       # reuse a saved key instead of retyping it
        model: str | None = None

    def discover_key(b: Discover) -> str:
        if b.api_key:
            return b.api_key
        row = store.one("SELECT key_enc FROM providers WHERE id=?", (b.provider_id,)) if b.provider_id else None
        return crypto.decrypt(row["key_enc"]) if row and row["key_enc"] else ""

    @app.post("/api/agent/models")
    def models(request: Request, b: Discover):
        """The refresh button: what models does this server offer right now? Only your API key is sent, never your data."""
        auth(request)
        if b.kind not in ("openai", "anthropic") or urlparse(b.base_url).scheme not in ("http", "https") or not urlparse(b.base_url).hostname:
            raise HTTPException(422, "pick a server type and a valid address")
        try:
            found = list_models(b.kind, b.base_url, discover_key(b), ctx.llm_transport)
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"the server answered {e.response.status_code} when asked for its models" + (" (check the API key)" if e.response.status_code in (401, 403) else ""))
        except httpx.HTTPError as e:
            raise HTTPException(502, f"could not reach {b.base_url} ({type(e).__name__}). Is it running?")
        return {"models": sorted(found, key=lambda m: m["id"]), "local": is_local(b.base_url)}

    @app.post("/api/agent/model-info")
    def model_info(request: Request, b: Discover):
        auth(request)
        return model_context(b.base_url, b.model or "", ctx.llm_transport) if b.kind == "openai" else {"context_tokens": None}

    @app.get("/api/agent/tools")
    def tool_list(request: Request):
        auth(request)
        return [{"name": n, "description": d, "parameters": s} for n, d, s, _ in build_tools(ctx)]

    def pick_provider(pid):
        pid = pid or (int(cfg.get(store, "agent_provider")) if cfg.get(store, "agent_provider") else None)
        row = (store.one("SELECT * FROM providers WHERE id=?", (pid,)) if pid else None) or store.one("SELECT * FROM providers ORDER BY id LIMIT 1")
        if not row:
            raise HTTPException(409, "No model set up yet. Add one in Settings > Models.")
        local = is_local(row["base_url"])
        if not local and not row["allow_cloud"]:
            raise HTTPException(403, f"'{row['name']}' is not on your machine or network. Your financial data would be sent to {urlparse(row['base_url']).hostname}. Tick 'allow cloud' on that provider if you really want this.")
        return row, local

    def model_errors(row, fn):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"model server answered {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"could not reach the model at {row['base_url']} ({type(e).__name__}). Is it running?")

    def key_of(row):
        return crypto.decrypt(row["key_enc"]) if row["key_enc"] else ""

    # ------------------------------------------------------------ saved chats, with compaction
    def live(chat_id):
        return store.all("SELECT * FROM chat_messages WHERE chat_id=? AND compacted=0 ORDER BY id", (chat_id,))

    def link_context(chat_id) -> tuple[str, list[dict]]:
        """Resolve linked goals/notes to (text for the model, display rows for the UI). Deleted items just drop out."""
        links, text = [], []
        gv = None
        for l in store.all("SELECT * FROM chat_links WHERE chat_id=? ORDER BY rowid", (chat_id,)):
            if l["kind"] == "goal":
                gv = gv or plan.goals_view(store, ctx.today())
                g = next((x for x in gv["items"] if x["id"] == l["ref_id"]), None)
                if g:
                    links.append({"kind": "goal", "ref_id": g["id"], "title": g["name"]})
                    text.append(f"GOAL #{g['id']} {g['name']}: target ${g['target']:,.2f}, earmarked ${g['allocated']:,.2f}, still needs ${g['shortfall']:,.2f}"
                                + (f", deadline {g['deadline']}" if g.get("deadline") else "") + (f", planned ${g['monthly']:,.2f}/month" if g.get("monthly") else "") + f". Free savings overall: ${gv['free']:,.2f}.")
            elif l["kind"] == "note":
                n = store.one("SELECT * FROM notes WHERE id=?", (l["ref_id"],))
                if n:
                    links.append({"kind": "note", "ref_id": n["id"], "title": n["title"]})
                    text.append(f"NOTE '{n['title']}' (by {n['author']}):\n{n['body'][:2500]}")
        return "\n".join(text), links

    def chat_view(chat_id):
        chat = store.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not chat:
            raise HTTPException(404, "chat not found")
        msgs = store.all("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", (chat_id,))
        for m in msgs:
            m["trace"] = json.loads(m["trace"]) if m["trace"] else []
        prov = (store.one("SELECT * FROM providers WHERE id=?", (chat["provider_id"],)) if chat["provider_id"] else None) or store.one("SELECT * FROM providers ORDER BY id LIMIT 1")
        usage = context_usage(ctx, prov, chat["summary"], [m for m in msgs if not m["compacted"]], bool(prov["tools"]), link_context(chat_id)[0]) if prov else None
        return {"chat": chat, "messages": msgs, "usage": usage, "links": link_context(chat_id)[1]}

    def compact(chat, prov, key, keep=4) -> bool:
        rows = live(chat["id"])
        if len(rows) <= keep:
            return False
        old = rows[:-keep]
        text = "\n".join(f"{r['role'].upper()}: {r['content']}" for r in old)
        prompt = ("Summarize this conversation so you can continue it later. Keep every number, name, decision and open question. Under 250 words.\n\n"
                  + (f"Earlier summary:\n{chat['summary']}\n\n" if chat["summary"] else "") + text)
        call = _call_anthropic if prov["kind"] == "anthropic" else _call_openai
        with httpx.Client(timeout=300, transport=ctx.llm_transport) as http:
            out = call(http, prov, key, "You write concise, faithful conversation summaries.", [{"role": "user", "content": prompt}], None)
        summary = out["content"].strip()
        if not summary:
            return False
        with store.tx() as c:
            c.execute("UPDATE chats SET summary=? WHERE id=?", (summary[:6000], chat["id"]))
            c.execute(f"UPDATE chat_messages SET compacted=1 WHERE id IN ({','.join('?' * len(old))})", [r["id"] for r in old])
        return True

    @app.get("/api/chats")
    def chats(request: Request):
        auth(request)
        return store.all("SELECT c.id, c.title, c.updated, c.provider_id, (SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id=c.id) messages FROM chats c ORDER BY c.updated DESC")

    class NewChat(BaseModel):
        title: str | None = None
        links: list[dict] = []              # [{kind: goal|note, ref_id}] to attach right away, e.g. "discuss this goal"

    @app.post("/api/chats")
    def chat_new(request: Request, b: NewChat | None = None):
        auth(request)
        ts = sync.now()
        with store.tx() as c:
            cur = c.execute("INSERT INTO chats(title,created,updated) VALUES(?,?,?)", ((b.title or "New chat")[:80] if b else "New chat", ts, ts))
            for l in (b.links if b else []):
                if l.get("kind") in ("goal", "note") and isinstance(l.get("ref_id"), int):
                    c.execute("INSERT OR IGNORE INTO chat_links VALUES(?,?,?)", (cur.lastrowid, l["kind"], l["ref_id"]))
        return chat_view(cur.lastrowid)

    class LinkIn(BaseModel):
        kind: str
        ref_id: int

    @app.post("/api/chats/{cid}/links")
    def chat_link(request: Request, cid: int, b: LinkIn):
        auth(request)
        if b.kind not in ("goal", "note"):
            raise HTTPException(422, "kind must be goal or note")
        table = "goals" if b.kind == "goal" else "notes"
        if not store.one("SELECT 1 FROM chats WHERE id=?", (cid,)) or not store.one(f"SELECT 1 FROM {table} WHERE id=?", (b.ref_id,)):
            raise HTTPException(404, "chat or item not found")
        with store.tx() as c:
            c.execute("INSERT OR IGNORE INTO chat_links VALUES(?,?,?)", (cid, b.kind, b.ref_id))
        return chat_view(cid)

    @app.delete("/api/chats/{cid}/links/{kind}/{ref_id}")
    def chat_unlink(request: Request, cid: int, kind: str, ref_id: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM chat_links WHERE chat_id=? AND kind=? AND ref_id=?", (cid, kind, ref_id))
        return chat_view(cid)

    @app.get("/api/chats/{cid}")
    def chat_get(request: Request, cid: int):
        auth(request)
        return chat_view(cid)

    class Rename(BaseModel):
        title: str

    @app.put("/api/chats/{cid}")
    def chat_rename(request: Request, cid: int, b: Rename):
        auth(request)
        if not b.title.strip():
            raise HTTPException(422, "title required")
        with store.tx() as c:
            cur = c.execute("UPDATE chats SET title=? WHERE id=?", (b.title.strip()[:80], cid))
        if not cur.rowcount:
            raise HTTPException(404, "chat not found")
        return chat_view(cid)

    @app.delete("/api/chats/{cid}")
    def chat_delete(request: Request, cid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM chat_messages WHERE chat_id=?", (cid,))
            c.execute("DELETE FROM chat_links WHERE chat_id=?", (cid,))
            c.execute("DELETE FROM chats WHERE id=?", (cid,))
        return {"deleted": cid}

    class Say(BaseModel):
        content: str
        provider_id: int | None = None
        tools: bool | None = None            # per-chat switch: tool calling on/off for this message

    @app.post("/api/chats/{cid}/messages")
    def chat_say(request: Request, cid: int, b: Say):
        auth(request)
        chat = store.one("SELECT * FROM chats WHERE id=?", (cid,))
        if not chat:
            raise HTTPException(404, "chat not found")
        text = b.content.strip()
        if not text:
            raise HTTPException(422, "message is empty")
        row, local = pick_provider(b.provider_id or chat["provider_id"])
        key, use_tools = key_of(row), (bool(row["tools"]) if b.tools is None else b.tools)
        ts = sync.now()
        with store.tx() as c:
            mid = c.execute("INSERT INTO chat_messages(chat_id,role,content,created) VALUES(?,?,?,?)", (cid, "user", text, ts)).lastrowid
        compacted = False
        try:
            msgs = live(cid)
            extra = link_context(cid)[0]
            if context_usage(ctx, row, chat["summary"], msgs, use_tools, extra)["pct"] > 0.75 and len(msgs) > 5:       # make room before the model runs out
                compacted = model_errors(row, lambda: compact(chat, row, key))
                chat = store.one("SELECT * FROM chats WHERE id=?", (cid,))
            hist = [{"role": m["role"], "content": m["content"]} for m in live(cid)]
            out = model_errors(row, lambda: run_chat(ctx, row, key, hist[-40:], summary=chat["summary"], use_tools=use_tools, extra=extra))
        except Exception:
            with store.tx() as c:                      # a failed turn leaves no half-saved question behind
                c.execute("DELETE FROM chat_messages WHERE id=?", (mid,))
            raise
        with store.tx() as c:
            c.execute("INSERT INTO chat_messages(chat_id,role,content,trace,created) VALUES(?,?,?,?,?)", (cid, "assistant", out["reply"] or "(no answer)", json.dumps(out["trace"], default=str), sync.now()))
            c.execute("UPDATE chats SET updated=?, provider_id=?, title=CASE WHEN title='New chat' THEN ? ELSE title END WHERE id=?", (sync.now(), row["id"], text[:60], cid))
        view = chat_view(cid)
        return {**view, "compacted": compacted, "provider": {"id": row["id"], "name": row["name"], "model": row["model"], "local": local}, "tools_used": use_tools}

    @app.post("/api/chats/{cid}/compact")
    def chat_compact(request: Request, cid: int):
        auth(request)
        chat = store.one("SELECT * FROM chats WHERE id=?", (cid,))
        if not chat:
            raise HTTPException(404, "chat not found")
        row, _ = pick_provider(chat["provider_id"])
        done = model_errors(row, lambda: compact(chat, row, key_of(row), keep=2))
        return {**chat_view(cid), "compacted": done}

    @app.post("/api/agent/chat")
    def chat(request: Request, body: ChatIn):
        auth(request)
        row, local = pick_provider(body.provider_id)
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
