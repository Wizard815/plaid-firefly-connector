"""When each linked bank is asked, and whether Plaid's paid recurring detection runs for it.

Plaid bills per bank per month, and the calls themselves are free, so the schedule saves calls (and rate-limit headroom), while the recurring switch saves money:
a bank that is not asked for recurring payments is not billed the $0.15. Banks on "auto" are scheduled by what they hold: a checking account or card daily,
a savings-only bank weekly, an investment-only bank monthly. Each can be set by hand.
"""
SCHEDULES = {"always": 0, "15m": 900, "3h": 3 * 3600, "daily": 86400, "weekly": 7 * 86400, "biweekly": 14 * 86400, "monthly": 30 * 86400, "manual": None}
LABELS = {"auto": "Automatic (by account type)", "15m": "Every 15 minutes", "3h": "Every 3 hours", "daily": "Daily", "weekly": "Weekly", "biweekly": "Every 2 weeks", "monthly": "Monthly", "manual": "Only when I press Sync"}
SAVINGS = {"savings", "cd", "money market", "cash management", "hsa", "high yield savings", "529", "cd/money market", "money_market"}
RECURRING_EVERY = 7 * 86400          # repeating payments change slowly: at most weekly


def _accounts(db, item_id: str) -> list[dict]:
    return db.all("SELECT kind, subtype FROM accounts WHERE item_id=?", (item_id,))


def has_everyday(db, item_id: str) -> bool:
    """A checking account, a card or a loan: where bills and subscriptions live."""
    return any(a["kind"] in ("credit", "loan") or (a["kind"] == "cash" and (a.get("subtype") or "").lower() not in SAVINGS) for a in _accounts(db, item_id))


def auto_schedule(db, item_id: str) -> str:
    accts = _accounts(db, item_id)
    if not accts or has_everyday(db, item_id):
        return "daily"
    return "weekly" if any(a["kind"] == "cash" for a in accts) else "monthly"


def schedule_of(db, row: dict) -> str:
    s = row.get("schedule") or "auto"
    return auto_schedule(db, row["item_id"]) if s == "auto" else s


def interval(db, row: dict, floor_seconds: float = 0) -> float | None:
    """Seconds between automatic pulls of this bank (None = only when you press Sync). `floor_seconds` is the admin's 'never more often than'."""
    sec = SCHEDULES.get(schedule_of(db, row), SCHEDULES["daily"])
    return None if sec is None else max(sec, floor_seconds)


def recurring_on(db, row: dict, global_on: bool = True) -> bool:
    r = row.get("recurring") or "auto"
    if r in ("on", "off"):
        return r == "on" and True
    return global_on and has_everyday(db, row["item_id"])


def calls_per_day(db, row: dict, floor_seconds: float = 0, global_on: bool = True) -> float:
    iv = interval(db, row, floor_seconds)
    if iv is None:
        return 0.0
    per_pass = 2 + (1 if any(a["kind"] == "investment" for a in _accounts(db, row["item_id"])) else 0)          # accounts + transactions (+ holdings)
    return per_pass * 86400 / max(iv, 900) + (86400 / RECURRING_EVERY if recurring_on(db, row, global_on) else 0)
