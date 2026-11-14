"""A Worker: one person's private bank service. Holds that person's data only.

Every route except /health requires the internal secret (the gateway adds it after
authenticating the person), so nothing here is reachable without going through CoinGateway.
Amounts are signed: positive = money in, negative = money out.
"""
import os
import queue
import threading
import time
from datetime import date, timedelta

import httpx
from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from plaidproxy.oplog import log
from plaidproxy import firefly, share as jointshare, plaidcost, plaidplan, api_money, external, notify, crypto, feed, importers, ledger, settings as cfg, sync
from plaidproxy.connectors.base import now_iso
from plaidproxy.connectors.rh_csv import CsvConnector
from plaidproxy.db import DB
from plaidproxy.plaid import Plaid, PlaidError, PlaidLimit
from plaidproxy.security import require_internal



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


def create_app(transport=None, plaid_transport=None, caldav_transport=None, db: DB | None = None, today=None, llm_transport=None, autosync: bool = False, inline_webhooks: bool = False, ops_log: bool = False, firefly_transport=None, autosync_tick: float | None = None, notify_transport=None) -> FastAPI:
    name = os.environ.get("PERSON_NAME", "worker")
    secret = os.environ.get("INTERNAL_SECRET", "")
    connector_secret = os.environ.get("CONNECTOR_SECRET", secret)
    urls = {"agent": os.environ.get("RH_AGENT_URL", ""), "live": os.environ.get("RH_LIVE_URL", "")}
    gateway_url = os.environ.get("GATEWAY_URL", "")
    client = httpx.Client(timeout=20, transport=transport)
    store = db or DB()
    policy_state = {"at": 0.0, "keys": set()}

    def update_item_webhooks():
        """Banks linked before the address was set learn it too (one free call each, once)."""
        for r in store.all("SELECT item_id, token_enc, hook_url FROM plaid_items"):
            if r.get("hook_url") == plaid.webhook_url:
                continue
            try:
                plaid.post("/item/webhook/update", {"access_token": crypto.decrypt(r["token_enc"]), "webhook": plaid.webhook_url})
                with store.tx() as c:
                    c.execute("UPDATE plaid_items SET hook_url=? WHERE item_id=?", (plaid.webhook_url, r["item_id"]))
            except PlaidError:
                pass

    def register_items(force: bool = False):
        """Tell the gateway which bank belongs to this worker, so a webhook finds its way here (the gateway keeps this in memory only, so say it again after any restart)."""
        if not gateway_url or (not force and time.time() - policy_state.get("reg", 0) < 600):
            return
        policy_state["reg"] = time.time()
        reached = True
        for r in store.all("SELECT item_id FROM plaid_items"):
            try:
                client.post(f"{gateway_url}/internal/items", json={"item_id": r["item_id"]}, headers={"X-Internal-Secret": secret, "X-Person": name})
            except httpx.HTTPError:
                reached = False
                break
        if policy_state.get("gw_ok", True) != reached:
            policy_state["gw_ok"] = reached
            (log.info if reached else log.warning)("the gateway is %s", "reachable again" if reached else "not reachable from here")

    def apply_policy(force: bool = False):
        """Fetch the limits the gateway admin set for this worker and keep them in settings, where everything else already reads them.
        If the gateway cannot be reached the last ones stay in force."""
        if not gateway_url or (not force and time.time() - policy_state["at"] < 300):
            return
        policy_state["at"] = time.time()
        try:
            r = client.get(f"{gateway_url}/internal/policy", headers={"X-Person": name, "X-Internal-Secret": secret})
            if r.status_code != 200:
                return
            pol = r.json()
        except Exception:
            return
        pub = (pol.get("public_url") or "").rstrip("/")
        if pub.startswith("https://"):
            plaid.webhook_url = pub + "/webhooks/plaid"          # one address on the admin page sets both of these for everyone
            if not os.environ.get("PLAID_REDIRECT_URI"):
                plaid.redirect_uri = pol["plaid_redirect_url"] if pol.get("plaid_redirect_url") else pub + "/link"          # banks that sign you in on their own site send you back to /link, which hands off to wherever you started
            update_item_webhooks()
        keys = set()
        for k in ("firefly_url", "firefly_token", "firefly_apply_rules", "firefly_send_categories", "firefly_include_pending"):          # this person's Firefly III connection and options, set by the admin
            if pol.get(k):
                try:
                    cfg.put(store, k, pol[k])
                    keys.add(k)
                except ValueError:
                    pass
        for k in ("plaid_client_id", "plaid_secret", "plaid_env"):                 # Plaid keys the admin entered once for everyone
            if pol.get(k):
                try:
                    cfg.put(store, k, pol[k])
                    keys.add(k)
                except ValueError:
                    pass
        if keys:
            apply_plaid_creds()
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

    def item_view(row: dict) -> dict:
        floor = float(cfg.get(store, "plaid_min_minutes") or 0) * 60
        glob = cfg.get(store, "plaid_use_recurring") != "off"
        iv = plaidplan.interval(store, row, floor)
        accts = store.all("SELECT id, name, nickname, mask, kind, subtype, balance, category FROM accounts WHERE item_id=? ORDER BY kind, name", (row["item_id"],))
        return {"item_id": row["item_id"], "institution": row["institution"], "category": row.get("category"), "accounts": [{**a, "category_effective": a["category"] or row.get("category")} for a in accts],
                "schedule": row.get("schedule") or "auto", "schedule_effective": plaidplan.schedule_of(store, row),
                "recurring": row.get("recurring") or "auto", "recurring_effective": plaidplan.recurring_on(store, row, glob), "interval_minutes": None if iv is None else round(iv / 60),
                "last_sync": row.get("last_sync"), "calls_per_day": round(plaidplan.calls_per_day(store, row, floor, glob), 1),
                "status": row.get("status"), "status_detail": row.get("status_detail"), "webhook_live": plaidplan.hook_live(row, time.time())}

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
                raise PlaidLimit(f"daily Plaid call cap reached ({cap}); raise it in Settings if this is expected")
            if path in plaidcost.PER_CALL:
                b = budget_state()
                if b["budget"] is not None and b["est"] + plaidcost.PER_CALL[path] > b["budget"] + 0.005:
                    raise PlaidLimit(f"that call costs ${plaidcost.PER_CALL[path]:.2f} and would put this month's estimated Plaid bill over your ${b['budget']:.2f} budget")

        def record(self, path):
            with store.tx() as c:
                c.execute("INSERT INTO plaid_usage VALUES(?,?,1) ON CONFLICT(day,endpoint) DO UPDATE SET calls=calls+1", (date.today().isoformat(), path))

    plaid = Plaid(plaid_transport, usage=Usage())

    def apply_plaid_creds():
        """Keys saved in the app (or set by the gateway admin) take over from the environment."""
        plaid.configure(cfg.get(store, "plaid_client_id"), cfg.get(store, "plaid_secret"), cfg.get(store, "plaid_env") or "sandbox")
    apply_plaid_creds()
    import_dir = os.environ.get("IMPORT_DIR", "/data/imports")
    csv_dir = os.environ.get("RH_CSV_DIR", "/data/robinhood-csv")
    csv_connector = CsvConnector(csv_dir)
    app = FastAPI(title=f"worker:{name}")

    def share() -> set[str]:
        return set(cfg.get_list(store, "share"))

    def auth(request: Request):
        require_internal(request, secret)

    # ---------------- Robinhood (csv parsed in-process; agent/live via connector containers) ----------------
    def fetch(kind: str):
        if kind == "csv":
            out = {}
            for ep in ("balances", "positions", "history"):
                try:
                    out[ep] = getattr(csv_connector, ep)()
                    out["as_of"], out["source"] = now_iso(), csv_connector.name
                except Exception as e:
                    out[ep] = None
                    out.setdefault("errors", []).append(f"{ep}: {type(e).__name__}")
            return out
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
        return {"ok": True, "worker": name}

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

    # ---------------- Firefly III: Plaid Firefly Connector pushes your bank data there ----------------
    @app.get("/api/firefly/status")
    def ff_status(request: Request):
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        return firefly.status(store)

    @app.post("/api/firefly/test")
    def ff_test(request: Request):
        """Check the address and token with one harmless call."""
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        c = firefly.conf(store)
        if not (c["url"] and c["token"]):
            raise HTTPException(409, "enter your Firefly III address and a Personal Access Token first")
        try:
            info = firefly.Firefly(c["url"], c["token"], firefly_transport).about()
        except firefly.FireflyError as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "version": info.get("version"), "api_version": info.get("api_version")}

    @app.post("/api/firefly/sync")
    def ff_sync(request: Request):
        """Push everything new now (it also happens after every bank sync)."""
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        with sync_lock:
            return firefly.run(store, firefly_transport)

    @app.post("/api/firefly/retry")
    def ff_retry(request: Request):
        """Give failed transactions another go."""
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        with store.tx() as c:
            c.execute("UPDATE ff_tx SET attempts=0 WHERE state='error'")
        with sync_lock:
            return firefly.run(store, firefly_transport)

    class ShareIn(BaseModel):
        peer: str = ""

    class ShareInbox(BaseModel):
        type: str = ""
        payload: dict = {}
        sender: str = ""

    @app.put("/api/shares/{account_id}")
    def share_set(request: Request, account_id: str, b: ShareIn):
        """Share one of this person's bank accounts with one other person (a joint account), or stop with an empty name. Set by the admin."""
        auth(request)
        if b.peer.strip().lower() == name:
            raise HTTPException(422, "an account cannot be shared with its own owner")
        try:
            jointshare.set_share(store, account_id, b.peer.strip().lower() or None)
        except ValueError as e:
            raise HTTPException(422, str(e))
        threading.Thread(target=lambda: jointshare.run(store, share_send), daemon=True).start()
        return {"ok": True}

    # ---- joint accounts, asked and answered by the people themselves (the admin's PUT above stays as an override)
    class AskIn(BaseModel):
        peer: str

    class AnswerIn(BaseModel):
        mode: str | None = None              # accept: create (a new account in my Firefly III) or link (one I have already)
        ff_id: int | None = None

    def share_people() -> list[str]:
        try:
            r = client.get(f"{gateway_url}/internal/people", headers={"X-Person": name, "X-Internal-Secret": secret}, timeout=10)
            return [p for p in r.json().get("people", []) if p != name] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return []

    def share_kick():
        threading.Thread(target=lambda: jointshare.run(store, share_send), daemon=True).start()

    @app.get("/api/joint")
    def joint_overview(request: Request):
        """My accounts I share or asked to share, and the accounts others asked to share with me."""
        auth(request)
        o = jointshare.overview(store)
        labels = {a["id"]: firefly.account_label(a) for a in store.all("SELECT * FROM accounts")}
        for m in o["mine"]:
            m["label"] = labels.get(m["account_id"], m["account_id"])
        return {**o, "people": share_people(), "my_accounts": [{"id": a["id"], "label": labels[a["id"]]} for a in store.all("SELECT * FROM accounts WHERE source != 'shared' ORDER BY institution, name")]}

    @app.post("/api/joint/{account_id}/ask")
    def joint_ask(request: Request, account_id: str, b: AskIn):
        """Ask another person to share one of my bank accounts with them. Only its name, bank and last digits go until they accept."""
        auth(request)
        peer = b.peer.strip().lower()
        if peer == name or peer not in share_people():
            raise HTTPException(422, "choose another person on this server")
        try:
            jointshare.request_share(store, account_id, peer)
        except ValueError as e:
            raise HTTPException(422, str(e))
        share_kick()
        return {"asked": peer}

    @app.delete("/api/joint/{account_id}")
    def joint_stop(request: Request, account_id: str):
        """Stop sharing (or cancel the question): the other person's copy stops getting anything."""
        auth(request)
        if not store.one("SELECT 1 FROM shares WHERE account_id=?", (account_id,)):
            raise HTTPException(404, "not shared")
        jointshare.set_share(store, account_id, None)
        share_kick()
        return {"stopped": account_id}

    @app.post("/api/joint/from/{sender}/{account_id}/{answer}")
    def joint_answer(request: Request, sender: str, account_id: str, answer: str, b: AnswerIn | None = None):
        """Answer a share someone asked: accept (create or link in my Firefly III), decline, or leave one I accepted."""
        auth(request)
        if answer not in ("accept", "decline", "leave"):
            raise HTTPException(404, "not found")
        if answer == "accept" and b and b.mode == "link":         # it must be one of my own Firefly III asset accounts
            c = firefly.conf(store)
            if not c["enabled"]:
                raise HTTPException(409, "set up Firefly III first")
            try:
                mine_ff = {int(x["id"]) for x in firefly.Firefly(c["url"], c["token"], firefly_transport).accounts("asset")}
            except Exception:
                raise HTTPException(502, "could not read your Firefly III accounts to check the one chosen")
            if not b.ff_id or int(b.ff_id) not in mine_ff:
                raise HTTPException(422, "that is not one of your Firefly III asset accounts")
        try:
            jointshare.answer(store, sender, account_id, answer, (b.mode if b else None), (b.ff_id if b else None))
        except KeyError:
            raise HTTPException(404, "no such request")
        except ValueError as e:
            raise HTTPException(422, str(e))
        share_kick()
        return {answer: account_id}

    @app.post("/internal/share/inbox")
    def share_inbox(request: Request, m: ShareInbox):
        """From the gateway only: it stamps `sender` after checking who really sent it."""
        auth(request)
        r = jointshare.receive(store, m.sender, m.type, m.payload)
        if r.get("ok") and m.type == "share-data" and firefly.conf(store)["enabled"]:
            def push():
                with sync_lock:                                 # never alongside a sync's own push (that could make an account twice)
                    firefly.run(store, firefly_transport)
            threading.Thread(target=push, daemon=True).start()
        return r

    @app.get("/api/firefly/accounts")
    def ff_accounts(request: Request):
        """Each bank account and what happens to it in Firefly III, plus the asset accounts already there to link to."""
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        firefly.ensure_mappings(store)
        mine = store.all("SELECT a.id, a.institution, a.name, a.nickname, a.mask, a.kind, a.balance, a.category, m.mode, m.ff_id, m.created FROM accounts a JOIN ff_accounts m ON m.account_id=a.id ORDER BY a.institution, a.name")
        theirs, error = [], None
        c = firefly.conf(store)
        if c["url"] and c["token"]:
            try:
                theirs = [{"id": x["id"], "name": x["name"], "balance": x.get("current_balance"), "role": x.get("account_role")} for x in firefly.Firefly(c["url"], c["token"], firefly_transport).accounts("asset")]
            except firefly.FireflyError as e:
                error = str(e)
        by = {x["id"]: x for x in theirs}
        sh = jointshare.shared_with(store)
        return {"accounts": [{**a, "shared_with": sh.get(a["id"]), "label": firefly.account_label(a), "ff_name": (by.get(a["ff_id"]) or {}).get("name"), "ff_balance": (by.get(a["ff_id"]) or {}).get("balance")} for a in mine], "firefly_accounts": theirs, "error": error}

    @app.post("/api/firefly/accounts/{account_id}/rebalance")
    def ff_rebalance(request: Request, account_id: str):
        """Make Firefly III's balance for this account equal the bank's (moves the opening balance)."""
        auth(request)
        apply_policy(force=True)                          # pick up what the admin just saved
        c = firefly.conf(store)
        if not (c["url"] and c["token"]):
            raise HTTPException(409, "set up Firefly III first")
        try:
            return firefly.rebalance(store, firefly.Firefly(c["url"], c["token"], firefly_transport), account_id)
        except KeyError:
            raise HTTPException(404, "that account is not in Firefly III yet")
        except firefly.FireflyError as e:
            raise HTTPException(502, str(e))

    class FFMap(BaseModel):
        mode: str                             # pending | create | link | skip
        ff_id: int | None = None

    @app.put("/api/firefly/accounts/{account_id}")
    def ff_map(request: Request, account_id: str, b: FFMap):
        auth(request)
        try:
            firefly.set_mapping(store, account_id, b.mode, b.ff_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "account not found")
        return {"ok": True}

    @app.post("/plaid/test")
    def plaid_test(request: Request):
        """Check the saved keys with a free call, so a typo shows up here and not later as a mysterious failure."""
        auth(request)
        if not plaid.configured:
            raise HTTPException(409, "enter your Plaid Client ID and secret first")
        try:
            plaid.post("/institutions/get", {"count": 1, "offset": 0, "country_codes": ["US"]})
        except PlaidError as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "env": "production" if "production" in plaid.base else "sandbox"}

    @app.post("/plaid/test-webhook")
    def plaid_test_webhook(request: Request, item_id: str | None = None, wait: int = 20):
        """Sandbox only: ask Plaid to send a real, signed test webhook to your public address, then see whether it arrived."""
        auth(request)
        apply_policy(force=True)
        if "sandbox" not in plaid.base:
            raise HTTPException(409, "test webhooks only work in the Plaid Sandbox; in production a real bank update is the test")
        if not plaid.webhook_url:
            raise HTTPException(409, "set a Public address on the gateway admin page first, so Plaid knows where to send it")
        row = store.one("SELECT * FROM plaid_items" + (" WHERE item_id=?" if item_id else " ORDER BY item_id LIMIT 1"), (item_id,) if item_id else ())
        if not row:
            raise HTTPException(404, "link a bank first")
        update_item_webhooks()
        before = row.get("last_hook") or 0
        try:
            plaid.post("/sandbox/item/fire_webhook", {"access_token": crypto.decrypt(row["token_enc"]), "webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"})
        except PlaidError as e:
            raise HTTPException(502, str(e))
        for _ in range(min(max(wait, 1), 30)):
            time.sleep(1)
            now_row = store.one("SELECT last_hook FROM plaid_items WHERE item_id=?", (row["item_id"],))
            if (now_row.get("last_hook") or 0) > before:
                return {"fired": True, "arrived": True, "webhook_url": plaid.webhook_url}
        return {"fired": True, "arrived": False, "webhook_url": plaid.webhook_url}

    @app.post("/plaid/exchange")
    def plaid_exchange(request: Request, body: Exchange):
        auth(request)
        ensure_room_for_new_item()
        try:
            r = plaid.exchange(body.public_token)
            with store.tx() as c:
                c.execute("INSERT OR REPLACE INTO plaid_items(item_id,institution,token_enc,cursor) VALUES(?,?,?,NULL)",
                          (r["item_id"], body.institution, crypto.encrypt(r["access_token"])))
            if gateway_url:      # tell the gateway the item_id -> worker map (never the token)
                try:
                    client.post(f"{gateway_url}/internal/items", json={"item_id": r["item_id"]},
                                headers={"X-Internal-Secret": secret, "X-Person": name})
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
            log.warning("plaid webhook refused: signature did not verify")
            raise HTTPException(401, "webhook signature invalid")
        import json
        body = json.loads(raw)
        log.info("plaid webhook accepted: %s %s", str(body.get("webhook_type"))[:30], str(body.get("webhook_code"))[:40])
        if inline_webhooks:                                   # tests: do the work before answering
            def now_():
                with sync_lock:
                    r = sync.handle_webhook(store, plaid, body)
                    firefly.run(store, firefly_transport)
                    return r
            return await run_in_threadpool(now_)
        hook_q.put(body)                                      # Plaid wants an answer within 10 seconds (and retries for a day), so answer now and do the pull in the background
        if hook_state["t"] is None:
            hook_state["t"] = threading.Thread(target=hook_worker, daemon=True, name="plaid-webhooks")
            hook_state["t"].start()
        return {"accepted": True}

    sync_state = {"last": 0.0, "at": None}
    sync_lock = threading.Lock()
    hook_q: queue.Queue = queue.Queue()
    hook_state = {"t": None}

    def hook_worker():
        while True:
            body = hook_q.get()
            try:
                with sync_lock:                               # one at a time, and never alongside a scheduled pull
                    sync.handle_webhook(store, plaid, body)
                    firefly.run(store, firefly_transport)
            except Exception:
                pass                                          # the weekly safety-net pull covers anything missed

    def share_send(peer: str, kind: str, payload: dict) -> bool:
        """Hand one message for another person's container to the gateway, which relays it and keeps nothing."""
        if not gateway_url:
            return False
        r = client.post(f"{gateway_url}/internal/share/send", json={"to": peer, "type": kind, "payload": payload}, headers={"X-Person": name, "X-Internal-Secret": secret}, timeout=30)
        if r.status_code == 200 and r.json().get("gone"):
            return "gone"                                   # that person was removed from this server
        return r.status_code == 200 and bool(r.json().get("delivered"))

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
        try:
            out["share"] = jointshare.run(store, share_send)                         # joint accounts: what changed goes to the other person
        except Exception:
            out["share"] = {"error": True}
        out["firefly"] = firefly.run(store, firefly_transport)                 # also picks up CSV imports; with nothing new it makes no calls to Firefly III
        out["notify"] = notify.run(store, (today or date.today)(), name, notify_transport)
        banks = out["plaid"]
        pulled, skipped = sum(1 for b in banks if "skipped" not in b and "error" not in b), sum(1 for b in banks if "skipped" in b)
        errors = [str(b["error"]).split(":")[0] for b in banks if b.get("error")] + ([str(out["plaid_error"]).split(":")[0][:40]] if out.get("plaid_error") else [])
        if pulled or errors or force:                                  # quiet rounds where nothing was due are not worth a line
            (log.warning if errors else log.info)("sync finished: %d banks pulled, %d not due%s", pulled, skipped, (", problems: " + ", ".join(sorted(set(errors)))) if errors else "")
        return out

    @app.post("/sync")
    def sync_now(request: Request):
        auth(request)
        register_items()                       # the gateway forgets which bank is whose when it restarts; say it again
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
                register_items()                                  # tells the gateway which banks are ours again after it restarts
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
        added = importers.store_bank(store, account_id, rows, bal)
        threading.Thread(target=lambda: firefly.run(store, firefly_transport), daemon=True).start() if added and firefly.conf(store)["enabled"] else None
        return {"rows": len(rows), "added": added, "balance": bal}

    ctx = api_money.make_ctx(store, auth, today or date.today, name=name, plaid=plaid, client=client, caldav_transport=caldav_transport)
    api_money.register(app, ctx)

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
            notify.post(url, {"text": f":coin: **Plaid Firefly Connector ({name})**: notifications are working" + (" for CDs and bonds." if route == "cds" else "."), "username": f"Plaid Firefly Connector ({name})", "icon_emoji": ":coin:"}, notify_transport)
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
                "calls_month": sum(month_calls().values()), "min_minutes": int(float(cfg.get(store, "plaid_min_minutes") or 0)),
                "webhook_banks": sum(1 for r in store.all("SELECT last_hook FROM plaid_items") if plaidplan.hook_live(r, time.time())), "webhook_set": bool(plaid.webhook_url),
                "firefly": (lambda s: {"configured": s["configured"], "sent": s["sent"], "failed": s["failed"], "waiting": s["waiting"], "last_ok": s["last_ok"], "problem": bool(s["last_error"])})(firefly.status(store))}

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
        if any(k.startswith("plaid_") and k in ("plaid_client_id", "plaid_secret", "plaid_env") for k in body):
            apply_plaid_creds()
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
        return {"plaid": {"configured": plaid.configured, "env": "production" if "production" in plaid.base else "sandbox", "webhook_set": bool(plaid.webhook_url),
                          "items": [item_view(r) for r in store.all("SELECT * FROM plaid_items ORDER BY institution")]},
                "robinhood": {"agent": probe("agent"), "csv": "built-in", "live": probe("live"), "csv_files": csvs,
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
        gap = float(cfg.get(store, "plaid_min_minutes") or 0)
        views = [item_view(r) for r in store.all("SELECT * FROM plaid_items")]
        per_day = round(sum(v["calls_per_day"] for v in views))
        return {"month": month, "calls_month": sum(by.values()), "calls_today": today, "daily_cap": int(float(cfg.get(store, "plaid_daily_cap") or 300)),
                "by_endpoint": dict(sorted(by.items(), key=lambda kv: -kv[1])), "items": b["items"], "est_monthly_cost": b["est"], "breakdown": b["detail"]["lines"], "list_total": b["detail"]["total"],
                "custom_rate": float(rate) if rate else None, "use_recurring": b["use_recurring"], "budget": b["budget"], "over_budget": bool(b["budget"] and b["est"] > b["budget"] + 0.005),
                "room_for_items": (int((b["budget"] - b["est"] + 0.005) // b["per_item"]) if b["budget"] and b["per_item"] else None), "per_item": b["per_item"],
                "min_minutes": int(gap), "calls_per_day_estimate": int(per_day), "calls_per_month_estimate": int(round(sum(v["calls_per_day"] for v in views) * 30)),
                "webhook_set": bool(plaid.webhook_url), "webhook_banks": sum(1 for v in views if v["webhook_live"])}

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
        return [item_view(r) for r in store.all("SELECT * FROM plaid_items ORDER BY institution")]

    class ItemPatch(BaseModel):
        schedule: str | None = None        # auto, 15m, 3h, daily, weekly, biweekly, monthly, manual
        recurring: str | None = None       # auto, on, off: Plaid's paid recurring-payment detection for this bank
        category: str | None = None        # what this bank is for; its accounts without a category of their own inherit it ("" clears)
        apply_to_accounts: bool = False    # also set that category on every account in this bank, replacing any they had

    @app.put("/api/plaid/items/{item_id}")
    def plaid_item_update(request: Request, item_id: str, b: ItemPatch):
        """When this bank is asked, and whether it uses Plaid's recurring detection. Saves calls, and (recurring) money."""
        auth(request)
        row = store.one("SELECT * FROM plaid_items WHERE item_id=?", (item_id,))
        if not row:
            raise HTTPException(404, "not found")
        if b.schedule is not None and b.schedule != "auto" and b.schedule not in plaidplan.SCHEDULES:
            raise HTTPException(422, "schedule must be auto, 15m, 3h, daily, weekly, biweekly, monthly or manual")
        if b.recurring is not None and b.recurring not in ("auto", "on", "off"):
            raise HTTPException(422, "recurring must be auto, on or off")
        with store.tx() as c:
            if b.schedule is not None:
                c.execute("UPDATE plaid_items SET schedule=? WHERE item_id=?", (b.schedule, item_id))
            if b.recurring is not None:
                c.execute("UPDATE plaid_items SET recurring=? WHERE item_id=?", (b.recurring, item_id))
            if b.category is not None:
                cat = b.category.strip()
                if len(cat) > 40 or any(ord(ch) < 32 for ch in cat):
                    raise HTTPException(422, "a category is up to 40 characters")
                c.execute("UPDATE plaid_items SET category=? WHERE item_id=?", (cat or None, item_id))
                if b.apply_to_accounts:
                    c.execute("UPDATE accounts SET category=? WHERE item_id=?", (cat or None, item_id))
        return item_view(store.one("SELECT * FROM plaid_items WHERE item_id=?", (item_id,)))

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
        from plaidproxy.security import sha256
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
        ext = external.summary(store, (today or date.today)()) if share() & {"bills", "alerts", "savings"} or every else {}
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
            out["savings"] = {"total": ext["savings_total"]}
        if "spending" in share() or every:
            fc = ledger.spending_forecast(store, (today or date.today)())
            out["spending"] = {"so_far": fc["spent_so_far"], "projected": fc["projected"], "income_avg": fc["income_avg"], "pct_projected": fc["pct_projected"], "left_after": fc["left_after_projected"], "confidence": fc["confidence"]}
        if "robinhood_agent" in share() or every:
            a = fetch("agent")
            out["robinhood_agent"] = a and {"as_of": a.get("as_of"), "balances": a.get("balances")}
        return out

    if ops_log:
        try:
            n_items = store.one("SELECT COUNT(*) n FROM plaid_items")["n"]
        except Exception:
            n_items = 0
        log.info("worker started: database ok, Plaid %s (%s), %d banks linked, background sync %s, gateway %s",
                 "configured" if plaid.configured else "NOT configured", "production" if "production" in plaid.base else "sandbox", n_items, "on" if autosync else "off", "set" if gateway_url else "not set")
    return app
