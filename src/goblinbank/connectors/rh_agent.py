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

import httpx


class AgentConnector:
    name = "rh-agent"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.url = os.environ.get("RH_MCP_URL", "")
        self.token = os.environ.get("RH_MCP_TOKEN", "")
        self.allow = {t.strip() for t in os.environ.get("RH_MCP_READ_TOOLS", "").split(",") if t.strip()}
        self.map = {k: os.environ.get(f"RH_MCP_TOOL_{k.upper()}", "") for k in ("balances", "positions", "history")}
        self._client = httpx.Client(timeout=15, transport=transport)
        self._id = 0

    def _call(self, kind: str):
        tool = self.map[kind]
        if not self.url or not tool:
            raise PermissionError(f"rh-agent not configured for {kind}")
        if tool not in self.allow:
            raise PermissionError(f"tool '{tool}' is not in RH_MCP_READ_TOOLS")
        self._id += 1
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = self._client.post(self.url, headers=headers, json={
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
