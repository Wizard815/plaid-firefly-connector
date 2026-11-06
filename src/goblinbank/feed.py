"""Money-movement Atom feed and subscription iCalendar / CalDAV output."""
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx

ATOM = "http://www.w3.org/2005/Atom"


def atom(events: list[dict], title: str, self_url: str = "") -> bytes:
    ET.register_namespace("", ATOM)
    f = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(f, f"{{{ATOM}}}title").text = title
    ET.SubElement(f, f"{{{ATOM}}}id").text = f"urn:goblin-bank:{title}"
    ET.SubElement(f, f"{{{ATOM}}}updated").text = events[0]["ts"] if events else datetime.now(timezone.utc).isoformat(timespec="seconds")
    if self_url:
        ET.SubElement(f, f"{{{ATOM}}}link", rel="self", href=self_url)
    for e in events:
        en = ET.SubElement(f, f"{{{ATOM}}}entry")
        ET.SubElement(en, f"{{{ATOM}}}title").text = e["title"]
        ET.SubElement(en, f"{{{ATOM}}}id").text = f"urn:goblin-bank:event:{e['id']}"
        ET.SubElement(en, f"{{{ATOM}}}updated").text = e["ts"]
        ET.SubElement(en, f"{{{ATOM}}}category", term=e["kind"])
        ET.SubElement(en, f"{{{ATOM}}}summary").text = e.get("detail") or ""
    return ET.tostring(f, encoding="utf-8", xml_declaration=True)


CYCLE_RRULE = {"weekly": "FREQ=WEEKLY", "biweekly": "FREQ=WEEKLY;INTERVAL=2", "monthly": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY"}


def _esc(s: str) -> str:
    bs = chr(92)
    return s.replace(bs, bs * 2).replace(";", bs + ";").replace(",", bs + ",").replace("\n", bs + "n")


def vevent(s: dict) -> tuple[str, str]:
    uid = f"goblin-sub-{s['id']}@goblin-bank"
    d = date.fromisoformat(s["next_date"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
             f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
             f"RRULE:{CYCLE_RRULE.get(s['cycle'], 'FREQ=MONTHLY')}",
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
