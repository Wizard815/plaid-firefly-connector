"""Read-only data for other systems (Home Assistant, Pondering Orb, scripts), behind per-person API tokens.

A token belongs to one goblin, has a name and a list of scopes, and can be revoked. Only a hash is stored. Tokens read
data; nothing here can change anything."""
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from goblinbank import ledger
from goblinbank.security import sha256

SCOPES = {
    "summary": "Totals: net worth, bills due, savings, alert counts (and the flat sensor values for Home Assistant)",
    "bills": "The list of upcoming bills with dates and amounts",
    "alerts": "Active alerts and flags",
    "savings": "Savings accounts and their total",
    "transactions": "Individual transactions (sensitive: every purchase)",
    "refresh": "May add ?refresh=1 to ask for a fresh bank sync first (at most once every 5 minutes)",
}
REFRESH_MIN_SECONDS = 300
RESOURCES = {"summary": "summary", "ha": "summary", "bills": "bills", "alerts": "alerts", "savings": "savings", "transactions": "transactions"}


def new_token(goblin: str) -> str:
    return f"gb_{goblin}_{secrets.token_urlsafe(32)}"


def goblin_of(token: str) -> str | None:
    parts = (token or "").split("_", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "gb" and parts[1] else None


class Limiter:
    def __init__(self, per_minute: int, clock=time.monotonic):
        self.n, self.clock, self.hits = per_minute, clock, defaultdict(deque)

    def ok(self, key) -> bool:
        now, q = self.clock(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True


def authenticate(db, token: str, resource: str, now_iso: str):
    """Return (token row, None) or (None, (status, message)). 404 for an unknown resource, 403 for a missing scope."""
    row = db.one("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (sha256(token or ""),))
    if not row:
        return None, (401, "invalid token")
    scopes = row["scopes"].split(",")
    if resource != "" and resource not in RESOURCES:
        return None, (404, "unknown resource")
    if resource and RESOURCES[resource] not in scopes:
        return row, (403, f"this token does not have the '{RESOURCES[resource]}' scope")
    with db.tx() as c:
        c.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now_iso, row["id"]))
    return row, None


def data(db, resource: str, today, params: dict) -> dict:
    if resource in ("summary", "ha"):
        s = summary(db, today)
        return s if resource == "summary" else flat(s)
    if resource == "bills":
        return ledger.upcoming(db, int(params.get("days", 30)), today)
    if resource == "alerts":
        return {"alerts": ledger.alerts(db, today)}
    if resource == "savings":
        sv = ledger.savings_summary(db, today)
        return {"total": sv["total"], "places": [{k: p.get(k) for k in ("name", "institution", "kind", "balance", "apy", "maturity")} for p in sv["items"]]}
    if resource == "transactions":
        r = ledger.query_transactions(db, limit=min(int(params.get("limit", 50)), 100), start=params.get("start"), end=params.get("end"), direction=params.get("direction"), category=params.get("category"))
        r["items"] = [{k: t[k] for k in ("date", "name", "amount", "category", "account_name")} for t in r["items"]]
        return r
    raise KeyError(resource)


def summary(db, today) -> dict:
    nxt = ledger.upcoming(db, 60, today)
    mine = [i for i in nxt["items"] if i["payer"] == "me"]
    al = ledger.alerts(db, today)
    subs = ledger.subscriptions(db, today)
    fc = ledger.spending_forecast(db, today)
    return {"as_of": today.isoformat(), "net_worth": ledger.net_worth(db), "subscriptions_monthly": round(sum(s["monthly"] for s in subs), 2),
            "bills_7d": ledger.upcoming(db, 7, today)["total"], "bills_30d": ledger.upcoming(db, 30, today)["total"],
            "next_bill": ({"name": mine[0]["name"], "date": mine[0]["date"], "amount": mine[0]["amount"]} if mine else None),
            "savings_total": ledger.savings_summary(db, today)["total"],
            "spend_month_so_far": fc["spent_so_far"], "spend_month_projected": fc["projected"], "income_avg_month": fc["income_avg"], "alerts_total": len(al), "alerts_critical": sum(1 for a in al if a["severity"] == "critical"), "alerts_warning": sum(1 for a in al if a["severity"] == "warning")}


def flat(s: dict) -> dict:
    """One level of plain values, which is what Home Assistant's REST sensors want."""
    out = {k: v for k, v in s.items() if not isinstance(v, (dict, list))}
    nb = s.get("next_bill") or {}
    out.update({"next_bill_name": nb.get("name"), "next_bill_date": nb.get("date"), "next_bill_amount": nb.get("amount")})
    return out
