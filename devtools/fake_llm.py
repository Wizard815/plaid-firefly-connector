"""Scripted stand-ins for model servers (OpenAI-compatible and Anthropic), as httpx MockTransports.
They are not models: they follow a script of tool calls, then answer from what the tools returned. That tests everything
around the model (tool schemas, execution, message formats, loop, guards), not the model's intelligence."""
import json

import httpx


def openai_server(script: list[list[tuple[str, dict]]], log: list):
    """script[i] = tool calls to make on request i; once exhausted, reply with the last tool results."""
    def handler(req: httpx.Request):
        if not req.url.path.endswith("/chat/completions"):          # model discovery calls (context detection) are not chats
            return httpx.Response(404)
        body = json.loads(req.content)
        log.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": body})
        if body["messages"][0]["content"].startswith("You write concise"):      # the compaction call
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "SUMMARY: " + body["messages"][-1]["content"][-300:]}}]})
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if body.get("tools") and n < len(script):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{n}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(script[n])]}}]})
        tools = [m["content"] for m in body["messages"] if m["role"] == "tool"]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "RESULTS:" + "||".join(tools)[:3000] if tools else "no tools used"}}]})
    return httpx.MockTransport(handler)


def anthropic_server(script, log):
    def handler(req: httpx.Request):
        if not req.url.path.endswith("/v1/messages"):
            return httpx.Response(404)
        body = json.loads(req.content)
        log.append({"url": str(req.url), "key": req.headers.get("x-api-key"), "version": req.headers.get("anthropic-version"), "body": body})
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if body.get("tools") and n < len(script):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "checking"}] + [
                {"type": "tool_use", "id": f"tu_{n}_{i}", "name": name, "input": args} for i, (name, args) in enumerate(script[n])]})
        results = [b["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "RESULTS:" + "||".join(results)[:3000]}]})
    return httpx.MockTransport(handler)
