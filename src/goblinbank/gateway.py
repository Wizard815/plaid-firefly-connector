"""CoinGateway: routing, auth, webhook relay and the opt-in aggregate API.

It never stores Plaid tokens, balances or transactions. It only knows:
goblin name -> url + secrets, and plaid item_id -> goblin name.
"""
import json
import os
from pathlib import Path

import httpx
import yaml
from fastapi.concurrency import run_in_threadpool
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
        return {"ok": True}

    @app.api_route("/g/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(name: str, path: str, request: Request):
        """A signed-in person reaches only their own goblin."""
        g = user_goblin(request, name)
        body = await request.body()
        # Blocking call goes to a worker thread: the goblin may call back into this gateway
        # (item registration) while we wait, and a blocked event loop would deadlock.
        r = await run_in_threadpool(client.request, request.method, f"{g['url']}/{path}", params=dict(request.query_params), content=body,
                                    headers={**goblin_headers(g), "Content-Type": request.headers.get("content-type", "application/octet-stream")})
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
        """Relay to the owning goblin. The goblin verifies Plaid's JWT signature (it holds the
        Plaid credentials); routing on the unverified item_id is harmless because nothing acts
        until that verification passes."""
        raw = await request.body()
        try:
            item_id = json.loads(raw).get("item_id", "")
        except ValueError:
            raise HTTPException(400, "bad json")
        name = items.get(item_id)
        if not name:
            return {"ignored": True}
        g = goblins[name]
        r = await run_in_threadpool(client.post, f"{g['url']}/plaid/webhook", content=raw,
                                    headers={**goblin_headers(g), "Plaid-Verification": request.headers.get("plaid-verification", ""),
                                             "Content-Type": "application/json"})
        return Response(r.content, r.status_code, media_type="application/json")

    ui = Path(os.environ.get("UI_DIR", Path(__file__).resolve().parents[2] / "ui"))

    @app.get("/")
    def index():
        f = ui / "index.html"
        if not f.exists():
            raise HTTPException(404, "ui not installed")
        return Response(f.read_text(), media_type="text/html")

    return app
