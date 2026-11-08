"""Push alerts to your phone through Mattermost incoming webhooks (Slack-style webhooks work the same way).

Two routes, both set in Settings:
  * CDs and bonds (maturing soon, matured and still sitting there): its own webhook and its own severity floor, default "everything".
    If you never set it, these alerts use the main webhook.
  * Everything else: the main webhook, default floor "warnings and critical".
A new alert is sent once; the same alert is not sent again while it stays active. A critical alert is repeated after 24 hours if it is
still there. When an alert clears and later returns, you are told again. A failed send is retried on the next check, never lost, and one
route failing never blocks the other."""
from datetime import datetime, timedelta, timezone

import httpx

from goblinbank import ledger, settings as cfg

SEV = {"info": 0, "warning": 1, "critical": 2}
ICON = {"critical": ":rotating_light:", "warning": ":warning:", "info": ":information_source:"}
CD_KINDS = {"maturing", "matured_idle"}
RENOTIFY_CRITICAL = timedelta(hours=24)
MAX_PER_MESSAGE = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def routes(db) -> dict:
    """Where each group of alerts goes: {group: (url, minimum severity)}. A group without any address is not sent."""
    main, cds = cfg.get(db, "notify_url"), cfg.get(db, "notify_url_cds")
    main_floor = SEV[cfg.get(db, "notify_min_severity") or "warning"]
    # with its own address the CD route has its own floor (default: everything); without one, CD alerts simply follow the main route
    return {"cds": (cds, SEV[cfg.get(db, "notify_min_severity_cds") or "info"]) if cds else (main, main_floor), "other": (main, main_floor)}


def group(alert: dict) -> str:
    return "cds" if alert["kind"] in CD_KINDS else "other"


def due(db, alerts: list[dict], now: datetime) -> dict:
    """The alerts to send now, split by group: new ones, plus criticals not mentioned for 24 hours."""
    rt = routes(db)
    sent = {r["key"]: r for r in db.all("SELECT * FROM notified")}
    out = {"cds": [], "other": []}
    for a in alerts:
        g = group(a)
        url, floor = rt[g]
        if not url or SEV[a["severity"]] < floor:
            continue
        prev = sent.get(a["key"])
        if prev is None or (a["severity"] == "critical" and now - datetime.fromisoformat(prev["sent"]) >= RENOTIFY_CRITICAL):
            out[g].append(a)
    return out


def payload(alerts: list[dict], who: str, grp: str = "other") -> dict:
    n = len(alerts)
    head = "CDs and bonds" if grp == "cds" else f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} a look"
    lines = [f"#### Goblin Bank: {head}"]
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
    """Check the alerts and send whatever is new, to the right place. Safe to call as often as you like."""
    rt = routes(db)
    if not any(url for url, _ in rt.values()):
        return {"configured": False, "sent": 0}
    now = now or _now()
    alerts = ledger.alerts(db, today)
    active = {a["key"] for a in alerts}
    with db.tx() as c:                                    # forget cleared alerts, so one that comes back is announced again
        for r in db.all("SELECT key FROM notified"):
            if r["key"] not in active:
                c.execute("DELETE FROM notified WHERE key=?", (r["key"],))
    result = {"configured": True, "sent": 0, "titles": [], "errors": [], "by_route": {}}
    for g, fresh in due(db, alerts, now).items():
        if not fresh:
            continue
        try:
            post(rt[g][0], payload(fresh, who, g), transport)
        except httpx.HTTPStatusError as e:
            result["errors"].append(f"{g}: the webhook answered {e.response.status_code}")
            continue
        except httpx.HTTPError as e:
            result["errors"].append(f"{g}: could not reach the webhook ({type(e).__name__})")
            continue
        with db.tx() as c:
            for a in fresh:
                c.execute("INSERT OR REPLACE INTO notified(key,severity,sent) VALUES(?,?,?)", (a["key"], a["severity"], now.isoformat(timespec="seconds")))
        result["sent"] += len(fresh)
        result["titles"] += [a["title"] for a in fresh]
        result["by_route"][g] = len(fresh)
    if result["errors"]:
        result["error"] = "; ".join(result["errors"])
    return result
