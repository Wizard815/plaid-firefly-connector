"""CoinGateway: routing, auth, webhook relay and the opt-in aggregate API.

It never stores Plaid tokens, balances or transactions. It only knows:
goblin name -> url + secrets, and plaid item_id -> goblin name.
"""
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx
import yaml
from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Request, Response

from goblinbank.security import Throttle, bearer, check_password_strength, check_token, hash_password, require_internal, sha256, verify_password


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class Metrics:
    """Counts and nothing else, in memory only, gone on restart. Per goblin and kind of call: how many, and how many answered 2xx, 4xx or 5xx.
    It never sees or keeps a path, a query string, a header, a body, a token or an address, so there is nothing personal to leak."""

    def __init__(self):
        self.started = time.time()
        self.n = defaultdict(lambda: {"calls": 0, "ok": 0, "client_errors": 0, "server_errors": 0})

    def record(self, goblin: str, kind: str, status: int):
        c = self.n[(goblin, kind)]
        c["calls"] += 1
        c["ok" if status < 400 else "client_errors" if status < 500 else "server_errors"] += 1

    def reset(self):
        self.n.clear()
        self.started = time.time()


KINDS = ("app", "api_token", "webhook", "orb")


def create_app(config: dict | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    for noisy in ("httpx", "httpcore", "uvicorn.access"):          # these log full URLs, query strings included; keep them quiet whatever the log level
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = config or load_config(os.environ.get("GATEWAY_CONFIG", "/config/gateway.yaml"))
    metrics = Metrics()
    goblins = {g["name"]: g for g in cfg.get("goblins", [])}
    orb_keys = list(cfg.get("orb_keys", []))                    # from gateway.yaml; keys made on the admin page are added to this same list (they are remembered in the state file)
    orb_added: list[dict] = []
    creds: dict[str, str] = {}                                  # passwords people set or changed in the app (scrypt hashes); they take over from gateway.yaml
    sessions: dict[str, tuple[str, float]] = {}                 # session token -> (name, expiry); memory only, so a restart signs everyone out
    throttle = Throttle()
    state_path = Path(os.environ.get("GATEWAY_STATE", cfg.get("state_file", "")) or "")
    policy: dict = {"default": {}, "people": {}}               # Plaid limits set on the admin page; goblins fetch theirs and obey (budget, daily call cap, how often to ask Plaid)
    added: dict[str, dict] = {}                               # people added from the admin page (name, address, secrets' hash): the same facts gateway.yaml holds
    if str(state_path) not in ("", "."):
        try:
            saved = json.loads(state_path.read_text())
            creds.update(saved.get("creds") or {})
            for k in saved.get("orb", []):
                orb_added.append(k)
                orb_keys.append({"sha256": k["sha256"], "scopes": ["summary:read"]})
            policy.update({k: v for k, v in (saved.get("policy") or {}).items() if k in policy})
            for g in saved.get("people", []):
                if g["name"] not in goblins:
                    goblins[g["name"]] = added[g["name"]] = g
        except (OSError, ValueError, KeyError):
            pass

    def save_state():
        if str(state_path) in ("", "."):
            raise HTTPException(409, "this gateway has no writable state folder; add people with goblins.yaml instead")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"people": list(added.values()), "policy": policy, "orb": orb_added, "creds": creds}))
        tmp.chmod(0o600)
        tmp.replace(state_path)
    items: dict[str, str] = {}
    client = httpx.Client(timeout=20, transport=transport)
    app = FastAPI(title="CoinGateway")

    @app.middleware("http")
    async def count(request: Request, call_next):
        """Tally the call by kind and goblin name only (the name comes from the path or the token's prefix, never anything else)."""
        resp = await call_next(request)
        path = request.url.path
        name, kind = "", None
        if path.startswith("/g/"):
            kind, name = "app", path.split("/")[2] if path.count("/") >= 2 else ""
        elif path.startswith("/api/v1/me"):
            parts = (bearer(request) or "").split("_", 2)
            kind, name = "api_token", parts[1] if len(parts) == 3 and parts[0] == "gb" else ""
        elif path == "/api/v1/summary":
            kind = "orb"
        elif path == "/webhooks/plaid":
            kind = "webhook"
        if kind:
            metrics.record(name if name in goblins else "", kind, resp.status_code)
        return resp

    def goblin_headers(g):
        return {"X-Internal-Secret": g["internal_secret"]}

    for n_, h_ in creds.items():                                # a password changed in the app wins over the file
        if n_ in goblins:
            goblins[n_]["user_password_scrypt"] = h_
            goblins[n_].pop("user_token_sha256", None)
    SESSION_SECONDS = 24 * 3600
    dummy = {}

    def password_matches(g: dict | None, presented: str) -> bool:
        """Slow scrypt for a chosen password; fast sha256 for a random token. An unknown name costs the same time as a known one."""
        if g and g.get("user_password_scrypt"):
            return verify_password(presented, g["user_password_scrypt"])
        if g:
            return check_token(presented, g.get("user_token_sha256", ""))
        dummy.setdefault("h", hash_password("not a real account"))
        verify_password(presented, dummy["h"])
        return False

    def new_session(name: str) -> str:
        now = time.time()
        for t in [t for t, (_, exp) in sessions.items() if exp < now]:
            del sessions[t]
        token = "sess_" + secrets.token_urlsafe(32)
        sessions[token] = (name, now + SESSION_SECONDS)
        return token

    def user_goblin(request: Request, name: str) -> dict:
        throttle.check("u:" + name)
        g, tok = goblins.get(name), bearer(request)
        sess = sessions.get(tok or "")
        # Same error for unknown goblin and wrong credential: no name enumeration.
        if g and tok and ((sess and sess[0] == name and sess[1] > time.time()) or (not g.get("user_password_scrypt") and check_token(tok, g.get("user_token_sha256", "")))):
            return g
        throttle.fail("u:" + name)
        raise HTTPException(401, "unauthorized")

    @app.post("/login")
    async def login(request: Request):
        """Name + password in, a session token out (use it as the bearer token). Wrong attempts are slowed down per name."""
        body = await request.json()
        name, pw = str(body.get("name", "")).strip().lower(), str(body.get("password", ""))
        throttle.check("u:" + name)
        g = goblins.get(name)
        if not await run_in_threadpool(password_matches, g, pw):
            throttle.fail("u:" + name)
            raise HTTPException(401, "wrong name or password")
        throttle.ok("u:" + name)
        return {"token": new_session(name)}

    @app.post("/account/password")
    async def change_password(request: Request):
        """Change your own password (the current one is required). Stored as a scrypt hash in the gateway's state folder."""
        body = await request.json()
        name, cur, new = str(body.get("name", "")).strip().lower(), str(body.get("current", "")), str(body.get("new", ""))
        throttle.check("u:" + name)
        g = goblins.get(name)
        if not await run_in_threadpool(password_matches, g, cur):
            throttle.fail("u:" + name)
            raise HTTPException(401, "the current password is wrong")
        try:
            check_password_strength(new, name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        h = await run_in_threadpool(hash_password, new)
        old = (creds.get(name), g.get("user_password_scrypt"), g.get("user_token_sha256"))
        creds[name] = g["user_password_scrypt"] = h
        g.pop("user_token_sha256", None)
        try:
            save_state()
        except HTTPException:
            if old[0] is None:
                creds.pop(name, None)
            else:
                creds[name] = old[0]
            g["user_password_scrypt"] = old[1]
            if old[2]:
                g["user_token_sha256"] = old[2]
            raise
        for t in [t for t, (n, _) in sessions.items() if n == name]:
            del sessions[t]
        throttle.ok("u:" + name)
        return {"token": new_session(name)}

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

    @app.get("/api/v1/me")
    @app.get("/api/v1/me/{resource:path}")
    async def me(request: Request, resource: str = ""):
        """For Home Assistant and scripts: Authorization: Bearer gb_<goblin>_<secret>. The token is checked inside that goblin."""
        tok = bearer(request) or ""
        parts = tok.split("_", 2)
        g = goblins.get(parts[1]) if len(parts) == 3 and parts[0] == "gb" else None
        if not g:
            raise HTTPException(401, "invalid token")           # same answer for an unknown goblin and a bad secret
        r = await run_in_threadpool(client.get, f"{g['url']}/external/{resource}", params=dict(request.query_params), headers={**goblin_headers(g), "X-API-Token": tok})
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

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

    @app.post("/internal/link/send")
    async def link_send(request: Request):
        """Relay one message from a goblin to another goblin it wants to link with. The gateway keeps nothing: it checks who is sending, stamps the
        sender's name on it (so nobody can pose as someone else) and passes it on. Whether the two are actually linked is the receiving goblin's call."""
        g = goblins.get(request.headers.get("x-goblin", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        body = await request.json()
        to = goblins.get(body.get("to", ""))
        if not to or to["name"] == g["name"] or not isinstance(body.get("payload", {}), dict):
            return {"delivered": False}
        try:
            r = await run_in_threadpool(client.post, f"{to['url']}/internal/link/inbox", json={"type": str(body.get("type", ""))[:20], "payload": body.get("payload", {}), "sender": g["name"]},
                                        headers=goblin_headers(to))
        except httpx.HTTPError:
            return {"delivered": False}
        return {"delivered": r.status_code == 200}

    POLICY_KEYS = {"plaid_budget_month": float, "plaid_daily_cap": int, "plaid_min_minutes": int, "plaid_use_recurring": str, "public_url": str, "plaid_client_id": str, "plaid_secret": str, "plaid_env": str, "plaid_redirect_url": str}

    def effective_policy(name: str) -> dict:
        return {**policy["default"], **policy["people"].get(name, {})}

    @app.get("/internal/policy")
    def goblin_policy(request: Request):
        """The limits this goblin must keep (budget, daily call cap, how often to ask Plaid). Set on the admin page; nothing personal."""
        g = goblins.get(request.headers.get("x-goblin", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        return effective_policy(g["name"])

    @app.get("/internal/link/people")
    def link_people(request: Request):
        """Names of the other goblins, for the 'link with…' picker. Names only."""
        g = goblins.get(request.headers.get("x-goblin", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        return {"people": sorted(n for n in goblins if n != g["name"])}

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

    # ---- admin: general health and counts, on demand. No route here reaches a goblin's data; the only call into a goblin is its /health.
    def admin(request: Request):
        throttle.check("admin")
        if not check_token(bearer(request), cfg.get("admin_token_sha256", "")):
            throttle.fail("admin")
            raise HTTPException(401, "unauthorized")
        throttle.ok("admin")

    @app.get("/admin/status")
    def admin_status(request: Request):
        admin(request)
        rows = []
        for n, g in goblins.items():
            try:
                up = client.get(f"{g['url']}/health", timeout=3).status_code == 200
            except httpx.HTTPError:
                up = False
            stats = {}
            if up:
                try:
                    r = client.get(f"{g['url']}/internal/plaid-stats", headers=goblin_headers(g), timeout=3)
                    stats = r.json() if r.status_code == 200 else {}
                except (httpx.HTTPError, ValueError):
                    stats = {}
            rows.append({"name": n, "up": up, "plaid": stats, "added_here": n in added, "calls": {k: dict(metrics.n[(n, k)]) for k in KINDS if (n, k) in metrics.n}})
        total = {k: sum(metrics.n[key][k] for key in metrics.n) for k in ("calls", "ok", "client_errors", "server_errors")}
        return {"goblins": rows, "orb": dict(metrics.n[("", "orb")]) if ("", "orb") in metrics.n else {}, "unmatched": {k: dict(v) for (n, k), v in metrics.n.items() if n == "" and k != "orb"},
                "total": total, "since_seconds": int(time.time() - metrics.started), "note": "Counts only. Nothing here shows what anyone asked for or any of their data."}

    def clean_policy(d: dict) -> dict:
        out = {}
        for k, v in (d or {}).items():
            if k not in POLICY_KEYS:
                raise HTTPException(422, f"unknown limit {k!r}")
            if v in ("", None):
                out[k] = ""                                    # empty = no limit (budget) or the built-in default
                continue
            try:
                if k in ("plaid_client_id", "plaid_secret", "plaid_env"):
                    t = str(v).strip()
                    if not re.fullmatch({"plaid_client_id": r"[A-Za-z0-9]{8,64}", "plaid_secret": r"[A-Za-z0-9_-]{8,128}", "plaid_env": r"sandbox|production"}[k], t):
                        raise ValueError
                    out[k] = t
                    continue
                if k in ("public_url", "plaid_redirect_url"):
                    u = str(v).strip().rstrip("/")
                    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?", u):
                        raise ValueError
                    out[k] = u
                    continue
                if k == "plaid_use_recurring":
                    if str(v) not in ("on", "off"):
                        raise ValueError
                    out[k] = str(v)
                else:
                    n = POLICY_KEYS[k](v)
                    if n < 0 or n > 100000:
                        raise ValueError
                    out[k] = int(n) if n == int(n) else n
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be " + ("an https:// address" if k in ("public_url", "plaid_redirect_url") else "your Plaid Client ID" if k == "plaid_client_id" else "your Plaid secret" if k == "plaid_secret" else "sandbox or production" if k == "plaid_env" else "on or off" if k == "plaid_use_recurring" else "a non-negative number"))
        return out

    def redacted() -> dict:
        """The policy without the Plaid secret: the admin page is told only whether one is saved."""
        d = {k: dict(v) if isinstance(v, dict) else v for k, v in policy.items()}
        d["default"] = {k: v for k, v in policy["default"].items() if k != "plaid_secret"}
        d["plaid_secret_set"] = bool(policy["default"].get("plaid_secret"))
        d["people"] = {n: {k: v for k, v in o.items() if k != "plaid_secret"} for n, o in policy["people"].items()}
        return d

    @app.get("/admin/policy")
    def admin_get_policy(request: Request):
        admin(request)
        return {**redacted(), "built_in": {"public_url": "", "plaid_budget_month": 5, "plaid_daily_cap": 300, "plaid_min_minutes": 0, "plaid_use_recurring": "on"}}

    @app.put("/admin/policy")
    async def admin_put_policy(request: Request):
        """Set the Plaid limits: one default for everyone plus optional per-person overrides. Empty budget = no limit."""
        admin(request)
        body = await request.json()
        new = {"default": clean_policy(body.get("default", {})), "people": {n: clean_policy(v) for n, v in (body.get("people") or {}).items() if n in goblins and v}}
        for k in ("plaid_client_id", "plaid_secret", "plaid_env", "plaid_redirect_url"):
            if k not in (body.get("default") or {}) and policy["default"].get(k):
                new["default"][k] = policy["default"][k]                                # saving the limits must not wipe the saved keys (send "" to clear one)
        old = dict(policy)
        policy.update(new)
        try:
            save_state()
        except HTTPException:
            policy.update(old)
            raise
        return redacted()

    @app.post("/admin/plaid/test")
    async def admin_plaid_test(request: Request):
        """Try the saved Plaid keys with a free call."""
        admin(request)
        d = policy["default"]
        if not (d.get("plaid_client_id") and d.get("plaid_secret")):
            raise HTTPException(409, "save a Client ID and secret first")
        base = {"production": "https://production.plaid.com"}.get(d.get("plaid_env"), "https://sandbox.plaid.com")
        try:
            r = await run_in_threadpool(client.post, base + "/institutions/get", json={"count": 1, "offset": 0, "country_codes": ["US"]},
                                        headers={"PLAID-CLIENT-ID": d["plaid_client_id"], "PLAID-SECRET": d["plaid_secret"], "Plaid-Version": "2020-09-14"})
        except httpx.HTTPError:
            raise HTTPException(502, "could not reach Plaid")
        if r.status_code != 200:
            try:
                e = r.json()
                raise HTTPException(502, f"{e.get('error_code')}: {e.get('error_message')}")
            except ValueError:
                raise HTTPException(502, f"Plaid answered {r.status_code}")
        return {"ok": True, "env": d.get("plaid_env") or "sandbox"}

    @app.get("/admin/orb")
    def admin_orb_list(request: Request):
        """Pondering Orb keys made here (name, id, date; never the key itself) and how many come from the config file."""
        admin(request)
        return {"keys": [{"id": k["id"], "name": k["name"], "created": k["created"]} for k in orb_added], "from_config": len(cfg.get("orb_keys", []))}

    @app.post("/admin/orb/key")
    async def admin_orb_create(request: Request):
        """Make a read-only key for Pondering Orb. Shown once; only its hash is kept. The Orb sees only what each person ticked under Settings, Sharing."""
        admin(request)
        name = str((await request.json()).get("name", "") or "Pondering Orb").strip()[:60]
        key = "orb_" + secrets.token_urlsafe(32)
        entry = {"id": sha256(key)[:10], "name": name, "created": time.strftime("%Y-%m-%d"), "sha256": sha256(key)}
        orb_added.append(entry)
        orb_keys.append({"sha256": entry["sha256"], "scopes": ["summary:read"]})
        try:
            save_state()
        except HTTPException:
            orb_added.pop(); orb_keys.pop()
            raise
        return {"id": entry["id"], "name": name, "key": key}

    @app.delete("/admin/orb/key/{kid}")
    def admin_orb_delete(request: Request, kid: str):
        admin(request)
        hit = next((k for k in orb_added if k["id"] == kid), None)
        if not hit:
            raise HTTPException(404, "not found")
        orb_added.remove(hit)
        orb_keys[:] = [k for k in orb_keys if k["sha256"] != hit["sha256"]]
        save_state()
        return {"removed": kid}

    @app.post("/admin/people")
    async def admin_add_person(request: Request):
        """Register a new person without touching any yaml. Returns their login token and internal secret once; only the token's hash is kept."""
        admin(request)
        body = await request.json()
        name = str(body.get("name", "")).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
            raise HTTPException(422, "a name is lowercase letters, digits and hyphens, starting with a letter")
        if name in goblins:
            raise HTTPException(409, "that name is taken")
        password = str(body.get("password", "") or "")
        token, internal = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        entry = {"name": name, "url": f"http://goblin-{name}:8000", "internal_secret": internal}
        if password:                                             # the person's own password: only its scrypt hash is kept
            try:
                check_password_strength(password, name)
            except ValueError as e:
                raise HTTPException(422, str(e))
            entry["user_password_scrypt"] = await run_in_threadpool(hash_password, password)
            token = ""
        else:
            entry["user_token_sha256"] = sha256(token)
        goblins[name] = added[name] = entry
        try:
            save_state()
        except HTTPException:
            del goblins[name], added[name]
            raise
        return {"name": name, "login_token": token, "password_set": bool(password), "internal_secret": internal, "service": f"goblin-{name}"}

    @app.delete("/admin/people/{name}")
    def admin_remove_person(request: Request, name: str):
        admin(request)
        if name not in added:
            raise HTTPException(404, "only people added on this page can be removed here")
        del goblins[name], added[name]
        save_state()
        return {"removed": name}

    @app.post("/admin/reset")
    def admin_reset(request: Request):
        admin(request)
        metrics.reset()
        return {"reset": True}

    @app.get("/admin")
    def admin_page():
        f = ui / "admin.html"
        if not f.exists():
            raise HTTPException(404, "admin page not installed")
        return Response(f.read_text(), media_type="text/html")

    ui = Path(os.environ.get("UI_DIR", Path(__file__).resolve().parents[2] / "ui"))

    @app.get("/")
    def index():
        f = ui / "index.html"
        if not f.exists():
            raise HTTPException(404, "ui not installed")
        return Response(f.read_text(), media_type="text/html")

    return app
