"""A Goblin: one person's private bank service. Holds that person's data only.

Every route except /health requires the internal secret (the gateway adds it after
authenticating the person), so nothing here is reachable without going through CoinGateway.
Amounts are signed: positive = money in, negative = money out.
"""
import os
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from goblinbank import crypto, feed, importers, sync
from goblinbank.db import DB
from goblinbank.plaid import Plaid, PlaidError
from goblinbank.security import require_internal

SHAREABLE = {"robinhood_agent", "robinhood_main", "net_worth", "subscriptions"}
MONTHLY_FACTOR = {"weekly": 52 / 12, "biweekly": 26 / 12, "monthly": 1, "yearly": 1 / 12}
CYCLES = set(MONTHLY_FACTOR)


class Sub(BaseModel):
    name: str
    amount: float
    cycle: str = "monthly"
    next_date: str
    currency: str = "USD"
    category: str = ""
    url: str = ""
    notes: str = ""
    active: bool = True


def monthly(s: dict) -> float:
    return s["amount"] * MONTHLY_FACTOR.get(s["cycle"], 1) if s.get("active", 1) else 0.0


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


def create_app(transport=None, plaid_transport=None, caldav_transport=None, db: DB | None = None) -> FastAPI:
    name = os.environ.get("GOBLIN_NAME", "goblin")
    secret = os.environ.get("INTERNAL_SECRET", "")
    connector_secret = os.environ.get("CONNECTOR_SECRET", secret)
    urls = {"agent": os.environ.get("RH_AGENT_URL", ""), "csv": os.environ.get("RH_CSV_URL", ""),
            "live": os.environ.get("RH_LIVE_URL", "")}
    share = {s for s in os.environ.get("SHARE", "").split(",") if s in SHAREABLE}
    gateway_url = os.environ.get("GATEWAY_URL", "")
    client = httpx.Client(timeout=20, transport=transport)
    store = db or DB()
    plaid = Plaid(plaid_transport)
    app = FastAPI(title=f"goblin:{name}")

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

    def main_portfolio():
        live = fetch("live")      # live preferred when enabled and answering; CSV is the fallback
        if live and live.get("positions") is not None:
            return live
        return fetch("csv")

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
            return {"item_id": r["item_id"], "sync": sync.sync_item(store, plaid, r["item_id"])}
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
            return {"synced": sync.sync_item(store, plaid, body["item_id"])}
        return {"ignored": body.get("webhook_code")}

    @app.post("/sync")
    def sync_now(request: Request):
        auth(request)
        out = {"plaid": [], "robinhood": snapshot_robinhood()}
        if plaid.configured:
            try:
                out["plaid"] = sync.sync_all(store, plaid)
            except PlaidError as e:
                out["plaid_error"] = str(e)
        return out

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

    # ---------------- Read API for the UI ----------------
    def net_worth(accts):
        sign = {"cash": 1, "investment": 1, "credit": -1, "loan": -1}
        return round(sum((a["balance"] or 0) * sign.get(a["kind"], 1) for a in accts), 2)

    @app.get("/api/accounts")
    def accounts(request: Request):
        auth(request)
        return store.all("SELECT * FROM accounts ORDER BY kind, institution, name")

    @app.get("/api/transactions")
    def transactions(request: Request, limit: int = 100, account_id: str | None = None):
        auth(request)
        limit = max(1, min(limit, 500))
        if account_id:
            return store.all("SELECT * FROM transactions WHERE account_id=? ORDER BY date DESC LIMIT ?", (account_id, limit))
        return store.all("SELECT * FROM transactions ORDER BY date DESC LIMIT ?", (limit,))

    @app.get("/api/history")
    def history(request: Request):
        """Daily net worth built from balance snapshots (last known balance carried forward)."""
        auth(request)
        kinds = {a["id"]: a["kind"] for a in store.all("SELECT id,kind FROM accounts")}
        sign = {"credit": -1, "loan": -1}
        last, by_day = {}, {}
        for r in store.all("SELECT * FROM snapshots ORDER BY ts"):
            last[r["account_id"]] = r["balance"] * sign.get(kinds.get(r["account_id"], "cash"), 1)
            by_day[r["ts"][:10]] = round(sum(last.values()), 2)
        return [{"date": d, "net_worth": v} for d, v in by_day.items()]

    @app.get("/api/events")
    def events(request: Request, limit: int = 50):
        auth(request)
        return store.all("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),))

    @app.get("/api/overview")
    def overview(request: Request):
        auth(request)
        accts, subs = store.all("SELECT * FROM accounts"), store.all("SELECT * FROM subscriptions WHERE active=1")
        horizon = (date.today() + timedelta(days=30)).isoformat()
        by_kind = {}
        for a in accts:
            by_kind[a["kind"]] = round(by_kind.get(a["kind"], 0) + (a["balance"] or 0), 2)
        return {"goblin": name, "net_worth": net_worth(accts), "by_kind": by_kind, "accounts": accts,
                "subscriptions_monthly": round(sum(monthly(s) for s in subs), 2),
                "upcoming": sorted([s for s in subs if s["next_date"] <= horizon], key=lambda s: s["next_date"]),
                "events": store.all("SELECT * FROM events ORDER BY ts DESC LIMIT 10")}

    # ---------------- Subscriptions (Wallos-style) ----------------
    def valid(s: Sub):
        if s.cycle not in CYCLES:
            raise HTTPException(422, f"cycle must be one of {sorted(CYCLES)}")
        try:
            date.fromisoformat(s.next_date)
        except ValueError:
            raise HTTPException(422, "next_date must be YYYY-MM-DD")
        if s.amount < 0:
            raise HTTPException(422, "amount must be >= 0")

    @app.get("/api/subscriptions")
    def subs_list(request: Request):
        auth(request)
        rows = store.all("SELECT * FROM subscriptions ORDER BY active DESC, next_date")
        for r in rows:
            r["monthly"] = round(monthly(r), 2)
        return {"items": rows, "monthly_total": round(sum(r["monthly"] for r in rows), 2),
                "yearly_total": round(12 * sum(r["monthly"] for r in rows), 2)}

    @app.post("/api/subscriptions")
    def subs_add(request: Request, s: Sub):
        auth(request)
        valid(s)
        with store.tx() as c:
            cur = c.execute("INSERT INTO subscriptions(name,amount,currency,cycle,next_date,category,url,notes,active) VALUES(?,?,?,?,?,?,?,?,?)",
                            (s.name, s.amount, s.currency, s.cycle, s.next_date, s.category, s.url, s.notes, int(s.active)))
        return store.one("SELECT * FROM subscriptions WHERE id=?", (cur.lastrowid,))

    @app.put("/api/subscriptions/{sid}")
    def subs_update(request: Request, sid: int, s: Sub):
        auth(request)
        valid(s)
        with store.tx() as c:
            cur = c.execute("UPDATE subscriptions SET name=?,amount=?,currency=?,cycle=?,next_date=?,category=?,url=?,notes=?,active=? WHERE id=?",
                            (s.name, s.amount, s.currency, s.cycle, s.next_date, s.category, s.url, s.notes, int(s.active), sid))
        if not cur.rowcount:
            raise HTTPException(404, "not found")
        return store.one("SELECT * FROM subscriptions WHERE id=?", (sid,))

    @app.delete("/api/subscriptions/{sid}")
    def subs_delete(request: Request, sid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        return {"deleted": sid}

    @app.post("/api/subscriptions/export/caldav")
    def subs_caldav(request: Request):
        """Push subscriptions into your CalDAV calendar (CALDAV_URL / CALDAV_USER / CALDAV_PASSWORD)."""
        auth(request)
        url, user, pw = (os.environ.get(k, "") for k in ("CALDAV_URL", "CALDAV_USER", "CALDAV_PASSWORD"))
        if not url:
            raise HTTPException(503, "CALDAV_URL not configured")
        return feed.push_caldav(store.all("SELECT * FROM subscriptions"), url, user, pw, caldav_transport)

    # ---------------- Outputs: Atom money-movement feed + .ics ----------------
    @app.get("/feed.atom")
    def atom(request: Request, limit: int = 50, min_amount: float = 0):
        auth(request)
        evs = store.all("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),))
        evs = [e for e in evs if e["amount"] is None or abs(e["amount"]) >= min_amount]
        return Response(feed.atom(evs, f"Goblin Bank: {name}"), media_type="application/atom+xml")

    @app.get("/subscriptions.ics")
    def subs_ics(request: Request):
        auth(request)
        return Response(feed.ics(store.all("SELECT * FROM subscriptions")), media_type="text/calendar")

    # ---------------- Opt-in summary for gateway / Orb ----------------
    @app.get("/summary")
    def summary(request: Request, scope: str = "shared"):
        """scope=shared -> only what this person opted to expose (gateway/orb use this)."""
        auth(request)
        out = {"goblin": name}
        every = scope == "all"
        if "net_worth" in share or every:
            out["net_worth"] = net_worth(store.all("SELECT * FROM accounts"))
        if "subscriptions" in share or every:
            out["subscriptions_monthly"] = round(sum(monthly(s) for s in store.all("SELECT * FROM subscriptions WHERE active=1")), 2)
        if "robinhood_main" in share or every:
            m = main_portfolio()
            out["robinhood_main"] = m and {"as_of": m.get("as_of"), "source": m.get("source"), "balances": m.get("balances")}
        if "robinhood_agent" in share or every:
            a = fetch("agent")
            out["robinhood_agent"] = a and {"as_of": a.get("as_of"), "balances": a.get("balances")}
        return out

    return app
