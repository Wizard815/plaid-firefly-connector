"""A Goblin: one person's private bank service. Holds that person's data only.

Every route except /health requires the internal secret (the gateway adds it after
authenticating the person), so nothing here is reachable without going through CoinGateway.
Amounts are signed: positive = money in, negative = money out.
"""
import os
import threading
import time
from datetime import date, timedelta

import httpx
from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from goblinbank import plaidcost, agent, api_links, api_money, api_plan, links, external, notify, plan, crypto, feed, importers, ledger, settings as cfg, sync
from goblinbank.db import DB
from goblinbank.plaid import Plaid, PlaidError
from goblinbank.security import require_internal



def _equity(bal) -> float | None:
    """Best-effort total value from a connector's balance payload (shapes differ per source)."""
    if isinstance(bal, dict):
        for k in ("equity", "total_equity", "portfolio_value", "market_value", "total"):
            try:
                if bal.get(k) is not None:
                    return float(bal[k])
            except (TypeError, ValueError):
                pass
    return None


def create_app(transport=None, plaid_transport=None, caldav_transport=None, db: DB | None = None, today=None, llm_transport=None, autosync: bool = False, autosync_tick: float | None = None, notify_transport=None) -> FastAPI:
    name = os.environ.get("GOBLIN_NAME", "goblin")
    secret = os.environ.get("INTERNAL_SECRET", "")
    connector_secret = os.environ.get("CONNECTOR_SECRET", secret)
    urls = {"agent": os.environ.get("RH_AGENT_URL", ""), "csv": os.environ.get("RH_CSV_URL", ""),
            "live": os.environ.get("RH_LIVE_URL", "")}
    gateway_url = os.environ.get("GATEWAY_URL", "")
    client = httpx.Client(timeout=20, transport=transport)
    store = db or DB()
    policy_state = {"at": 0.0, "keys": set()}

    def apply_policy(force: bool = False):
        """Fetch the limits the gateway admin set for this goblin and keep them in settings, where everything else already reads them.
        If the gateway cannot be reached the last ones stay in force."""
        if not gateway_url or (not force and time.time() - policy_state["at"] < 300):
            return
        policy_state["at"] = time.time()
        try:
            r = client.get(f"{gateway_url}/internal/policy", headers={"X-Goblin": name, "X-Internal-Secret": secret})
            if r.status_code != 200:
                return
            pol = r.json()
        except Exception:
            return
        keys = set()
        for k in ("plaid_budget_month", "plaid_daily_cap", "plaid_min_minutes", "plaid_use_recurring"):
            if k in pol:
                try:
                    if pol[k] == "" and k != "plaid_budget_month":
                        continue                              # empty = the built-in default; the budget alone may be "no limit"
                    cfg.put(store, k, pol[k])
                    keys.add(k)
                except ValueError:
                    pass
        policy_state["keys"] = keys

    def month_calls() -> dict:
        return {r["endpoint"]: r["calls"] for r in store.all("SELECT endpoint, SUM(calls) calls FROM plaid_usage WHERE day LIKE ? GROUP BY endpoint", (date.today().strftime("%Y-%m") + "%",))}

    def budget_state() -> dict:
        """This month's estimated Plaid bill against the budget (default $5; empty = no limit) and what one more linked bank would add."""
        apply_policy()
        raw = cfg.get(store, "plaid_budget_month")
        budget = float(raw) if raw not in ("", None) and float(raw) > 0 else None
        use_rec = cfg.get(store, "plaid_use_recurring") != "off"
        det = plaidcost.estimate(store, use_rec, month_calls())
        rate = cfg.get(store, "plaid_rate_item_month")
        items = store.one("SELECT COUNT(*) n FROM plaid_items")["n"]
        est = round(items * float(rate), 2) if rate else det["total"]
        per_item = float(rate) if rate else det["per_item"]
        return {"est": est, "budget": budget, "per_item": per_item, "detail": det, "use_recurring": use_rec, "items": items}

    def ensure_room_for_new_item():
        b = budget_state()
        if b["budget"] is not None and b["est"] + b["per_item"] > b["budget"] + 0.005:
            raise HTTPException(409, f"Linking another bank would raise the estimated Plaid bill from ${b['est']:.2f} to ${b['est'] + b['per_item']:.2f} a month, over your ${b['budget']:.2f} budget. Raise the budget in Settings, Connect & import, or unlink a bank you no longer need.")

    class Usage:
        """Counts every Plaid call per day, refuses past the daily cap (a runaway-sync / webhook-storm guard), and never lets a call that Plaid bills per use push the month over budget."""
        def before(self, path):
            apply_policy()
            cap = int(float(cfg.get(store, "plaid_daily_cap") or 300))
            row = store.one("SELECT COALESCE(SUM(calls),0) n FROM plaid_usage WHERE day=?", (date.today().isoformat(),))
            if row["n"] >= cap:
                raise PlaidError(f"daily Plaid call cap reached ({cap}); raise it in Settings if this is expected")
            if path in plaidcost.PER_CALL:
                b = budget_state()
                if b["budget"] is not None and b["est"] + plaidcost.PER_CALL[path] > b["budget"] + 0.005:
                    raise PlaidError(f"that call costs ${plaidcost.PER_CALL[path]:.2f} and would put this month's estimated Plaid bill over your ${b['budget']:.2f} budget")

        def record(self, path):
            with store.tx() as c:
                c.execute("INSERT INTO plaid_usage VALUES(?,?,1) ON CONFLICT(day,endpoint) DO UPDATE SET calls=calls+1", (date.today().isoformat(), path))

    plaid = Plaid(plaid_transport, usage=Usage())
    import_dir = os.environ.get("IMPORT_DIR", "/data/imports")
    csv_dir = os.environ.get("RH_CSV_DIR", "/data/robinhood-csv")
    app = FastAPI(title=f"goblin:{name}")

    def share() -> set[str]:
        return set(cfg.get_list(store, "share"))

    def auth(request: Request):
        require_internal(request, secret)

    # ---------------- Robinhood (via connector containers) ----------------
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

    def plaid_portfolio():
        """Robinhood positions as reported by Plaid Investments, if a Robinhood item is linked."""
        accts = store.all("SELECT * FROM accounts WHERE source='plaid' AND kind='investment' AND lower(institution) LIKE '%robinhood%'")
        if not accts:
            return None
        ids = [a["id"] for a in accts]
        rows = store.all(f"SELECT * FROM holdings WHERE account_id IN ({','.join('?' * len(ids))})", ids)
        return {"source": "plaid", "as_of": max(a["updated"] or "" for a in accts), "balances": {"equity": sum(a["balance"] or 0 for a in accts)},
                "positions": [{"symbol": r["symbol"], "quantity": r["quantity"], "equity": r["value"]} for r in rows]}

    def main_portfolio():
        """rh_main_source: auto = plaid -> live (if enabled and answering) -> csv. Or force one."""
        pref = cfg.get(store, "rh_main_source") or "auto"
        order = {"auto": ["plaid", "live", "csv"], "plaid": ["plaid"], "live": ["live"], "csv": ["csv"]}[pref]
        for src in order:
            d = plaid_portfolio() if src == "plaid" else fetch(src)
            if d and d.get("positions") is not None:
                return d
        return None

    @app.get("/health")
    def health():
        return {"ok": True, "goblin": name}

    @app.get("/robinhood/main")
    def rh_main(request: Request):
        auth(request)
        return main_portfolio() or {"error": "no main-account connector configured"}

    @app.get("/robinhood/agent")
    def rh_agent(request: Request):
        auth(request)
        return fetch("agent") or {"error": "rh-agent not configured"}

    def snapshot_robinhood() -> dict:
        got = {}
        for acct, label, data in (("rh:main", "Robinhood (main)", main_portfolio()), ("rh:agent", "Robinhood (agent)", fetch("agent"))):
            eq = _equity(data.get("balances")) if data else None
            if eq is None:
                continue
            with store.tx() as c:
                c.execute("""INSERT INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated""",
                          (acct, "robinhood", "Robinhood", label, "investment", eq, sync.now()))
            sync.record_balance(store, acct, label, eq, "investment")
            got[acct] = eq
        return got

    # ---------------- Plaid ----------------
    class Exchange(BaseModel):
        public_token: str
        institution: str = ""

    @app.post("/plaid/link-token")
    def link_token(request: Request, item_id: str | None = None):
        auth(request)
        tok = None
        if not item_id:
            ensure_room_for_new_item()
        if item_id:
            row = store.one("SELECT token_enc FROM plaid_items WHERE item_id=?", (item_id,))
            tok = crypto.decrypt(row["token_enc"]) if row else None
        try:
            return {"link_token": plaid.link_token(name, tok)}
        except PlaidError as e:
            raise HTTPException(502, str(e))

    @app.post("/plaid/exchange")
    def plaid_exchange(request: Request, body: Exchange):
        auth(request)
        ensure_room_for_new_item()
        try:
            r = plaid.exchange(body.public_token)
            with store.tx() as c:
                c.execute("INSERT OR REPLACE INTO plaid_items(item_id,institution,token_enc,cursor) VALUES(?,?,?,NULL)",
                          (r["item_id"], body.institution, crypto.encrypt(r["access_token"])))
            if gateway_url:      # tell the gateway the item_id -> goblin map (never the token)
                try:
                    client.post(f"{gateway_url}/internal/items", json={"item_id": r["item_id"]},
                                headers={"X-Internal-Secret": secret, "X-Goblin": name})
                except httpx.HTTPError:
                    pass
            return {"item_id": r["item_id"], "sync": sync.sync_item(store, plaid, r["item_id"], force=True)}
        except PlaidError as e:
            raise HTTPException(502, str(e))

    @app.post("/plaid/webhook")
    async def plaid_webhook(request: Request):
        auth(request)
        raw = await request.body()
        if not plaid.verify_webhook(raw, request.headers.get("plaid-verification")):
            raise HTTPException(401, "webhook signature invalid")
        import json
        body = json.loads(raw)
        if body.get("item_id") and body.get("webhook_type") == "TRANSACTIONS":
            return {"synced": await run_in_threadpool(sync.sync_item, store, plaid, body["item_id"], True)}
        return {"ignored": body.get("webhook_code")}

    sync_state = {"last": 0.0, "at": None}
    sync_lock = threading.Lock()

    def do_sync(force: bool = False) -> dict:
        """One pull of everything: Plaid, Robinhood snapshots, drop-folder CSVs. One at a time, whoever asks."""
        apply_policy()
        with sync_lock:
            out = {"plaid": [], "robinhood": snapshot_robinhood(), "imports": scan_imports()}
            if plaid.configured:
                try:
                    out["plaid"] = sync.sync_all(store, plaid, force)
                except PlaidError as e:
                    out["plaid_error"] = str(e)
            sync_state["last"], sync_state["at"] = time.time(), sync.now()
        out["notify"] = notify.run(store, (today or date.today)(), name, notify_transport)
        return out

    @app.post("/sync")
    def sync_now(request: Request):
        auth(request)
        return do_sync(force=True)             # you asked: pull now (the daily cap and the budget still apply)

    def autosync_loop():
        """Keep the data fresh without anyone pressing Sync. Every `sync_minutes` (default 15; 0 turns it off)."""
        while True:
            try:
                mins = float(cfg.get(store, "sync_minutes") or 15)
            except ValueError:
                mins = 15.0
            time.sleep(autosync_tick if autosync_tick is not None else max(60.0, mins * 60))        # autosync_tick: tests only
            try:
                shared_changed()                                  # also retries anything that could not be delivered earlier
            except Exception:
                pass
            try:
                if mins > 0 and (autosync_tick is not None or time.time() - sync_state["last"] >= mins * 60 - 5):
                    do_sync()                                     # also checks alerts
                else:
                    notify.run(store, (today or date.today)(), name, notify_transport)                     # alerts change with the calendar even without new data
            except Exception:                                     # a failed round must never kill the loop
                pass

    if autosync:
        threading.Thread(target=autosync_loop, daemon=True, name="autosync").start()

    def scan_imports() -> list[dict]:
        """Import any CSV dropped in the imports folder, then move it to done/ so it is never re-read."""
        out = []
        if not os.path.isdir(import_dir):
            return out
        done = os.path.join(import_dir, "done")
        for f in sorted(os.listdir(import_dir)):
            path = os.path.join(import_dir, f)
            if not (f.lower().endswith(".csv") and os.path.isfile(path)) or os.path.getsize(path) > 5_000_000:
                continue
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    prov, rows = importers.parse(fh.read())
                added = importers.store(store, prov, rows)
                os.makedirs(done, exist_ok=True)
                os.replace(path, os.path.join(done, f"{sync.now().replace(':', '')}-{f}"))
                out.append({"file": f, "provider": prov, "rows": len(rows), "added": added})
            except (ValueError, OSError) as e:
                out.append({"file": f, "error": str(e)[:120]})      # left in place so you can see and fix it
        return out

    @app.post("/import/scan")
    def import_scan(request: Request):
        auth(request)
        return {"imports": scan_imports()}

    # ---------------- CSV import (PayPal / Venmo / Cash App) ----------------
    @app.post("/import/csv")
    async def import_csv(request: Request, provider: str | None = None):
        auth(request)
        if provider not in (None, "paypal", "venmo", "cashapp"):
            raise HTTPException(422, "provider must be paypal, venmo or cashapp")
        try:
            prov, rows = importers.parse((await request.body()).decode("utf-8", "replace"), provider)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"provider": prov, "rows": len(rows), "added": importers.store(store, prov, rows)}

    @app.post("/import/bank-csv")
    async def import_bank_csv(request: Request, account_id: str, flip: bool = False):
        """A CSV downloaded from a bank's own site, into an account you track by hand (a linked account already has its own feed)."""
        auth(request)
        a = store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not a:
            raise HTTPException(404, "account not found")
        if a["source"] == "plaid":
            raise HTTPException(409, "that account is linked through Plaid already; import into an account you track by hand")
        try:
            rows, bal = importers.parse_bank((await request.body()).decode("utf-8", "replace"), flip)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"rows": len(rows), "added": importers.store_bank(store, account_id, rows, bal), "balance": bal}

    def link_send(peer: str, kind: str, payload: dict) -> bool:
        """Hand a message for another goblin to the gateway, which relays it and keeps nothing."""
        if not gateway_url:
            return False
        r = client.post(f"{gateway_url}/internal/link/send", json={"to": peer, "type": kind, "payload": payload}, headers={"X-Goblin": name, "X-Internal-Secret": secret})
        return r.status_code == 200 and bool(r.json().get("delivered"))

    def link_people() -> list[str]:
        if not gateway_url:
            return []
        try:
            r = client.get(f"{gateway_url}/internal/link/people", headers={"X-Goblin": name, "X-Internal-Secret": secret})
            return r.json().get("people", []) if r.status_code == 200 else []
        except Exception:
            return []

    def link_flush() -> int:
        return links.flush(store, link_send)

    def shared_changed(gid: int | None = None, edited: bool = False) -> None:
        """A goal I share changed (or my set-aside amount did): tell the others. Never lets a link problem break the edit."""
        try:
            links.bump(store, name, gid) if edited and gid else links.push(store, name)
            links.push_accounts(store)
            link_flush()
        except Exception:
            pass

    ctx = api_money.make_ctx(store, auth, today or date.today, name=name, plaid=plaid, client=client, caldav_transport=caldav_transport, shared_changed=shared_changed, link_flush=link_flush, link_people=link_people)
    ctx.llm_transport = llm_transport
    api_money.register(app, ctx)
    api_plan.register(app, ctx)
    api_links.register(app, ctx)
    agent.register(app, ctx)

    @app.post("/api/subscriptions/export/caldav")
    def subs_caldav(request: Request):
        """Push subscriptions into your CalDAV calendar (CALDAV_URL / CALDAV_USER / CALDAV_PASSWORD)."""
        auth(request)
        url, user, pw = (cfg.get(store, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
        if not url:
            raise HTTPException(503, "CalDAV is not set up yet (Settings > Calendar)")
        return feed.push_caldav(store.all("SELECT * FROM subscriptions"), url, user, pw, caldav_transport)

    # ---------------- Calendar export ----------------
    @app.get("/subscriptions.ics")
    def subs_ics(request: Request):
        auth(request)
        return Response(feed.ics(store.all("SELECT * FROM subscriptions")), media_type="text/calendar")

    # ---------------- Notifications (Mattermost incoming webhook) ----------------
    @app.get("/api/notify/status")
    def notify_status(request: Request):
        auth(request)
        last = store.one("SELECT MAX(sent) s, COUNT(*) n FROM notified")
        return {"configured": bool(cfg.get(store, "notify_url") or cfg.get(store, "notify_url_cds")), "main": bool(cfg.get(store, "notify_url")), "cds": bool(cfg.get(store, "notify_url_cds")),
                "min_severity": cfg.get(store, "notify_min_severity") or "warning", "min_severity_cds": cfg.get(store, "notify_min_severity_cds") or "info", "last_sent": last["s"], "tracked": last["n"]}

    @app.post("/api/notify/test")
    def notify_test(request: Request, route: str = "main"):
        auth(request)
        if route not in ("main", "cds"):
            raise HTTPException(422, "route must be main or cds")
        url = cfg.get(store, "notify_url_cds") or cfg.get(store, "notify_url") if route == "cds" else cfg.get(store, "notify_url")
        if not url:
            raise HTTPException(503, "Add your Mattermost webhook address first")
        try:
            notify.post(url, {"text": f":coin: **Goblin Bank ({name})**: notifications are working" + (" for CDs and bonds." if route == "cds" else "."), "username": f"Goblin Bank ({name})", "icon_emoji": ":coin:"}, notify_transport)
        except httpx.HTTPStatusError as e:
            return {"ok": False, "detail": f"the webhook answered {e.response.status_code}" + (" (is the address right?)" if e.response.status_code in (400, 404) else "")}
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"could not reach it ({type(e).__name__})"}
        return {"ok": True}

    @app.post("/api/notify/run")
    def notify_run(request: Request):
        """Check the alerts now and send anything new."""
        auth(request)
        return notify.run(store, (today or date.today)(), name, notify_transport)

    # ---------------- Settings ----------------
    @app.get("/api/settings")
    def settings_get(request: Request):
        auth(request)
        apply_policy(force=True)                          # opening Settings picks up a change the admin just made
        return {**cfg.view(store), "managed": sorted(policy_state["keys"])}          # managed: limits the gateway admin has set; the app shows them read-only

    @app.get("/internal/plaid-stats")
    def plaid_stats(request: Request):
        """Counts only, for the admin page: how many banks, the estimated bill against the budget, calls today. Nothing about what is in them."""
        auth(request)
        b = budget_state()
        today_n = store.one("SELECT COALESCE(SUM(calls),0) n FROM plaid_usage WHERE day=?", (date.today().isoformat(),))["n"]
        return {"items": b["items"], "est_monthly_cost": b["est"], "budget": b["budget"], "calls_today": today_n, "daily_cap": int(float(cfg.get(store, "plaid_daily_cap") or 300)),
                "calls_month": sum(month_calls().values()), "min_minutes": int(float(cfg.get(store, "plaid_min_minutes") or 180))}

    @app.put("/api/settings")
    async def settings_put(request: Request):
        """Partial update. Secret fields: omit = unchanged, "" = clear. Secrets are never returned."""
        auth(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "expected an object")
        locked = [k for k in body if k in policy_state["keys"]]
        if locked:
            raise HTTPException(409, f"{', '.join(locked)} is set by the gateway admin and cannot be changed here")
        try:
            for k, v in body.items():
                cfg.validate(k, v)          # validate everything before writing anything
            for k, v in body.items():
                cfg.put(store, k, v)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return cfg.view(store)

    @app.post("/api/settings/test-caldav")
    def test_caldav(request: Request):
        auth(request)
        url, user, pw = (cfg.get(store, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
        if not url:
            raise HTTPException(503, "CalDAV is not set up yet")
        try:
            r = httpx.Client(timeout=15, auth=(user, pw), transport=caldav_transport).request("PROPFIND", url, headers={"Depth": "0"})
        except httpx.HTTPError as e:
            return {"ok": False, "detail": type(e).__name__}
        return {"ok": r.status_code in (200, 207), "status": r.status_code}

    @app.get("/api/status")
    def status(request: Request):
        """What is connected, for the Settings page."""
        auth(request)
        def probe(kind):
            if not urls[kind]:
                return "not deployed"
            try:
                return "up" if client.get(f"{urls[kind]}/health").status_code == 200 else "error"
            except httpx.HTTPError:
                return "down"
        csvs = sorted(os.listdir(csv_dir)) if os.path.isdir(csv_dir) else []
        return {"plaid": {"configured": plaid.configured, "env": os.environ.get("PLAID_ENV", "sandbox"),
                          "items": store.all("SELECT item_id, institution FROM plaid_items")},
                "robinhood": {"agent": probe("agent"), "csv": probe("csv"), "live": probe("live"), "csv_files": csvs,
                              "agent_configured": bool(cfg.get(store, "rh_mcp_url") and cfg.get(store, "rh_mcp_read_tools"))},
                "caldav_configured": bool(cfg.get(store, "caldav_url")),
                "imports": store.all("""SELECT substr(a.id, 8) provider, COUNT(t.id) transactions, MAX(t.date) last_transaction
                                        FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id WHERE a.id LIKE 'manual:%' GROUP BY a.id""")}

    @app.get("/api/plaid/usage")
    def plaid_usage(request: Request):
        """API calls this month, the estimated bill at Plaid's list prices against the budget, and how many calls a day the settings allow."""
        auth(request)
        month = date.today().strftime("%Y-%m")
        by = month_calls()
        today = store.one("SELECT COALESCE(SUM(calls),0) n FROM plaid_usage WHERE day=?", (date.today().isoformat(),))["n"]
        b = budget_state()
        rate = cfg.get(store, "plaid_rate_item_month")
        gap = float(cfg.get(store, "plaid_min_minutes") or 180)
        inv = sum(1 for x in b["detail"]["lines"] if x["label"] == "Investments holdings" for _ in range(x["qty"]))
        per_day = round(b["items"] * (max(1.0, 1440 / max(gap, 1)) * 2 + (2 if b["use_recurring"] else 0)) + inv, 0)     # accounts + transactions per pass, plus recurring and holdings once a day
        return {"month": month, "calls_month": sum(by.values()), "calls_today": today, "daily_cap": int(float(cfg.get(store, "plaid_daily_cap") or 300)),
                "by_endpoint": dict(sorted(by.items(), key=lambda kv: -kv[1])), "items": b["items"], "est_monthly_cost": b["est"], "breakdown": b["detail"]["lines"], "list_total": b["detail"]["total"],
                "custom_rate": float(rate) if rate else None, "use_recurring": b["use_recurring"], "budget": b["budget"], "over_budget": bool(b["budget"] and b["est"] > b["budget"] + 0.005),
                "room_for_items": (int((b["budget"] - b["est"] + 0.005) // b["per_item"]) if b["budget"] and b["per_item"] else None), "per_item": b["per_item"],
                "min_minutes": int(gap), "calls_per_day_estimate": int(per_day)}

    @app.get("/internal/connector-config/rh-agent")
    def agent_config(request: Request):
        """The rh-agent connector pulls its (editable) config from here, authenticated with the connector secret."""
        require_internal(request, connector_secret)
        g = lambda k: cfg.get(store, k)
        return {"url": g("rh_mcp_url"), "token": g("rh_mcp_token"), "read_tools": g("rh_mcp_read_tools"),
                "balances": g("rh_mcp_tool_balances"), "positions": g("rh_mcp_tool_positions"), "history": g("rh_mcp_tool_history")}

    @app.post("/robinhood/csv-upload")
    async def csv_upload(request: Request):
        """Save a Robinhood activity-report CSV where the rh-csv connector reads it."""
        auth(request)
        raw = await request.body()
        if len(raw) > 5_000_000:
            raise HTTPException(413, "file too large")
        head = raw[:2000].decode("utf-8", "replace")
        if "Trans Code" not in head or "Activity Date" not in head:
            raise HTTPException(422, "not a Robinhood activity report (expected Activity Date and Trans Code columns)")
        os.makedirs(csv_dir, exist_ok=True)
        fname = f"import-{sync.now().replace(':', '').replace('+', 'Z')}.csv"
        with open(os.path.join(csv_dir, fname), "wb") as f:
            f.write(raw)
        return {"saved": fname}

    @app.get("/api/plaid/items")
    def plaid_items(request: Request):
        auth(request)
        return store.all("SELECT item_id, institution FROM plaid_items")

    @app.delete("/api/plaid/items/{item_id}")
    def plaid_remove(request: Request, item_id: str):
        """Unlink: tell Plaid to drop the item (best effort), then delete it and its data here."""
        auth(request)
        row = store.one("SELECT token_enc FROM plaid_items WHERE item_id=?", (item_id,))
        if not row:
            raise HTTPException(404, "not found")
        try:
            plaid.post("/item/remove", {"access_token": crypto.decrypt(row["token_enc"])})
        except PlaidError:
            pass
        with store.tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM accounts WHERE item_id=?", (item_id,))]
            for a in ids:
                for t in ("transactions", "holdings", "snapshots"):
                    c.execute(f"DELETE FROM {t} WHERE account_id=?", (a,))
            c.execute("DELETE FROM accounts WHERE item_id=?", (item_id,))
            c.execute("DELETE FROM plaid_items WHERE item_id=?", (item_id,))
        return {"removed": item_id, "accounts": len(ids)}

    # ---------------- API tokens: read-only access for Home Assistant, the Orb, scripts ----------------
    class TokenIn(BaseModel):
        name: str
        scopes: list[str]

    limiter = external.Limiter(int(os.environ.get("API_RATE_PER_MIN", "120")))

    @app.get("/api/tokens")
    def tokens(request: Request):
        auth(request)
        return {"items": [{**t, "scopes": t["scopes"].split(",")} for t in store.all("SELECT id,name,scopes,created,last_used,revoked FROM api_tokens ORDER BY revoked, id DESC")], "scopes": external.SCOPES}

    @app.post("/api/tokens")
    def token_new(request: Request, b: TokenIn):
        auth(request)
        bad = [x for x in b.scopes if x not in external.SCOPES]
        if not b.name.strip() or not b.scopes or bad:
            raise HTTPException(422, "a name and at least one valid scope are required" + (f" (unknown: {', '.join(bad)})" if bad else ""))
        tok = external.new_token(name)
        with store.tx() as c:
            cur = c.execute("INSERT INTO api_tokens(name,token_hash,scopes,created) VALUES(?,?,?,?)", (b.name.strip()[:60], external_hash(tok), ",".join(sorted(set(b.scopes))), sync.now()))
        return {"id": cur.lastrowid, "name": b.name.strip(), "scopes": sorted(set(b.scopes)), "token": tok, "note": "Copy it now. It is not shown again."}

    @app.delete("/api/tokens/{tid}")
    def token_revoke(request: Request, tid: int):
        auth(request)
        with store.tx() as c:
            cur = c.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,))
        if not cur.rowcount:
            raise HTTPException(404, "not found")
        return {"revoked": tid}

    def external_hash(tok):
        from goblinbank.security import sha256
        return sha256(tok)

    @app.get("/external/{resource:path}")
    def external_get(request: Request, resource: str):
        """Called by the gateway on behalf of a token holder (the gateway adds the internal secret; the token is checked here)."""
        auth(request)
        tok = request.headers.get("x-api-token", "")
        row, err = external.authenticate(store, tok, resource.strip("/"), sync.now())
        if err:
            raise HTTPException(*err)
        if not limiter.ok(row["id"]):
            raise HTTPException(429, "too many requests: slow down (limit %d a minute)" % limiter.n)
        res = resource.strip("/")
        wants_refresh = request.query_params.get("refresh") in ("1", "true", "yes")
        refreshed = False
        if wants_refresh:
            if "refresh" not in row["scopes"].split(","):
                raise HTTPException(403, "this token does not have the 'refresh' scope")
            if time.time() - sync_state["last"] >= external.REFRESH_MIN_SECONDS:           # at most one real refresh per window, however often it is asked
                do_sync()
                refreshed = True
        if not res:
            return {"goblin": name, "token": row["name"], "resources": sorted(r for r, sc in external.RESOURCES.items() if sc in row["scopes"].split(","))}
        try:
            out = external.data(store, res, (today or date.today)(), {k: v for k, v in request.query_params.items() if k != "refresh"})
        except ValueError:
            raise HTTPException(422, "bad parameter")
        if isinstance(out, dict):
            out["synced_at"], out["refreshed"] = sync_state["at"], refreshed
        return out

    # ---------------- Opt-in summary for gateway / Orb ----------------
    @app.get("/summary")
    def summary(request: Request, scope: str = "shared"):
        """scope=shared -> only what this person opted to expose (gateway/orb use this)."""
        auth(request)
        out = {"goblin": name}
        every = scope == "all"
        if "net_worth" in share() or every:
            out["net_worth"] = ledger.net_worth(store)
        if "subscriptions" in share() or every:
            out["subscriptions_monthly"] = round(sum(s["monthly"] for s in ledger.subscriptions(store, (today or date.today)())), 2)
        if "robinhood_main" in share() or every:
            m = main_portfolio()
            out["robinhood_main"] = m and {"as_of": m.get("as_of"), "source": m.get("source"), "balances": m.get("balances")}
        ext = external.summary(store, (today or date.today)()) if share() & {"bills", "alerts", "savings", "goals"} or every else {}
        if "bills" in share() or every:
            out["bills"] = {"next_7d": ext["bills_7d"], "next_30d": ext["bills_30d"], "next": ext["next_bill"]}
        if "alerts" in share() or every:
            out["alerts"] = {"total": ext["alerts_total"], "critical": ext["alerts_critical"], "warning": ext["alerts_warning"]}
        # The itemised lists are separate opt-ins: sharing totals doesn't share these.
        if "bill_list" in share() or every:
            up = ledger.upcoming(store, 30, (today or date.today)())["items"][:60]
            out["bill_list"] = [{"date": i["date"], "name": i["name"], "amount": i["amount"], "category": i["category"],
                                 "paid_by": "me" if i["payer"] == "me" else (i["payer_name"] or "someone else")} for i in up]
        if "alert_list" in share() or every:
            out["alert_list"] = [{"severity": a["severity"], "kind": a["kind"], "title": a["title"], "detail": a.get("detail", ""), **({"date": a["date"]} if a.get("date") else {})}
                                 for a in ledger.alerts(store, (today or date.today)())][:30]
        if "savings" in share() or every:
            out["savings"] = {"total": ext["savings_total"], "free": ext["savings_free"], "earmarked": ext["savings_earmarked"]}
        if "goals" in share() or every:
            gv = plan.goals_view(store, (today or date.today)())
            out["goals"] = [{"name": g["name"], "target": g["target"], "allocated": g["allocated"], "pct": g["pct"]} for g in gv["items"] if g["status"] == "active" and g.get("shared_state") != "offered"]
        if "spending" in share() or every:
            fc = ledger.spending_forecast(store, (today or date.today)())
            out["spending"] = {"so_far": fc["spent_so_far"], "projected": fc["projected"], "income_avg": fc["income_avg"], "pct_projected": fc["pct_projected"], "left_after": fc["left_after_projected"], "confidence": fc["confidence"]}
        if "robinhood_agent" in share() or every:
            a = fetch("agent")
            out["robinhood_agent"] = a and {"as_of": a.get("as_of"), "balances": a.get("balances")}
        return out

    return app
