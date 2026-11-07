"""An OpenAI-compatible HTTP server that behaves like a small tool-calling model: picks tools from keywords in the
question, then answers from what they returned. For click-through tests of the whole path (UI -> gateway -> goblin -> model server)."""
import json

from fastapi import FastAPI, Request

app = FastAPI()
ROUTES = [(("lowest", "balance", "short", "afford"), [("project_balance", {"days": 30})]), (("recurring", "subscription"), [("find_recurring_payments", {})]),
          (("cd ", "matur", "saving"), [("savings_summary", {})]), (("calendar",), [("calendar_events", {"days_ahead": 14})]),
          (("food", "gas", "budget", "spent"), [("budget_status", {}), ("spending_by_category", {})]), (("plan", "note"), [("write_note", {"title": "Plan from the agent", "body": "Sample plan. Cut dining out, move $200 to savings."})]),
          (("bill", "pay", "due"), [("upcoming_bills", {"days": 60}), ("calculate", {"expression": "1200 + 300"})])]


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    msgs = body["messages"]
    done = any(m["role"] == "tool" for m in msgs)
    if body.get("tools") and not done:
        q = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "").lower() + " "
        calls = next((c for keys, c in ROUTES if any(k in q for k in keys)), [("get_overview", {})])
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]}}]}
    outs = [json.loads(m["content"]) for m in msgs if m["role"] == "tool"]
    bits = []
    for o in outs:
        if isinstance(o, dict):
            if "total" in o and "count" in o: bits.append(f"{o['count']} bills totalling ${o['total']:,.2f} in the next {o['days']} days")
            elif "lowest" in o: bits.append(f"cash starts at ${o['start']:,.2f}, lowest ${o['lowest']:,.2f} on {o['lowest_date']}, ends near ${o['end']:,.2f} (estimate)")
            elif "result" in o: bits.append(f"the sum is {o['result']}")
            elif o.get("saved"): bits.append("I saved a note in your Planner")
            elif "net_worth" in o: bits.append(f"net worth is ${o['net_worth']:,.2f}")
            elif "weighted_apy" in o: bits.append(f"you have ${o['total']:,.2f} saved at {o['weighted_apy']}% average APY")
        elif isinstance(o, list): bits.append(f"found {len(o)} items")
    return {"choices": [{"message": {"role": "assistant", "content": "Here is what I found: " + "; ".join(bits) + "." if bits else "Done."}}]}
