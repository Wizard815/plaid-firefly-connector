"""When each linked bank is asked, and whether Plaid's paid recurring detection runs for it.

Plaid bills per bank per month, and the calls themselves are free, so the schedule saves calls (and rate-limit headroom), while the recurring switch saves money:
a bank that is not asked for recurring payments is not billed the $0.15. Banks on "auto" are scheduled by what they hold: a checking account or card daily,
a savings-only bank weekly, an investment-only bank monthly. Each can be set by hand.
"""
SCHEDULES = {"always": 0, "15m": 900, "3h": 3 * 3600, "daily": 86400, "weekly": 7 * 86400, "biweekly": 14 * 86400, "monthly": 30 * 86400, "manual": None}
LABELS = {"auto": "Automatic (by account type)", "15m": "Every 15 minutes", "3h": "Every 3 hours", "daily": "Daily", "weekly": "Weekly", "biweekly": "Every 2 weeks", "monthly": "Monthly", "manual": "Only when I press Sync"}
SAVINGS = {"savings", "cd", "money market", "cash management", "hsa", "high yield savings", "529", "cd/money market", "money_market"}
RECURRING_EVERY = 7 * 86400          # repeating payments change slowly: at most weekly
HOOK_LIVE = 14 * 86400               # a bank that has sent a verified webhook in the last two weeks is "live": Plaid tells us when there is news
SAFETY_NET = 7 * 86400               # so on Automatic it is only polled weekly, in case a webhook was missed


def hook_live(row: dict, now: float) -> bool:
    return bool(row.get("last_hook")) and now - row["last_hook"] < HOOK_LIVE


# What an account is FOR, set by you (per account, or once for a whole bank). It decides how it is treated: whether it counts as savings, and how often its bank is asked.
CATEGORIES = ["Checking", "Bills", "Spending", "Savings", "Emergency fund", "Credit card", "Retirement", "Investment", "Joint", "Business", "Other"]
SAVINGS_CATS = {"savings", "emergency fund"}
INVEST_CATS = {"retirement", "investment"}


def category_group(cat: str | None) -> str | None:
    """everyday | savings | investment for a category you chose, None if none was chosen. A name of your own counts as everyday."""
    if not cat:
        return None
    c = cat.strip().lower()
    return "savings" if c in SAVINGS_CATS else "investment" if c in INVEST_CATS else "everyday"


def _accounts(db, item_id: str) -> list[dict]:
    return db.all("SELECT a.kind, a.subtype, COALESCE(a.category, i.category) AS cat FROM accounts a LEFT JOIN plaid_items i ON i.item_id=a.item_id WHERE a.item_id=?", (item_id,))


def has_everyday(db, item_id: str) -> bool:
    """A checking account, a card or a loan: where bills and subscriptions live (or an account you called checking, bills, spending...)."""
    for a in _accounts(db, item_id):
        g = category_group(a.get("cat"))
        if g:
            if g == "everyday":
                return True
            continue
        if a["kind"] in ("credit", "loan") or (a["kind"] == "cash" and (a.get("subtype") or "").lower() not in SAVINGS):
            return True
    return False


def auto_schedule(db, item_id: str) -> str:
    accts = _accounts(db, item_id)
    if not accts or has_everyday(db, item_id):
        return "daily"
    return "weekly" if any(category_group(a.get("cat")) == "savings" or (not a.get("cat") and a["kind"] == "cash") for a in accts) else "monthly"


def schedule_of(db, row: dict) -> str:
    s = row.get("schedule") or "auto"
    return auto_schedule(db, row["item_id"]) if s == "auto" else s


def interval(db, row: dict, floor_seconds: float = 0, now: float | None = None) -> float | None:
    """Seconds between automatic pulls of this bank (None = only when you press Sync). `floor_seconds` is the admin's 'never more often than'.
    A bank on Automatic whose webhooks are arriving is only polled weekly as a safety net."""
    import time
    sec = SCHEDULES.get(schedule_of(db, row), SCHEDULES["daily"])
    if sec is not None and (row.get("schedule") or "auto") == "auto" and hook_live(row, now if now is not None else time.time()):
        sec = max(sec, SAFETY_NET)
    return None if sec is None else max(sec, floor_seconds)


def recurring_on(db, row: dict, global_on: bool = True) -> bool:
    r = row.get("recurring") or "auto"
    if r in ("on", "off"):
        return r == "on" and True
    return global_on and has_everyday(db, row["item_id"])


def calls_per_day(db, row: dict, floor_seconds: float = 0, global_on: bool = True, now: float | None = None) -> float:
    """Expected calls a day for this bank. A quiet polled pass costs two calls (transactions, then balances, since Plaid only sends balances with the sync when something
    changed), plus holdings for an investment account. A pass Plaid triggers by webhook has news in it, so balances come with it: one call. For a bank whose webhooks are
    live, add those passes (about one a day for a checking account or card, about one a month for savings or investments); recurring then arrives by webhook only when it changed."""
    import time
    iv = interval(db, row, floor_seconds, now)
    if iv is None:
        return 0.0
    inv = 1 if any(a["kind"] == "investment" for a in _accounts(db, row["item_id"])) else 0
    live = hook_live(row, now if now is not None else time.time())
    polled = (2 + inv) * 86400 / max(iv, 900)
    pushed = (1 + inv) * (1.0 if has_everyday(db, row["item_id"]) else 1 / 30) if live else 0.0
    rec = (1 / 30 if live else 1 / 7) if recurring_on(db, row, global_on) else 0.0
    return polled + pushed + rec
