"""rh-agent: Robinhood official MCP (Agentic Trading) -- READ TOOLS ONLY.

I could not verify Robinhood's MCP auth or tool names (the announcement does not
document them), so everything is configuration, and the connector FAILS CLOSED:
only tools you list in RH_MCP_READ_TOOLS can ever be called. Trade tools are
never reachable because they are not in the allowlist.

Env:
  RH_MCP_URL            MCP server URL (streamable HTTP / JSON-RPC)
  RH_MCP_TOKEN          bearer token, if the server uses one
  RH_MCP_READ_TOOLS     comma list of allowed tool names
  RH_MCP_TOOL_BALANCES / _POSITIONS / _HISTORY   which allowed tool serves each read
"""
import os
import time

import httpx


class AgentConnector:
    name = "rh-agent"

    def __init__(self, transport: httpx.BaseTransport | None = None, config_url: str = "", config_secret: str = ""):
        self._client = httpx.Client(timeout=15, transport=transport)
        self._id = 0
        self._config_url, self._config_secret = config_url, config_secret
        self._cached: tuple[float, dict] | None = None

    def _config(self) -> dict:
        """Env wins when RH_MCP_URL is set; otherwise ask the goblin (Settings page), cached 30s."""
        if os.environ.get("RH_MCP_URL") or not self._config_url:
            return {"url": os.environ.get("RH_MCP_URL", ""), "token": os.environ.get("RH_MCP_TOKEN", ""),
                    "read_tools": os.environ.get("RH_MCP_READ_TOOLS", ""),
                    **{k: os.environ.get(f"RH_MCP_TOOL_{k.upper()}", "") for k in ("balances", "positions", "history")}}
        now = time.monotonic()
        if self._cached and now - self._cached[0] < 30:
            return self._cached[1]
        r = self._client.get(self._config_url, headers={"X-Internal-Secret": self._config_secret})
        r.raise_for_status()
        self._cached = (now, r.json())
        return self._cached[1]

    def _call(self, kind: str):
        cfg = self._config()
        allow = {t.strip() for t in cfg.get("read_tools", "").split(",") if t.strip()}
        tool, url, token = cfg.get(kind, ""), cfg.get("url", ""), cfg.get("token", "")
        if not url or not tool:
            raise PermissionError(f"rh-agent not configured for {kind}")
        if tool not in allow:
            raise PermissionError(f"tool '{tool}' is not in the allowed read tools")
        self._id += 1
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self._client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body.get("result")

    def balances(self): return self._call("balances")
    def positions(self): return self._call("positions")
    def history(self): return self._call("history")
