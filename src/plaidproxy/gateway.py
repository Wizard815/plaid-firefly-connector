"""CoinGateway: routing, auth, webhook relay and the opt-in aggregate API.

It never stores Plaid tokens, balances or transactions. It only knows:
worker name -> url + secrets, and plaid item_id -> worker name.
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
from fastapi.responses import RedirectResponse

from plaidproxy.oplog import log
from plaidproxy.security import Throttle, bearer, check_password_strength, check_token, hash_password, require_internal, sha256, verify_password


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class Metrics:
    """Counts and nothing else, in memory only, gone on restart. Per worker and kind of call: how many, and how many answered 2xx, 4xx or 5xx.
    It never sees or keeps a path, a query string, a header, a body, a token or an address, so there is nothing personal to leak."""

    def __init__(self):
        self.started = time.time()
        self.n = defaultdict(lambda: {"calls": 0, "ok": 0, "client_errors": 0, "server_errors": 0})

    def record(self, worker: str, kind: str, status: int):
        c = self.n[(worker, kind)]
        c["calls"] += 1
        c["ok" if status < 400 else "client_errors" if status < 500 else "server_errors"] += 1

    def reset(self):
        self.n.clear()
        self.started = time.time()


KINDS = ("app", "api_token", "webhook", "orb")


def create_app(config: dict | None = None, transport: httpx.BaseTransport | None = None, monitor: bool = False) -> FastAPI:
    for noisy in ("httpx", "httpcore", "uvicorn.access"):          # these log full URLs, query strings included; keep them quiet whatever the log level
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = config or load_config(os.environ.get("GATEWAY_CONFIG", "/config/gateway.yaml"))
    metrics = Metrics()
    workers = {g["name"]: g for g in cfg.get("people", [])}
    orb_keys = list(cfg.get("orb_keys", []))                    # from gateway.yaml; keys made on the admin page are added to this same list (they are remembered in the state file)
    orb_added: list[dict] = []
    creds: dict[str, str] = {}                                  # passwords people set or changed in the app (scrypt hashes); they take over from gateway.yaml
    removed: set[str] = set()                                  # names of people removed on the admin page: never given to someone new
    invites: dict[str, dict] = {}                               # temporary link codes the admin hands to a person (hash -> who, until when); memory only
    link_sessions: dict[str, tuple[str, float]] = {}           # what a code turns into: a token that can only link that person's banks, until the code runs out
    invite_throttle = Throttle()
    sessions: dict[str, tuple[str, float]] = {}                 # session token -> (name, expiry); memory only, so a restart signs everyone out
    throttle = Throttle()
    failed = {"sign_in": 0, "locked": 0}                       # counts only, for the heartbeat line

    def note_failure(key: str) -> None:
        throttle.fail(key)
        failed["sign_in"] += 1
        if throttle.wait_for(key) > 0:
            failed["locked"] += 1
            log.warning("%s sign-in is locked for %d seconds after repeated wrong attempts", "the admin" if key == "admin" else "a", int(throttle.wait_for(key)) + 1)
    state_path = Path(os.environ.get("GATEWAY_STATE", cfg.get("state_file", "")) or "")
    policy: dict = {"default": {}, "people": {}}               # Plaid limits set on the admin page; workers fetch theirs and obey (budget, daily call cap, how often to ask Plaid)
    added: dict[str, dict] = {}                               # people added from the admin page (name, address, secrets' hash): the same facts gateway.yaml holds
    if str(state_path) not in ("", "."):
        try:
            saved = json.loads(state_path.read_text())
            creds.update(saved.get("creds") or {})
            removed.update(saved.get("removed") or [])
            for k in saved.get("orb", []):
                orb_added.append(k)
                orb_keys.append({"sha256": k["sha256"], "scopes": ["summary:read"]})
            policy.update({k: v for k, v in (saved.get("policy") or {}).items() if k in policy})
            for g in saved.get("people", []):
                if g["name"] not in workers:
                    workers[g["name"]] = added[g["name"]] = g
        except (OSError, ValueError, KeyError):
            pass

    def save_state():
        if str(state_path) in ("", "."):
            raise HTTPException(409, "this gateway has no writable state folder; add people with people.yaml instead")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)        # owner-only from the first byte: it holds the Plaid secret and Firefly III tokens
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"people": list(added.values()), "policy": policy, "orb": orb_added, "creds": creds, "removed": sorted(removed)}))
        tmp.replace(state_path)
    items: dict[str, str] = {}
    client = httpx.Client(timeout=20, transport=transport)
    app = FastAPI(title="CoinGateway")

    @app.middleware("http")
    async def count(request: Request, call_next):
        """Tally the call by kind and worker name only (the name comes from the path or the token's prefix, never anything else)."""
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
            metrics.record(name if name in workers else "", kind, resp.status_code)
        return resp

    def worker_headers(g):
        return {"X-Internal-Secret": g["internal_secret"]}

    for n_, h_ in creds.items():                                # a password changed in the app wins over the file
        if n_ in workers:
            workers[n_]["user_password_scrypt"] = h_
            workers[n_].pop("user_token_sha256", None)
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

    def user_worker(request: Request, name: str) -> dict:
        throttle.check("u:" + name)
        g, tok = workers.get(name), bearer(request)
        sess = sessions.get(tok or "")
        # Same error for unknown worker and wrong credential: no name enumeration.
        if g and tok and ((sess and sess[0] == name and sess[1] > time.time()) or (not g.get("user_password_scrypt") and check_token(tok, g.get("user_token_sha256", "")))):
            return g
        note_failure("u:" + name)
        raise HTTPException(401, "unauthorized")

    @app.post("/login")
    async def login(request: Request):
        """Name + password in, a session token out (use it as the bearer token). Wrong attempts are slowed down per name."""
        body = await request.json()
        name, pw = str(body.get("name", "")).strip().lower(), str(body.get("password", ""))
        throttle.check("u:" + name)
        g = workers.get(name)
        if not await run_in_threadpool(password_matches, g, pw):
            note_failure("u:" + name)
            raise HTTPException(401, "wrong name or password")
        throttle.ok("u:" + name)
        return {"token": new_session(name)}

    @app.post("/account/password")
    async def change_password(request: Request):
        """Change your own password (the current one is required). Stored as a scrypt hash in the gateway's state folder."""
        body = await request.json()
        name, cur, new = str(body.get("name", "")).strip().lower(), str(body.get("current", "")), str(body.get("new", ""))
        throttle.check("u:" + name)
        g = workers.get(name)
        if not await run_in_threadpool(password_matches, g, cur):
            note_failure("u:" + name)
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

    # The admin's decisions: which bank accounts go to which Firefly III account, joint-account shares, and where a person's data is sent.
    # A person's own login may look at these but not change them (the admin page reaches them through /admin/p/...).
    ADMIN_ONLY = [("PUT", r"api/firefly/accounts/[^/]+"), ("POST", r"api/firefly/accounts/[^/]+/rebalance"), ("PUT", r"api/shares/[^/]+")]

    def clean_path(path: str) -> str:
        """The one spelling of a path that is both checked and forwarded. httpx would drop "." and ".." segments when it builds the URL,
        so a check made on the raw text could be stepped round ("./internal/..."); such paths are refused instead of normalised."""
        p = path.strip("/")
        segs = p.split("/")
        if not p or any(s in ("", ".", "..") or "\\" in s or any(ord(ch) < 32 for ch in s) for s in segs):
            raise HTTPException(404, "not found")
        return p

    @app.api_route("/g/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(name: str, path: str, request: Request):
        """A signed-in person reaches only their own worker."""
        g = user_worker(request, name)
        body = await request.body()
        p = clean_path(path)
        if p.startswith(("internal/", "external/")) or p in ("internal", "external"):
            raise HTTPException(404, "not found")                  # the gateway's own channel to the worker, never a person's
        if any(request.method == m and re.fullmatch(rx, p) for m, rx in ADMIN_ONLY):
            raise HTTPException(403, "only the admin can change this (the admin page)")
        if request.method in ("PUT", "POST") and p == "api/settings":
            try:
                keys = set((json.loads(body or b"{}") or {}).keys())
            except (ValueError, AttributeError):
                raise HTTPException(422, "expected an object")
            if any(k.startswith("firefly_") for k in keys):
                raise HTTPException(403, "where your data is sent in Firefly III is set by the admin")
        # Blocking call goes to a worker thread: the worker may call back into this gateway
        # (item registration) while we wait, and a blocked event loop would deadlock.
        r = await run_in_threadpool(client.request, request.method, f"{g['url']}/{p}", params=dict(request.query_params), content=body,
                                    headers={**worker_headers(g), "Content-Type": request.headers.get("content-type", "application/octet-stream")})
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.get("/api/v1/summary")
    def aggregate(request: Request):
        """For Pondering Orb: read-only, scoped key, only data each worker opted to share."""
        key = bearer(request)
        ok = [k for k in orb_keys if check_token(key, k["sha256"])]
        if not ok or "summary:read" not in ok[0].get("scopes", []):
            raise HTTPException(401, "unauthorized")
        out = {}
        for name, g in workers.items():
            try:
                r = client.get(f"{g['url']}/summary", params={"scope": "shared"}, headers=worker_headers(g))
                r.raise_for_status()
                out[name] = r.json()
            except httpx.HTTPError:
                out[name] = {"error": "unavailable"}
        return {"goblins": out}

    @app.get("/api/v1/me")
    @app.get("/api/v1/me/{resource:path}")
    async def me(request: Request, resource: str = ""):
        """For Home Assistant and scripts: Authorization: Bearer gb_<worker>_<secret>. The token is checked inside that worker."""
        tok = bearer(request) or ""
        parts = tok.split("_", 2)
        g = workers.get(parts[1]) if len(parts) == 3 and parts[0] == "gb" else None
        if not g:
            raise HTTPException(401, "invalid token")           # same answer for an unknown worker and a bad secret
        r = await run_in_threadpool(client.get, f"{g['url']}/external/{resource}", params=dict(request.query_params), headers={**worker_headers(g), "X-API-Token": tok})
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.post("/internal/items")
    async def register_item(request: Request):
        g = workers.get(request.headers.get("x-person", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        item_id = (await request.json()).get("item_id")
        if not item_id:
            raise HTTPException(422, "item_id required")
        items[item_id] = g["name"]
        return {"ok": True}

    @app.post("/internal/share/send")
    async def share_send(request: Request):
        """Relay one joint-account message from one person's container to another's. The gateway keeps nothing: it checks who is sending, stamps the sender's name on it
        and passes it on. The admin decides who may share with whom by setting the share on the account; the receiver still needs admin approval before Firefly III gets anything."""
        g = workers.get(request.headers.get("x-person", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        body = await request.json()
        to = workers.get(body.get("to", ""))
        if body.get("type") not in ("share-data", "share-revoke", "share-request", "share-accept", "share-decline", "share-leave") or not isinstance(body.get("payload", {}), dict):
            return {"delivered": False}
        if not to:
            return {"delivered": False, "gone": True}              # not on this server (removed): the sender drops its shares with them
        if to["name"] == g["name"]:
            return {"delivered": False}
        try:
            r = await run_in_threadpool(client.post, f"{to['url']}/internal/share/inbox", json={"type": body["type"], "payload": body["payload"], "sender": g["name"]}, headers=worker_headers(to), timeout=60)
        except httpx.HTTPError:
            return {"delivered": False}
        return {"delivered": r.status_code == 200}

    @app.get("/internal/people")
    def internal_people(request: Request):
        """For a person's container: the names of the other people on this server, so a joint account is asked of someone who exists. Names only."""
        g = workers.get(request.headers.get("x-person", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        return {"people": sorted(workers)}

    POLICY_KEYS = {"plaid_budget_month": float, "plaid_daily_cap": int, "plaid_min_minutes": int, "plaid_use_recurring": str, "public_url": str, "plaid_client_id": str, "plaid_secret": str, "plaid_env": str, "plaid_redirect_url": str, "firefly_url": str, "firefly_token": str, "firefly_apply_rules": str, "firefly_send_categories": str, "firefly_include_pending": str}

    def effective_policy(name: str) -> dict:
        return {**policy["default"], **policy["people"].get(name, {})}

    @app.get("/internal/policy")
    def worker_policy(request: Request):
        """The limits this worker must keep (budget, daily call cap, how often to ask Plaid). Set on the admin page; nothing personal."""
        g = workers.get(request.headers.get("x-person", ""))
        if not g:
            raise HTTPException(401, "unauthorized")
        require_internal(request, g["internal_secret"])
        return effective_policy(g["name"])

    @app.post("/webhooks/plaid")
    async def plaid_webhook(request: Request):
        """Relay to the owning worker. The worker verifies Plaid's JWT signature (it holds the
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
        g = workers[name]
        r = await run_in_threadpool(client.post, f"{g['url']}/plaid/webhook", content=raw,
                                    headers={**worker_headers(g), "Plaid-Verification": request.headers.get("plaid-verification", ""),
                                             "Content-Type": "application/json"})
        return Response(r.content, r.status_code, media_type="application/json")

    # ---- admin: general health and counts, on demand. No route here reaches a worker's data; the only call into a worker is its /health.
    def admin(request: Request):
        throttle.check("admin")
        if not check_token(bearer(request), cfg.get("admin_token_sha256", "")):
            note_failure("admin")
            raise HTTPException(401, "unauthorized")
        throttle.ok("admin")

    @app.get("/admin/status")
    def admin_status(request: Request):
        admin(request)
        rows = []
        for n, g in workers.items():
            try:
                up = client.get(f"{g['url']}/health", timeout=3).status_code == 200
            except httpx.HTTPError:
                up = False
            stats = {}
            if up:
                try:
                    r = client.get(f"{g['url']}/internal/plaid-stats", headers=worker_headers(g), timeout=3)
                    stats = r.json() if r.status_code == 200 else {}
                except (httpx.HTTPError, ValueError):
                    stats = {}
            rows.append({"name": n, "up": up, "plaid": stats, "added_here": n in added, "calls": {k: dict(metrics.n[(n, k)]) for k in KINDS if (n, k) in metrics.n}})
        total = {k: sum(metrics.n[key][k] for key in metrics.n) for k in ("calls", "ok", "client_errors", "server_errors")}
        return {"workers": rows, "orb": dict(metrics.n[("", "orb")]) if ("", "orb") in metrics.n else {}, "unmatched": {k: dict(v) for (n, k), v in metrics.n.items() if n == "" and k != "orb"},
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
                if k == "firefly_token":
                    t = str(v).strip()
                    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{20,4096}", t):
                        raise ValueError
                    out[k] = t
                    continue
                if k == "firefly_url":
                    u = str(v).strip().rstrip("/")
                    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?", u):
                        raise ValueError
                    out[k] = u
                    continue
                if k in ("public_url", "plaid_redirect_url"):
                    u = str(v).strip().rstrip("/")
                    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?", u):
                        raise ValueError
                    out[k] = u
                    continue
                if k in ("plaid_use_recurring", "firefly_apply_rules", "firefly_send_categories", "firefly_include_pending"):
                    if str(v) not in ("on", "off"):
                        raise ValueError
                    out[k] = str(v)
                else:
                    n = POLICY_KEYS[k](v)
                    if n < 0 or n > 100000:
                        raise ValueError
                    out[k] = int(n) if n == int(n) else n
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be " + ("an https:// address" if k in ("public_url", "plaid_redirect_url") else "on or off" if k.startswith("firefly_") and k not in ("firefly_url", "firefly_token") else "your Plaid Client ID" if k == "plaid_client_id" else "your Plaid secret" if k == "plaid_secret" else "sandbox or production" if k == "plaid_env" else "your Firefly III address (http:// or https://)" if k == "firefly_url" else "a Firefly III Personal Access Token" if k == "firefly_token" else "on or off" if k == "plaid_use_recurring" else "a non-negative number"))
        return out

    def redacted() -> dict:
        """The policy without the Plaid secret: the admin page is told only whether one is saved."""
        d = {k: dict(v) if isinstance(v, dict) else v for k, v in policy.items()}
        d["default"] = {k: v for k, v in policy["default"].items() if k != "plaid_secret"}
        d["plaid_secret_set"] = bool(policy["default"].get("plaid_secret"))
        d["people"] = {n: {k: v for k, v in o.items() if k not in ("plaid_secret", "firefly_token")} for n, o in policy["people"].items()}
        d["firefly_token_set"] = {n: True for n, o in policy["people"].items() if o.get("firefly_token")}
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
        new = {"default": clean_policy(body.get("default", {})), "people": {n: clean_policy(v) for n, v in (body.get("people") or {}).items() if n in workers and v}}
        if "people" not in body:                                                    # a call that only sets the defaults leaves per-person settings alone, and the other way round
            new["people"] = {n: dict(o) for n, o in policy["people"].items()}
        if "default" not in body:
            new["default"] = dict(policy["default"])
        if "firefly_token" in new["default"]:
            raise HTTPException(422, "a Firefly III token belongs to one person: set it on that person, not as a default")
        for n, o_old in policy["people"].items():                                  # saving limits must not wipe anyone's saved token (send "" to clear it)
            if o_old.get("firefly_token") and n in workers and "firefly_token" not in ((body.get("people") or {}).get(n) or {}):
                new["people"].setdefault(n, {})["firefly_token"] = o_old["firefly_token"]
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

    @app.put("/admin/people/{name}/firefly")
    async def admin_person_firefly(request: Request, name: str):
        """Attach one person's Plaid connections to their Firefly III: its address and a Personal Access Token. Only this person's entry changes; a blank token keeps the saved one."""
        admin(request)
        if name not in workers:
            raise HTTPException(404, "no such person")
        b = await request.json()
        url, token = str(b.get("url", "")).strip(), str(b.get("token", "")).strip()
        entry = dict(policy["people"].get(name, {}))
        if url:
            entry.update(clean_policy({"firefly_url": url}))
        else:
            entry.pop("firefly_url", None)
            entry.pop("firefly_token", None)                                         # no address means no connection: the token goes too
        if url and token:
            entry.update(clean_policy({"firefly_token": token}))
        old = dict(policy["people"])
        if entry:
            policy["people"][name] = entry
        else:
            policy["people"].pop(name, None)
        try:
            save_state()
        except HTTPException:
            policy["people"] = old
            raise
        return {"ok": True, "url": entry.get("firefly_url", ""), "token_set": bool(entry.get("firefly_token"))}

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

    # ---- the admin operates each person's bank links and Firefly III connection through these. Only an allowlist of calls goes through, and what comes back
    # is scrubbed: bank and account labels and counts, never a balance or a transaction.
    ADMIN_CALLS = [("GET", r"api/status"), ("GET", r"api/plaid/usage"), ("POST", r"plaid/link-token"), ("POST", r"plaid/exchange"), ("PUT", r"api/plaid/items/[^/]+"), ("DELETE", r"api/plaid/items/[^/]+"),
                   ("POST", r"plaid/test"), ("POST", r"plaid/test-webhook"), ("POST", r"sync"), ("GET", r"api/firefly/status"), ("POST", r"api/firefly/(test|sync|retry)"), ("GET", r"api/firefly/accounts"),
                   ("PUT", r"api/firefly/accounts/[^/]+"), ("PUT", r"api/shares/[^/]+"), ("POST", r"api/firefly/accounts/[^/]+/rebalance")]

    def scrub(path: str, data):
        if path == "api/status" and isinstance(data, dict):
            plaid = data.get("plaid", {})
            for it in plaid.get("items", []):
                for a in it.get("accounts", []):
                    a.pop("balance", None)
            return {"plaid": plaid}
        if path == "api/firefly/accounts" and isinstance(data, dict):
            by = {f["id"]: f for f in data.get("firefly_accounts", [])}
            for a in data.get("accounts", []):
                bank = (-1 if a.get("kind") == "credit" else 1) * (a.get("balance") or 0.0)
                theirs = (by.get(a.get("ff_id")) or {}).get("balance")
                a["drift"] = theirs is not None and abs(float(theirs) - bank) > 0.005
                a.pop("balance", None)
                a.pop("ff_balance", None)
            for f in data.get("firefly_accounts", []):
                f.pop("balance", None)
        return data

    @app.api_route("/admin/p/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def admin_person(request: Request, name: str, path: str):
        admin(request)
        g = workers.get(name)
        if not g:
            raise HTTPException(404, "no such person")
        path = clean_path(path)
        if not any(request.method == m and re.fullmatch(p, path) for m, p in ADMIN_CALLS):
            raise HTTPException(403, "the admin page cannot make that call")
        body = await request.body()
        try:
            r = await run_in_threadpool(client.request, request.method, f"{g['url']}/{path}", params=dict(request.query_params), content=body, timeout=60,
                                        headers={**worker_headers(g), "Content-Type": request.headers.get("content-type", "application/json")})
        except httpx.HTTPError:
            raise HTTPException(502, f"{name}'s container is not answering")
        try:
            data = scrub(path, r.json())
        except ValueError:
            return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))
        return Response(json.dumps(data), r.status_code, media_type="application/json")

    @app.post("/admin/people/{name}/invite")
    async def admin_invite(request: Request, name: str):
        """A temporary code for one person: with it they can open /link and link their own banks (they type their own bank login into Plaid, not into the admin's screen). It stops working when its time is up."""
        admin(request)
        if name not in workers:
            raise HTTPException(404, "no such person")
        minutes = int(((await request.json()) or {}).get("minutes", 60))
        if not 5 <= minutes <= 7 * 24 * 60:
            raise HTTPException(422, "between 5 minutes and 7 days")
        code = secrets.token_urlsafe(9)
        invites[sha256(code)] = {"id": sha256(code)[:8], "name": name, "expires": time.time() + minutes * 60, "made": time.strftime("%Y-%m-%d %H:%M")}
        return {"code": code, "name": name, "minutes": minutes, "page": "/link"}

    @app.get("/admin/invites")
    def admin_invites(request: Request):
        admin(request)
        now = time.time()
        for k in [k for k, v in invites.items() if v["expires"] < now]:
            del invites[k]
        return {"invites": [{"id": v["id"], "name": v["name"], "minutes_left": int((v["expires"] - now) / 60) + 1, "made": v["made"]} for v in invites.values()]}

    @app.delete("/admin/invites/{iid}")
    def admin_invite_revoke(request: Request, iid: str):
        admin(request)
        hit = [k for k, v in invites.items() if v["id"] == iid]
        if not hit:
            raise HTTPException(404, "not found")
        gone = invites.pop(hit[0])
        for t in [t for t, (n, _) in link_sessions.items() if n == gone["name"]]:
            del link_sessions[t]
        return {"revoked": iid}

    @app.post("/link/session")
    async def link_session(request: Request):
        """Swap a temporary code for a session that can link banks for one person, and nothing else."""
        invite_throttle.check("invite")
        code = str((await request.json()).get("code", "")).strip()
        inv = invites.get(sha256(code))
        if not inv or inv["expires"] < time.time():
            invite_throttle.fail("invite")
            raise HTTPException(401, "that code is wrong or has run out")
        invite_throttle.ok("invite")
        token = "lnk_" + secrets.token_urlsafe(32)
        link_sessions[token] = (inv["name"], inv["expires"])
        return {"token": token, "name": inv["name"], "minutes_left": int((inv["expires"] - time.time()) / 60) + 1}

    LINK_CALLS = [("GET", r"api/status"), ("GET", r"api/firefly/status"), ("POST", r"plaid/link-token"), ("POST", r"plaid/exchange"), ("POST", r"sync")]

    @app.api_route("/link/p/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def link_person(request: Request, path: str):
        """The few calls a person with a temporary code may make: see their banks, link or relink one, pull. Nothing about balances or transactions comes back."""
        s = link_sessions.get(bearer(request) or "")
        if not s or s[1] < time.time():
            raise HTTPException(401, "your link code has run out; ask for a new one")
        name = s[0]
        if name not in workers or not any(request.method == m and re.fullmatch(p, path) for m, p in LINK_CALLS):
            raise HTTPException(403, "not available here")
        g = workers[name]
        body = await request.body()
        try:
            r = await run_in_threadpool(client.request, request.method, f"{g['url']}/{path}", params=dict(request.query_params), content=body, timeout=60,
                                        headers={**worker_headers(g), "Content-Type": request.headers.get("content-type", "application/json")})
        except httpx.HTTPError:
            raise HTTPException(502, "your container is not answering")
        try:
            return Response(json.dumps(scrub(path, r.json())), r.status_code, media_type="application/json")
        except ValueError:
            return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.get("/link")
    def link_page():
        f = ui / "link.html"
        if not f.exists():
            raise HTTPException(404, "ui not installed")
        return Response(f.read_text(), media_type="text/html")

    @app.post("/admin/people")
    async def admin_add_person(request: Request):
        """Register a new person without touching any yaml. Returns their login token and internal secret once; only the token's hash is kept."""
        admin(request)
        body = await request.json()
        name = str(body.get("name", "")).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
            raise HTTPException(422, "a name is lowercase letters, digits and hyphens, starting with a letter")
        if name in workers:
            raise HTTPException(409, "that name is taken")
        if name in removed:
            raise HTTPException(409, "that name belonged to someone who was removed; pick another, so nothing they shared reaches a new person")
        password = str(body.get("password", "") or "")
        token, internal = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        entry = {"name": name, "url": f"http://worker-{name}:8000", "internal_secret": internal}
        if password:                                             # the person's own password: only its scrypt hash is kept
            try:
                check_password_strength(password, name)
            except ValueError as e:
                raise HTTPException(422, str(e))
            entry["user_password_scrypt"] = await run_in_threadpool(hash_password, password)
            token = ""
        else:
            entry["user_token_sha256"] = sha256(token)
        workers[name] = added[name] = entry
        try:
            save_state()
        except HTTPException:
            del workers[name], added[name]
            raise
        return {"name": name, "login_token": token, "password_set": bool(password), "internal_secret": internal, "service": f"worker-{name}"}

    @app.delete("/admin/people/{name}")
    def admin_remove_person(request: Request, name: str):
        admin(request)
        if name not in added:
            raise HTTPException(404, "only people added on this page can be removed here")
        del workers[name], added[name]
        removed.add(name)                          # never handed to someone new: what was shared with the old one must not reach them
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
        """The admin page is the only screen. (The full per-person app lives on the `full-ui` branch.)"""
        return RedirectResponse("/admin", status_code=307)


    up_state: dict[str, bool] = {}

    def check_workers() -> list[str]:
        """Ask every worker if it is alive and log only a change (down, or back up). Returns the lines logged, for tests."""
        lines = []
        for n, g in list(workers.items()):
            try:
                up = client.get(f"{g['url']}/health", timeout=3).status_code == 200
            except httpx.HTTPError:
                up = False
            before = up_state.get(n)
            up_state[n] = up
            if before is None and not up:                           # first look, and it is down
                lines.append(f"worker {n} is not answering")
            elif before is not None and before != up:                # a change
                lines.append(f"worker {n} is {'back up' if up else 'not answering'}")
            else:
                continue
            (log.info if up else log.warning)(lines[-1])
        return lines

    def heartbeat(prev: dict) -> dict:
        """One line of counts since the last one: requests, errors, failed sign-ins, workers up."""
        tot = {k: sum(v[k] for v in metrics.n.values()) for k in ("calls", "ok", "client_errors", "server_errors")}
        d = {k: tot[k] - prev.get(k, 0) for k in tot}
        d_f = {k: failed[k] - prev.get("f_" + k, 0) for k in failed}
        log.info("gateway running: %d requests in the last period (%d ok, %d refused or invalid, %d server errors), %d failed sign-ins (%d locked), workers up %d of %d",
                 d["calls"], d["ok"], d["client_errors"], d["server_errors"], d_f["sign_in"], d_f["locked"], sum(1 for v in up_state.values() if v), len(workers))
        return {**tot, **{"f_" + k: v for k, v in failed.items()}}

    if monitor:
        import threading

        def loop():
            log.info("gateway started: %d people configured, state folder %s", len(workers), "writable" if str(state_path) not in ("", ".") else "not set (people and limits added in the app will not be remembered)")
            check_workers()
            prev, tick = {}, 0
            while True:
                time.sleep(60)
                check_workers()
                tick += 1
                if tick % 10 == 0:                                   # a heartbeat every 10 minutes
                    prev = heartbeat(prev)
        threading.Thread(target=loop, daemon=True, name="gateway-monitor").start()
    app.state.check_workers, app.state.heartbeat = check_workers, heartbeat
    return app
