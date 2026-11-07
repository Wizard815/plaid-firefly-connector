"""Push alerts to your phone through a Mattermost incoming webhook (Slack-style webhooks work the same way).

You paste one URL (Mattermost: Integrations > Incoming Webhooks). A new alert at or above your chosen severity is sent once;
the same alert is not sent again while it stays active. A critical alert is repeated after 24 hours if it is still there.
When an alert clears and later returns, you are told again. A failed send is retried on the next check, never lost."""
from datetime import datetime, timedelta, timezone

import httpx

from goblinbank import ledger, settings as cfg

SEV = {"info": 0, "warning": 1, "critical": 2}
ICON = {"critical": ":rotating_light:", "warning": ":warning:", "info": ":information_source:"}
RENOTIFY_CRITICAL = timedelta(hours=24)
MAX_PER_MESSAGE = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due(db, alerts: list[dict], now: datetime) -> list[dict]:
    floor = SEV[cfg.get(db, "notify_min_severity") or "warning"]
    sent = {r["key"]: r for r in db.all("SELECT * FROM notified")}
    out = []
    for a in alerts:
        if SEV[a["severity"]] < floor:
            continue
        prev = sent.get(a["key"])
        if prev is None or (a["severity"] == "critical" and now - datetime.fromisoformat(prev["sent"]) >= RENOTIFY_CRITICAL):
            out.append(a)
    return out


def payload(alerts: list[dict], who: str) -> dict:
    n = len(alerts)
    lines = [f"#### Goblin Bank: {n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} a look"]
    for a in alerts[:MAX_PER_MESSAGE]:
        detail = (a.get("detail") or "").replace("\n", " ")[:300]
        lines.append(f"{ICON[a['severity']]} **{a['title']}**" + (f"\n{detail}" if detail else ""))
    if n > MAX_PER_MESSAGE:
        lines.append(f"…and {n - MAX_PER_MESSAGE} more in the app.")
    return {"text": "\n\n".join(lines), "username": f"Goblin Bank ({who})", "icon_emoji": ":coin:"}


def post(url: str, body: dict, transport=None) -> None:
    with httpx.Client(timeout=10, transport=transport) as c:
        c.post(url, json=body).raise_for_status()


def run(db, today, who: str, transport=None, now: datetime | None = None) -> dict:
    """Check the alerts and send whatever is new. Safe to call as often as you like."""
    url = cfg.get(db, "notify_url")
    if not url:
        return {"configured": False, "sent": 0}
    now = now or _now()
    alerts = ledger.alerts(db, today)
    active = {a["key"] for a in alerts}
    with db.tx() as c:                                    # forget cleared alerts, so one that comes back is announced again
        for r in db.all("SELECT key FROM notified"):
            if r["key"] not in active:
                c.execute("DELETE FROM notified WHERE key=?", (r["key"],))
    fresh = due(db, alerts, now)
    if not fresh:
        return {"configured": True, "sent": 0}
    try:
        post(url, payload(fresh, who), transport)
    except httpx.HTTPStatusError as e:
        return {"configured": True, "sent": 0, "error": f"the webhook answered {e.response.status_code}"}
    except httpx.HTTPError as e:
        return {"configured": True, "sent": 0, "error": f"could not reach the webhook ({type(e).__name__})"}
    with db.tx() as c:
        for a in fresh:
            c.execute("INSERT OR REPLACE INTO notified(key,severity,sent) VALUES(?,?,?)", (a["key"], a["severity"], now.isoformat(timespec="seconds")))
    return {"configured": True, "sent": len(fresh), "titles": [a["title"] for a in fresh]}
