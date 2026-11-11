"""Interval math (Wallos-style repeats) and detection of repeating payments."""
import calendar
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

PRESETS = {"daily": (1, "day"), "weekly": (1, "week"), "biweekly": (2, "week"), "fourweekly": (4, "week"), "monthly": (1, "month"),
           "bimonthly": (2, "month"), "quarterly": (3, "month"), "semiannual": (6, "month"), "yearly": (1, "year"), "biennial": (2, "year")}
UNITS = ("day", "week", "month", "year")


def label(every: int, unit: str) -> str:
    for k, v in PRESETS.items():
        if v == (every, unit):
            return k
    return f"every {every} {unit}{'s' if every != 1 else ''}"


def resolve(every=None, unit=None, cycle=None) -> tuple[int, str]:
    """Accept a preset name or an explicit (every, unit). Raises ValueError on nonsense."""
    if cycle and not (every and unit):
        if cycle not in PRESETS:
            raise ValueError(f"cycle must be one of {sorted(PRESETS)} or use every + unit")
        return PRESETS[cycle]
    if unit not in UNITS:
        raise ValueError(f"unit must be one of {UNITS}")
    if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 1000:
        raise ValueError("every must be a whole number from 1 to 1000")
    return every, unit


def shift(anchor: date, every: int, unit: str, k: int) -> date:
    """anchor + k intervals. Months are counted from the anchor each time, so a 31st clamps to the month end without drifting."""
    if unit in ("day", "week"):
        try:
            return anchor + timedelta(days=every * k * (7 if unit == "week" else 1))
        except OverflowError:
            return date.max if k > 0 else date.min
    months = every * k * (12 if unit == "year" else 1)
    y, m = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    m += 1
    if not 1 <= y <= 9999:
        return date.max if y > 9999 else date.min      # absurd intervals must not crash the page
    return date(y, m, min(anchor.day, calendar.monthrange(y, m)[1]))


def first_k_on_or_after(anchor: date, every: int, unit: str, target: date) -> int:
    if unit in ("day", "week"):
        step = every * (7 if unit == "week" else 1)
        return -((anchor - target).days // step)
    per = every * (12 if unit == "year" else 1)
    k = ((target.year - anchor.year) * 12 + target.month - anchor.month) // per - 1
    while shift(anchor, every, unit, k) < target:
        k += 1
    return k


def occurrences(anchor: date, every: int, unit: str, start: date, end: date, stop: date | None = None) -> list[date]:
    """Every occurrence date in [start, end]; none after `stop` (the end date) if given."""
    out, k = [], first_k_on_or_after(anchor, every, unit, start)
    while True:
        d = shift(anchor, every, unit, k)
        if d > end or (stop and d > stop):
            return out
        out.append(d)
        k += 1


def next_due(anchor: date, every: int, unit: str, today: date, stop: date | None = None) -> date | None:
    d = shift(anchor, every, unit, first_k_on_or_after(anchor, every, unit, today))
    return None if stop and d > stop else d


def monthly_equiv(amount: float, every: int, unit: str) -> float:
    per_month = {"day": 30.4375, "week": 30.4375 / 7, "month": 1.0, "year": 1 / 12}[unit]
    return amount * per_month / every


# ---------------------------------------------------------------- detection of repeating payments
NOISE = {"pos", "purchase", "debit", "card", "payment", "pmt", "ach", "www", "com", "inc", "llc", "recurring", "online", "checkcard", "visa", "withdrawal", "autopay"}


def norm(name: str) -> str:
    toks = [t for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split() if t not in NOISE and len(t) > 1]
    return " ".join(toks[:3])


def same_payee(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / len(ta | tb) >= 0.5


# (every, unit, expected gap days, tolerance days, minimum occurrences)
CANDIDATES = [(1, "week", 7, 1, 4), (2, "week", 14, 2, 4), (4, "week", 28, 2, 4), (1, "month", 30.4, 3.5, 3), (2, "month", 60.8, 5, 3),
              (3, "month", 91.3, 7, 3), (6, "month", 182.6, 10, 2), (1, "year", 365.25, 14, 2)]


def detect(txns: list[dict], today: date, loose: bool = False) -> list[dict]:
    """Find payments that repeat on a steady interval for a steady amount. Pure function of the transaction list.
    `loose` is the manual, thorough scan: one fewer repeat is enough and the timing and amount may wander a little more, so it also catches newer or irregular ones (with lower confidence)."""
    groups = defaultdict(list)
    for t in txns:
        key = norm(t["name"])
        if key and t["amount"]:
            groups[(key, t["amount"] > 0)].append(t)
    found = []
    for (key, inflow), ts in groups.items():
        ts.sort(key=lambda t: t["date"])
        dates = [date.fromisoformat(t["date"]) for t in ts]
        if len(dates) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        med = statistics.median(gaps)
        best = min(CANDIDATES, key=lambda c: abs(c[2] - med))
        every, unit, exp, tol, need = best
        if len(dates) < (max(2, need - 1) if loose else need) or abs(exp - med) > tol * (2.2 if loose else 1.5):
            continue
        on_time = sum(1 for g in gaps if abs(g - exp) <= tol) / len(gaps)
        amts = [abs(t["amount"]) for t in ts]
        m = statistics.median(amts)
        stable = sum(1 for a in amts if abs(a - m) <= max(1.0, 0.15 * m)) / len(amts)
        if on_time < (0.55 if loose else 0.7) or stable < (0.55 if loose else 0.7):
            continue
        last = dates[-1]
        nxt = next_due(last, every, unit, today) if last >= today else shift(last, every, unit, first_k_on_or_after(last, every, unit, today))
        acct = Counter(t["account_id"] for t in ts).most_common(1)[0][0]
        display = Counter(t["name"].strip() for t in ts).most_common(1)[0][0]
        found.append({"key": f"{key}|{'in' if inflow else 'out'}", "name": display.title() if display.isupper() else display, "amount": round(m, 2),
                      "direction": "in" if inflow else "out", "every": every, "unit": unit, "label": label(every, unit), "account_id": acct,
                      "last_date": last.isoformat(), "next_date": nxt.isoformat(), "occurrences": len(dates),
                      "confidence": round(min(1.0, (0.4 + 0.12 * len(dates)) * on_time * stable), 2)})
    return sorted(found, key=lambda f: -f["confidence"])
