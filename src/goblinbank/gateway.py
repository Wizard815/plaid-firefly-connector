"""CoinGateway: routing, auth, webhook relay and the opt-in aggregate API.

It never stores Plaid tokens, balances or transactions. It only knows:
goblin name -> url + secrets, and plaid item_id -> goblin name.
"""
import os

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Response

from goblinbank.security import bearer, check_token, require_internal


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def create_app(config: dict | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    cfg = config or load_config(os.environ.get("GATEWAY_CONFIG", "/config/gateway.yaml"))
    goblins = {g["name"]: g for g in cfg.get("goblins", [])}
    orb_keys = cfg.get("orb_keys", [])
    items: dict[str, str] = {}
    client = httpx.Client(timeout=20, transport=transport)
    app = FastAPI(title="CoinGateway")

    def goblin_headers(g):
        return {"X-Internal-Secret": g["internal_secret"]}

    def user_goblin(request: Request, name: str) -> dict:
        g = goblins.get(name)
        # Same error for unknown goblin and wrong token: no name enumeration.
        if not g or not check_token(bearer(request), g["user_token_sha256"]):
            raise HTTPException(401, "unauthorized")
        return g

    @app.get("/health")
    def health():
        return {"ok": True, "goblins": sorted(goblins)}

    @app.api_route("/g/{name}/{path:path}", methods=["GET", "POST"])
    async def proxy(name: str, path: str, request: Request):
        """A signed-in person reaches only their own goblin."""
        g = user_goblin(request, name)
        r = client.request(request.method, f"{g['url']}/{path}", params=dict(request.query_params),
                           content=await request.body(), headers=goblin_headers(g))
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.get("/api/v1/summary")
    def aggregate(request: Request):
        """For Pondering Orb: read-only, scoped key, only data each goblin opted to share."""
        key = bearer(request)
        ok = [k for k in orb_keys if check_token(key, k["sha256"])]
        if not ok or "summary:read" not in ok[0].get("scopes", []):
            raise HTTPException(401, "unauthorized")
        out = {}
        for name, g in goblins.items():
            try:
                r = client.get(f"{g['url']}/summary", params={"scope": "shared"}, headers=goblin_headers(g))
                r.raise_for_status()
                out[name] = r.json()
            except httpx.HTTPError:
                out[name] = {"error": "unavailable"}
        return {"goblins": out}

    @app.post("/internal/items")
    async def register_item(request: Request):
        g = goblins.get(request.headers.get("x-goblin", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        item_id = (await request.json()).get("item_id")
        if not item_id:
            raise HTTPException(422, "item_id required")
        items[item_id] = g["name"]
        return {"ok": True}

    @app.post("/webhooks/plaid")
    async def plaid_webhook(request: Request):
        # Plaid signs webhooks with a JWT (Plaid-Verification header). Verification is not
        # implemented yet, so this FAILS CLOSED unless explicitly overridden for local testing.
        if os.environ.get("GATEWAY_INSECURE_WEBHOOKS") != "1":
            raise HTTPException(501, "plaid webhook signature verification not implemented")
        body = await request.json()
        name = items.get(body.get("item_id", ""))
        if not name:
            return {"ignored": True}
        g = goblins[name]
        client.post(f"{g['url']}/plaid/webhook", json=body, headers=goblin_headers(g))
        return {"relayed_to": name}

    return app
