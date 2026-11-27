#!/usr/bin/env python3
"""Move Wallos subscriptions into Firefly III as bills (Firefly calls them subscriptions in its menus), so Wallos can be retired.

Wallos is only READ. Nothing is written to Firefly III unless you pass --apply, and a bill whose name Firefly already has is skipped,
so running it twice adds nothing twice.

    python3 scripts/migrate_from_wallos.py --wallos http://192.168.20.5:8109 --firefly http://192.168.20.5:8182          # what would be added
    python3 scripts/migrate_from_wallos.py --wallos ... --firefly ... --apply                                           # add them

Keys come from the environment, never the command line:
    WALLOS_API_KEY     Wallos: Settings -> API key
    FIREFLY_TOKEN      Firefly III: Options -> Profile -> OAuth -> Personal Access Tokens

Afterwards check Firefly III -> Subscriptions, then stop and remove the Wallos container (and keep its /var/www/html/db folder until you're sure).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plaidproxy.firefly import Firefly, FireflyError  # noqa: E402

# Wallos cycle ids: 1 days, 2 weeks, 3 months, 4 years; "frequency" is how many of them.
DAYS, WEEKS, MONTHS, YEARS = 1, 2, 3, 4


class Stop(Exception):
    pass


def wallos_subscriptions(url: str, key: str, transport: httpx.BaseTransport | None = None) -> list[dict]:
    try:
        with httpx.Client(timeout=30, transport=transport) as c:
            r = c.get(url.rstrip("/") + "/api/subscriptions/get_subscriptions.php", params={"api_key": key})
    except httpx.HTTPError as e:
        raise Stop(f"could not reach Wallos at {url} ({type(e).__name__})")
    try:
        j = r.json()
    except ValueError:
        raise Stop(f"Wallos answered {r.status_code} with something that isn't JSON: is --wallos the right address?")
    if r.status_code >= 400 or j.get("success") is False:
        raise Stop(f"Wallos refused: {j.get('title') or j.get('message') or r.status_code}. Check WALLOS_API_KEY (Wallos: Settings -> API key).")
    return list(j.get("subscriptions") or [])


def repeat(cycle: int, every: int) -> tuple[str, int] | None:
    """Firefly's repeat_freq and skip for a Wallos cycle, or None when Firefly can't repeat that way."""
    every = max(1, int(every or 1))
    if cycle == WEEKS:
        return "weekly", every - 1
    if cycle == DAYS:
        return ("weekly", every // 7 - 1) if every % 7 == 0 else None
    if cycle == MONTHS:
        return {3: ("quarterly", 0), 6: ("half-year", 0), 12: ("yearly", 0)}.get(every, ("monthly", every - 1))
    if cycle == YEARS:
        return "yearly", every - 1
    return None


def bill_for(s: dict, currency: str, slack: float, group: str) -> tuple[dict | None, str]:
    """The Firefly bill for one Wallos subscription, or None and why not."""
    name = str(s.get("name") or "").strip()
    if not name:
        return None, "it has no name"
    try:
        price = round(float(s.get("price")), 2)
    except (TypeError, ValueError):
        return None, "its price isn't a number"
    rep = repeat(int(s.get("cycle") or 0), int(s.get("frequency") or 1))
    if rep is None:
        return None, f"it repeats every {s.get('frequency')} days, which Firefly can't do (only weeks, months and years): add it by hand"
    due = str(s.get("next_payment") or "")[:10]
    if len(due) != 10:
        return None, "it has no next payment date"
    notes = "\n".join(x for x in (str(s.get("notes") or "").strip(), str(s.get("url") or "").strip(),
                                  f"Paid with: {s['payment_method_name']}" if s.get("payment_method_name") else "",
                                  f"Category in Wallos: {s['category_name']}" if s.get("category_name") else "",
                                  "Moved from Wallos.") if x)
    return {
        "name": name[:255], "amount_min": f"{price * (1 - slack):.2f}", "amount_max": f"{price * (1 + slack):.2f}",
        "date": due, "repeat_freq": rep[0], "skip": rep[1], "active": not s.get("inactive"),
        "currency_code": currency, "notes": notes, "object_group_title": group,
    }, ""


def existing_bills(ff: Firefly) -> set[str]:
    names, page = set(), 1
    while True:
        j = ff.call("GET", "/bills", params={"page": page, "limit": 50})
        names |= {str(b["attributes"].get("name", "")).strip().lower() for b in j.get("data", [])}
        if page >= int((j.get("meta", {}).get("pagination", {}) or {}).get("total_pages", 1)):
            return names
        page += 1


def run(subs: list[dict], ff: Firefly, apply: bool, currency: str, slack: float, group: str, include_inactive: bool) -> dict:
    have = existing_bills(ff)
    out = {"added": [], "would_add": [], "already": [], "skipped": []}
    for s in subs:
        if s.get("inactive") and not include_inactive:
            out["skipped"].append((str(s.get("name")), "it's inactive in Wallos (pass --include-inactive to move it as an inactive bill)"))
            continue
        bill, why = bill_for(s, currency, slack, group)
        if bill is None:
            out["skipped"].append((str(s.get("name")), why))
            continue
        if bill["name"].lower() in have:
            out["already"].append(bill["name"])
            continue
        if apply:
            ff.call("POST", "/bills", body=bill)
            have.add(bill["name"].lower())
            out["added"].append(bill["name"])
        else:
            every = f" (every {bill['skip'] + 1})" if bill["skip"] else ""
            out["would_add"].append(f"{bill['name']}: about {s.get('price')} {currency}, {bill['repeat_freq']}{every}, next {bill['date']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wallos", required=True, help="Wallos address, e.g. http://192.168.20.5:8109")
    ap.add_argument("--firefly", required=True, help="Firefly III address, e.g. http://192.168.20.5:8182")
    ap.add_argument("--apply", action="store_true", help="add the bills (without it, only shows what would be added)")
    ap.add_argument("--currency", default="USD", help="currency code for every bill (Wallos's own currency ids aren't read)")
    ap.add_argument("--slack", type=float, default=0.05, help="how far a payment may differ from the price and still match (default 0.05 = 5%%)")
    ap.add_argument("--group", default="Subscriptions", help="Firefly group to put the bills in")
    ap.add_argument("--include-inactive", action="store_true")
    a = ap.parse_args(argv)
    wkey, ftoken = os.environ.get("WALLOS_API_KEY", ""), os.environ.get("FIREFLY_TOKEN", "")
    if not wkey or not ftoken:
        print("Set WALLOS_API_KEY and FIREFLY_TOKEN in the environment first.", file=sys.stderr)
        return 2
    try:
        out = run(wallos_subscriptions(a.wallos, wkey), Firefly(a.firefly, ftoken), a.apply, a.currency, a.slack, a.group, a.include_inactive)
    except (Stop, FireflyError) as e:
        print(e, file=sys.stderr)
        return 1
    for k, title in (("added", "Added to Firefly III"), ("would_add", "Would add (run again with --apply)"), ("already", "Already in Firefly III, left alone")):
        if out[k]:
            print(f"{title}:\n  " + "\n  ".join(out[k]))
    if out["skipped"]:
        print("Not moved:\n  " + "\n  ".join(f"{n}: {why}" for n, why in out["skipped"]))
    if not any(out.values()):
        print("Wallos has no subscriptions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
