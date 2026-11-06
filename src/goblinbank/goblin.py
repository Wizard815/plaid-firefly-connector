"""A Goblin: one person's private bank service. Holds that person's data only."""
import os

import httpx
from fastapi import FastAPI, HTTPException, Request

from goblinbank.security import require_internal

SHAREABLE = {"robinhood_agent", "robinhood_main"}


def create_app(transport: httpx.BaseTransport | None = None) -> FastAPI:
    name = os.environ.get("GOBLIN_NAME", "goblin")
    secret = os.environ.get("INTERNAL_SECRET", "")
    connector_secret = os.environ.get("CONNECTOR_SECRET", secret)
    urls = {"agent": os.environ.get("RH_AGENT_URL", ""), "csv": os.environ.get("RH_CSV_URL", ""),
            "live": os.environ.get("RH_LIVE_URL", "")}
    share = {s for s in os.environ.get("SHARE", "").split(",") if s in SHAREABLE}
    gateway_url = os.environ.get("GATEWAY_URL", "")
    client = httpx.Client(timeout=20, transport=transport)
    app = FastAPI(title=f"goblin:{name}")

    def fetch(kind: str):
        base = urls[kind]
        if not base:
            return None
        out = {}
        for ep in ("balances", "positions", "history"):
            try:
                r = client.get(f"{base}/{ep}", headers={"X-Internal-Secret": connector_secret})
                r.raise_for_status()
                j = r.json()
                out[ep] = j["data"]
                out["as_of"], out["source"] = j["as_of"], j["source"]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                out[ep] = None
                out.setdefault("errors", []).append(f"{ep}: {type(e).__name__}")
        return out

    def main_portfolio():
        # Live is preferred when enabled and answering; CSV is the fallback.
        live = fetch("live")
        if live and live.get("positions") is not None:
            return live
        return fetch("csv")

    @app.get("/health")
    def health():
        return {"ok": True, "goblin": name}

    @app.get("/robinhood/main")
    def rh_main(request: Request):
        require_internal(request, secret)
        return main_portfolio() or {"error": "no main-account connector configured"}

    @app.get("/robinhood/agent")
    def rh_agent(request: Request):
        require_internal(request, secret)
        return fetch("agent") or {"error": "rh-agent not configured"}

    @app.get("/summary")
    def summary(request: Request, scope: str = "shared"):
        """scope=shared -> only what this person opted to expose (gateway/orb use this)."""
        require_internal(request, secret)
        out = {"goblin": name}
        if "robinhood_main" in share or scope == "all":
            m = main_portfolio()
            out["robinhood_main"] = m and {"as_of": m.get("as_of"), "source": m.get("source"), "balances": m.get("balances")}
        if "robinhood_agent" in share or scope == "all":
            a = fetch("agent")
            out["robinhood_agent"] = a and {"as_of": a.get("as_of"), "balances": a.get("balances")}
        return out

    @app.post("/plaid/webhook")
    async def plaid_webhook(request: Request):
        require_internal(request, secret)
        body = await request.json()
        # TODO: trigger a transactions sync for body["item_id"] once the Plaid connector exists.
        return {"received": body.get("webhook_code")}

    @app.post("/plaid/register-item")
    def register_item(request: Request, item_id: str):
        """Tell the gateway which item belongs to us (item_id only, never tokens)."""
        require_internal(request, secret)
        if not gateway_url:
            raise HTTPException(503, "no gateway configured")
        r = client.post(f"{gateway_url}/internal/items", json={"item_id": item_id},
                        headers={"X-Internal-Secret": secret, "X-Goblin": name})
        r.raise_for_status()
        return {"registered": item_id}

    return app
