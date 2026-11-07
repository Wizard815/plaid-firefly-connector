"""Subscription iCalendar and CalDAV output."""
from datetime import date, datetime, timedelta, timezone

import httpx

FREQ = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}


def rrule(every: int, unit: str) -> str:
    return f"FREQ={FREQ[unit]}" + (f";INTERVAL={every}" if every != 1 else "")


def _esc(s: str) -> str:
    bs = chr(92)
    return s.replace(bs, bs * 2).replace(";", bs + ";").replace(",", bs + ",").replace("\n", bs + "n")


def vevent(s: dict) -> tuple[str, str]:
    uid = f"goblin-sub-{s['id']}@goblin-bank"
    d = date.fromisoformat(s["next_date"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
             f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
             f"RRULE:{rrule(s.get('every') or 1, s.get('unit') or 'month')}",
             f"SUMMARY:{_esc('$%.2f %s' % (s['amount'], s['name']))}",
             f"DESCRIPTION:{_esc((s.get('category') or '') + ' ' + (s.get('notes') or '')).strip()}",
             "X-GOBLIN-AMOUNT:%.2f" % s["amount"], "TRANSP:TRANSPARENT", "END:VEVENT"]
    return uid, "\r\n".join(lines)


def ics(subs: list[dict]) -> str:
    body = "\r\n".join(vevent(s)[1] for s in subs if s.get("active", 1))
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Goblin-Bank//Subscriptions//EN\r\nX-WR-CALNAME:Goblin Bank Subscriptions\r\n" + body + "\r\nEND:VCALENDAR\r\n"


def push_caldav(subs: list[dict], url: str, user: str, password: str, transport: httpx.BaseTransport | None = None) -> dict:
    """PUT one event per subscription into a CalDAV calendar collection (idempotent: stable UIDs)."""
    ok, failed = 0, []
    with httpx.Client(timeout=20, auth=(user, password), transport=transport) as c:
        for s in subs:
            if not s.get("active", 1):
                continue
            uid, ev = vevent(s)
            body = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Goblin-Bank//EN\r\n{ev}\r\nEND:VCALENDAR\r\n"
            r = c.put(f"{url.rstrip('/')}/{uid}.ics", content=body.encode(), headers={"Content-Type": "text/calendar; charset=utf-8"})
            (ok := ok + 1) if r.status_code in (200, 201, 204) else failed.append({"id": s["id"], "status": r.status_code})
    return {"pushed": ok, "failed": failed}


# ---------------------------------------------------------------- read events back out of a CalDAV calendar
import re
from xml.etree import ElementTree as ET

AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")


def parse_ics(text: str) -> list[dict]:
    """Minimal VEVENT reader: date, title, and a dollar amount from X-GOBLIN-AMOUNT or '$12.50' in the title."""
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text.replace("\r\n ", "").replace("\r", ""), re.S):
        f = {}
        for line in block.strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                f.setdefault(k.split(";")[0].upper(), v.strip())
        m = re.match(r"(\d{4})(\d{2})(\d{2})", f.get("DTSTART", ""))
        title = f.get("SUMMARY", "").replace("\\,", ",").replace("\\;", ";")
        amt = f.get("X-GOBLIN-AMOUNT")
        if amt is None and (mm := AMOUNT.search(title)):
            amt = mm.group(1).replace(",", "")
        if m:
            out.append({"date": f"{m[1]}-{m[2]}-{m[3]}", "title": title, "amount": float(amt) if amt else None, "uid": f.get("UID", ""),
                        "goblin": f.get("UID", "").startswith("goblin-sub-"), "repeats": "RRULE" in f})
    return out


def read_caldav(url: str, user: str, password: str, start: date, end: date, transport: httpx.BaseTransport | None = None) -> list[dict]:
    body = f"""<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{start:%Y%m%d}T000000Z" end="{end:%Y%m%d}T000000Z"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"""
    with httpx.Client(timeout=20, auth=(user, password), transport=transport) as c:
        r = c.request("REPORT", url, content=body.encode(), headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    r.raise_for_status()
    events = []
    for el in ET.fromstring(r.content).iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
        events += parse_ics(el.text or "")
    return sorted((e for e in events if start.isoformat() <= e["date"] <= end.isoformat()), key=lambda e: e["date"])
