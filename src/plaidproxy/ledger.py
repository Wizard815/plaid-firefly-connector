"""All money logic in one place. The HTTP routes and the agent's tools both call these, so they can never disagree.
Signed amounts everywhere: positive = money in, negative = money out."""
import re
from datetime import date, timedelta

from plaidproxy import recur, settings as cfg

OUT_KINDS = {"credit", "loan"}


def d(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(s)


# ------------------------------------------------------------------ categories
PLAID_MAP = {  # detailed first, then primary
    "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES": "Clothes", "TRANSPORTATION_GAS": "Gas", "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "RENT_AND_UTILITIES_RENT": "Housing", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Transfer", "ENTERTAINMENT_VIDEO_GAMES": "Games",
    "INCOME": "Income", "LOAN_DISBURSEMENTS": "Transfer", "TRANSFER_IN": "Transfer", "TRANSFER_OUT": "Transfer", "LOAN_PAYMENTS": "Bills", "FOOD_AND_DRINK": "Food",
    "ENTERTAINMENT": "Fun", "TRANSPORTATION": "Transport", "MEDICAL": "Health", "PERSONAL_CARE": "Health", "RENT_AND_UTILITIES": "Bills",
    "HOME_IMPROVEMENT": "Housing", "TRAVEL": "Travel", "GENERAL_MERCHANDISE": "Shopping", "GENERAL_SERVICES": "Misc",
    "BANK_FEES": "Misc", "GOVERNMENT_AND_NON_PROFIT": "Misc", "OTHER": "Misc",
}


KEYWORDS = {  # last-resort guesses for payees with no bank category (CSV imports, manual entries). Your own rules always win.
    "Groceries": ("kroger", "aldi", "trader joe", "costco", "walmart", "safeway", "publix", "whole foods", "wholefds", "heb ", "meijer", "grocery"),
    "Gas": ("shell", "marathon", "sheetz", "exxon", "chevron", "bp ", "speedway", "wawa", "valero", "citgo", "gasoline", "fuel"),
    "Food": ("starbucks", "chipotle", "domino", "pizza", "panera", "wendy", "five guys", "mcdonald", "burger", "taco", "subway", "coffee", "diner", "restaurant", "doordash", "grubhub", "cafe"),
    "Clothes": ("zara", "old navy", "h&m", "nike", "gap ", "uniqlo", "nordstrom", "tj maxx", "marshalls", "ross "),
    "Games": ("steam", "xbox", "playstation", "nintendo", "epic games", "blizzard"),
    "Subscriptions": ("netflix", "spotify", "hulu", "disney", "hbo", "apple.com/bill", "icloud", "youtube premium", "patreon"),
    "Transport": ("uber", "lyft", "metro", "transit", "parking", "toll"),
    "Health": ("cvs", "walgreens", "pharmacy", "dental", "clinic", "hospital", "optical"),
    "Housing": ("home depot", "lowes", "lowe's", "ikea", "rent "),
    "Shopping": ("amazon", "amzn", "target", "best buy", "etsy", "ebay"),
    "Bills": ("water", "electric", "utility", "insurance", "comcast", "verizon", "at&t", "t-mobile"),
    "Income": ("payroll", "salary", "direct dep", "refund", "interest paid", "dividend"),
}


def categorize(db, name: str, amount: float, plaid_cat: str | None = None, account_kind: str | None = None) -> str:
    low = (name or "").lower()
    for r in db.all("SELECT pattern, category FROM category_rules ORDER BY length(pattern) DESC"):
        if r["pattern"].lower() in low:
            return r["category"]
    # Money arriving on a card or loan is a payment toward the debt, not income and not new money: the other side of a transfer.
    if account_kind in ("credit", "loan") and amount > 0 and plaid_cat and plaid_cat.startswith(("LOAN_PAYMENTS", "TRANSFER")):
        return "Transfer"
    if plaid_cat:
        for key in (plaid_cat, plaid_cat.split("_")[0]):
            if key in PLAID_MAP:
                return PLAID_MAP[key]
        # detailed codes look like FOOD_AND_DRINK_COFFEE: walk back to the primary
        parts = plaid_cat.split("_")
        for n in range(len(parts) - 1, 0, -1):
            if "_".join(parts[:n]) in PLAID_MAP:
                return PLAID_MAP["_".join(parts[:n])]
    low = f" {low} "
    for cat, words in KEYWORDS.items():
        if any(w in low for w in words) and (cat == "Income") == (amount > 0 or cat == "Income" and "refund" in low):
            return cat
    return "Income" if amount > 0 else "Misc"


def set_category(db, tx_id: str, category: str, make_rule: bool = False) -> dict:
    row = db.one("SELECT * FROM transactions WHERE id=?", (tx_id,))
    if not row:
        raise KeyError(tx_id)
    if not db.one("SELECT 1 FROM categories WHERE name=?", (category,)):
        raise ValueError(f"unknown category {category!r}")
    changed = 1
    with db.tx() as c:
        c.execute("UPDATE transactions SET category=?, category_locked=1 WHERE id=?", (category, tx_id))
        if make_rule:
            pat = recur.norm(row["name"]) or row["name"].lower()
            c.execute("INSERT INTO category_rules(pattern,category) VALUES(?,?) ON CONFLICT(pattern) DO UPDATE SET category=excluded.category", (pat, category))
            changed += c.execute("UPDATE transactions SET category=? WHERE category_locked=0 AND lower(name) LIKE ?", (category, f"%{pat}%")).rowcount
    return {"id": tx_id, "category": category, "updated": changed}


def recategorize(db) -> int:
    """Re-run rules over every transaction the user has not set by hand."""
    n = 0
    for t in db.all("SELECT id, name, amount, plaid_category, category FROM transactions WHERE category_locked=0"):
        new = categorize(db, t["name"], t["amount"], t["plaid_category"])
        if new != t["category"]:
            with db.tx() as c:
                c.execute("UPDATE transactions SET category=? WHERE id=?", (new, t["id"]))
            n += 1
    return n


# ------------------------------------------------------------------ transactions
def query_transactions(db, q=None, account_id=None, category=None, min_amount=None, max_amount=None, start=None, end=None,
                       direction=None, limit=100, offset=0, sort="date_desc") -> dict:
    where, args = ["1=1"], []
    if q:
        for tok in q.split():
            where.append("lower(t.name) LIKE ?")
            args.append(f"%{tok.lower()}%")
    if account_id:
        where.append("t.account_id=?"); args.append(account_id)
    if category:
        cats = [c for c in category.split(",") if c]
        where.append(f"t.category IN ({','.join('?' * len(cats))})"); args += cats
    if min_amount is not None:      # compares magnitude, so "over $50" works for spending and income alike
        where.append("abs(t.amount) >= ?"); args.append(min_amount)
    if max_amount is not None:
        where.append("abs(t.amount) <= ?"); args.append(max_amount)
    if start:
        where.append("t.date >= ?"); args.append(start)
    if end:
        where.append("t.date <= ?"); args.append(end)
    if direction == "in":
        where.append("t.amount > 0")
    elif direction == "out":
        where.append("t.amount < 0")
    w = " AND ".join(where)
    order = {"date_desc": "t.date DESC, t.id", "date_asc": "t.date ASC, t.id", "amount_desc": "abs(t.amount) DESC, t.id", "amount_asc": "abs(t.amount) ASC, t.id"}.get(sort, "t.date DESC, t.id")
    tot = db.one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN t.amount>0 THEN t.amount END),0) s_in, COALESCE(SUM(CASE WHEN t.amount<0 THEN t.amount END),0) s_out, "
                 "COALESCE(SUM(CASE WHEN t.amount>0 AND coalesce(c.kind,'')!='transfer' THEN t.amount END),0) r_in, COALESCE(SUM(CASE WHEN t.amount<0 AND coalesce(c.kind,'')!='transfer' THEN t.amount END),0) r_out "
                 f"FROM transactions t LEFT JOIN categories c ON c.name=t.category WHERE {w}", args)
    rows = db.all(f"SELECT t.*, a.name account_name, a.institution FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                  args + [max(1, min(int(limit), 500)), max(0, int(offset))])
    return {"items": rows, "total": tot["n"], "sum_in": round(tot["s_in"], 2), "sum_out": round(tot["s_out"], 2), "net": round(tot["s_in"] + tot["s_out"], 2),
            "real_in": round(tot["r_in"], 2), "real_out": round(tot["r_out"], 2)}      # the same sums not counting transfers (moving your own money around)


def spending_by_category(db, start=None, end=None) -> list[dict]:
    """Money out per category. Transfers are excluded so paying a card is not counted as spending twice."""
    w, a = "t.amount<0 AND c.kind='expense'", []
    if start:
        w += " AND t.date>=?"; a.append(start)
    if end:
        w += " AND t.date<=?"; a.append(end)
    rows = db.all(f"SELECT t.category, ROUND(-SUM(t.amount),2) spent, COUNT(*) n FROM transactions t JOIN categories c ON c.name=t.category WHERE {w} GROUP BY t.category ORDER BY spent DESC", a)
    return rows


# ------------------------------------------------------------------ subscriptions
def sub_view(row: dict, today: date) -> dict:
    every, unit = row.get("every") or 1, row.get("unit") or "month"
    stop = d(row["end_date"]) if row.get("end_date") else None
    nd = recur.next_due(d(row["next_date"]), every, unit, today, stop)
    return {**row, "every": every, "unit": unit, "label": recur.label(every, unit), "next_due": nd.isoformat() if nd else None,
            "monthly": round(recur.monthly_equiv(row["amount"], every, unit), 2) if row.get("active", 1) and row.get("payer", "me") == "me" else 0.0}


SUB_SELECT = "SELECT s.*, a.name account_name, a.institution account_institution FROM subscriptions s LEFT JOIN accounts a ON a.id=s.account_id"


def subscriptions(db, today: date) -> list[dict]:
    return [sub_view(s, today) for s in db.all(SUB_SELECT + " ORDER BY s.active DESC, s.name")]


def subscription(db, sid: int, today: date) -> dict | None:
    row = db.one(SUB_SELECT + " WHERE s.id=?", (sid,))
    return sub_view(row, today) if row else None


def upcoming(db, days: int, today: date) -> dict:
    """Every bill falling due in the next `days` days, repeats included (a weekly bill appears each week)."""
    days = max(1, min(int(days), 3650))
    end = today + timedelta(days=days)
    items = []
    for s in db.all("SELECT * FROM subscriptions WHERE active=1"):
        every, unit = s["every"] or 1, s["unit"] or "month"
        stop = d(s["end_date"]) if s.get("end_date") else None
        for occ in recur.occurrences(d(s["next_date"]), every, unit, today, end, stop):
            items.append({"date": occ.isoformat(), "name": s["name"], "amount": s["amount"], "sub_id": s["id"], "account_id": s["account_id"],
                          "payer": s["payer"] or "me", "payer_name": s["payer_name"] or "", "category": s["category"] or "Subscriptions"})
    items.sort(key=lambda i: (i["date"], i["name"]))
    mine = [i for i in items if i["payer"] == "me"]
    by_cat = {}
    for i in mine:
        by_cat[i["category"]] = round(by_cat.get(i["category"], 0) + i["amount"], 2)
    return {"days": days, "from": today.isoformat(), "to": end.isoformat(), "items": items, "count": len(mine),
            "total": round(sum(i["amount"] for i in mine), 2), "others_total": round(sum(i["amount"] for i in items if i["payer"] != "me"), 2), "by_category": by_cat}


def late_payments(db, today: date, grace: int = 3, look_back: int = 3) -> list[dict]:
    """For subscriptions bound to an account: was each expected payment actually seen? Counts consecutive misses."""
    out = []
    for s in db.all("SELECT * FROM subscriptions WHERE active=1 AND account_id IS NOT NULL AND account_id != '' AND coalesce(payer,'me')='me'"):
        acct = db.one("SELECT * FROM accounts WHERE id=?", (s["account_id"],))
        first = db.one("SELECT MIN(date) m, MAX(date) x FROM transactions WHERE account_id=?", (s["account_id"],))
        if not acct or not first["m"]:
            continue
        every, unit = s["every"] or 1, s["unit"] or "month"
        anchor = d(s["next_date"])
        k = recur.first_k_on_or_after(anchor, every, unit, today - timedelta(days=grace))
        misses, missed_dates = 0, []
        for back in range(1, look_back + 1):
            exp = recur.shift(anchor, every, unit, k - back)
            if exp + timedelta(days=grace) >= today:
                continue                                  # still inside the grace window
            if s.get("end_date") and exp > d(s["end_date"]):
                continue
            if d(first["m"]) > exp - timedelta(days=3) or d(first["x"]) < exp + timedelta(days=grace):
                break                                     # we do not have data covering that date, so we cannot call it missed
            # A hit is an outflow near the expected date that is EITHER (almost) exactly the bill's amount OR looks like the same payee at a
            # similar amount (variable bills). Amount alone is not enough: an unrelated $60 purchase must not hide a missing $61 bill.
            cands = db.all("SELECT name, amount FROM transactions WHERE account_id=? AND amount<0 AND date BETWEEN ? AND ?",
                           (s["account_id"], (exp - timedelta(days=3)).isoformat(), (exp + timedelta(days=grace)).isoformat()))
            hit = any(abs(abs(t["amount"]) - s["amount"]) <= max(0.5, 0.01 * s["amount"])
                      or (abs(abs(t["amount"]) - s["amount"]) <= 0.2 * s["amount"] and recur.same_payee(t["name"], s["name"])) for t in cands)
            if hit:
                break                                     # misses must be consecutive, newest first
            misses += 1
            missed_dates.append(exp.isoformat())
        if misses:
            out.append({"sub_id": s["id"], "name": s["name"], "account": acct["name"], "misses": misses, "missed_dates": missed_dates, "amount": s["amount"]})
    return out


# ------------------------------------------------------------------ joint accounts
def mine(a: dict, bal: float | None = None) -> float:
    """My part of an account's balance. A joint account is linked in both people's workers, so each counts only their share (default all of it)."""
    b = a["balance"] if bal is None else bal
    s = a.get("my_share")
    return (b or 0.0) * (100.0 if s is None else s) / 100.0


# ------------------------------------------------------------------ savings
def savings_view(db, row: dict, today: date) -> dict:
    bal = row["balance"] or 0.0
    if row.get("account_id"):
        a = db.one("SELECT balance, my_share FROM accounts WHERE id=?", (row["account_id"],))
        if a and a["balance"] is not None:
            bal = mine(a)
    apy = row.get("apy") or 0.0
    out = {**row, "balance": round(bal, 2), "linked": bool(row.get("account_id")), "yearly_interest": round(bal * apy / 100, 2)}
    if row.get("maturity"):
        days = (d(row["maturity"]) - today).days
        out["days_to_maturity"] = days
        if days >= 0:
            out["value_at_maturity"] = round(bal * (1 + apy / 100 / 365) ** days, 2)
    if row.get("goal"):
        out["goal_progress"] = round(min(1.0, bal / row["goal"]), 3) if row["goal"] else None
    return out


def savings_summary(db, today: date) -> dict:
    items = [savings_view(db, r, today) for r in db.all("SELECT * FROM savings WHERE active=1 ORDER BY kind, name")]
    total = sum(i["balance"] for i in items)
    wapy = sum(i["balance"] * (i.get("apy") or 0) for i in items) / total if total else 0.0
    maturing = [i for i in items if i.get("days_to_maturity") is not None and 0 <= i["days_to_maturity"] <= 60]
    return {"items": items, "total": round(total, 2), "weighted_apy": round(wapy, 3), "yearly_interest": round(sum(i["yearly_interest"] for i in items), 2),
            "maturing_soon": maturing, "by_kind": {k: round(sum(i["balance"] for i in items if i["kind"] == k), 2) for k in sorted({i["kind"] for i in items})}}


# ------------------------------------------------------------------ net worth, projection, alerts
def net_worth(db, today: date | None = None) -> float:
    total = 0.0
    for a in db.all("SELECT kind, balance, my_share FROM accounts"):
        total += mine(a) * (-1 if a["kind"] in OUT_KINDS else 1)
    for s in db.all("SELECT * FROM savings WHERE active=1 AND (account_id IS NULL OR account_id='')"):
        total += s["balance"] or 0                       # savings linked to an account are already counted via that account
    return round(total, 2)


def project_balance(db, days: int, today: date, account_ids: list[str] | None = None, include_income: bool = True) -> dict:
    """Walk forward day by day: start from current cash balances, subtract upcoming bills, add detected steady income."""
    days = max(1, min(int(days), 3650))
    accts = db.all("SELECT * FROM accounts WHERE kind='cash'" if not account_ids else f"SELECT * FROM accounts WHERE id IN ({','.join('?' * len(account_ids))})", account_ids or [])
    ids = {a["id"] for a in accts}
    start = round(sum(mine(a) for a in accts), 2)
    events = []
    for i in upcoming(db, days, today)["items"]:
        if i["payer"] != "me" or (account_ids and i["account_id"] not in ids):
            continue
        if i["account_id"] and i["account_id"] not in ids:
            continue                                      # paid from an account outside this projection
        events.append((i["date"], -i["amount"], i["name"]))
    if include_income:
        for r in recurring_found(db, today):
            if r["direction"] == "in" and (not ids or r["account_id"] in ids):
                for occ in recur.occurrences(d(r["next_date"]), r["every"], r["unit"], today, today + timedelta(days=days)):
                    events.append((occ.isoformat(), r["amount"], r["name"]))
    events.sort()
    bal, low, low_date, timeline = start, start, today.isoformat(), []
    for dt, amt, name in events:
        bal += amt
        timeline.append({"date": dt, "name": name, "amount": round(amt, 2), "balance": round(bal, 2)})
        if bal < low:
            low, low_date = bal, dt
    return {"start": start, "end": round(bal, 2), "days": days, "lowest": round(low, 2), "lowest_date": low_date, "events": timeline,
            "bills_total": round(-sum(a for _, a, _ in events if a < 0), 2), "income_total": round(sum(a for _, a, _ in events if a > 0), 2),
            "note": "Estimate: counts only subscriptions you have entered and steady income the detector found. Everyday spending is not included."}


def recurring_found(db, today: date, account_id: str | None = None, days: int = 800, loose: bool = False) -> list[dict]:
    # Interest accruing on a loan and transfers between your own accounts repeat on a schedule but are not subscriptions or income.
    rows = db.all("SELECT t.id, t.account_id, t.date, t.name, t.amount FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id "
                  "WHERE t.date >= ? AND coalesce(a.kind,'') != 'loan' AND coalesce(t.category,'') != 'Transfer'" + (" AND t.account_id=?" if account_id else "") + " ORDER BY t.date",
                  ((today - timedelta(days=days)).isoformat(),) + ((account_id,) if account_id else ()))
    return recur.detect(rows, today, loose)


def scan_subscriptions(db, today: date, account_id: str | None = None, thorough: bool = True, include_dismissed: bool = False, days: int = 3650) -> dict:
    """A manual look through the transactions (including CSVs you imported) for repeating payments that are not tracked as subscriptions yet.
    Unlike the automatic suggestions it can cover one account, all history, ones you said no to, and a more forgiving match."""
    subs = db.all("SELECT name FROM subscriptions")
    hidden = {r["match_key"] for r in db.all("SELECT match_key FROM subscriptions WHERE match_key IS NOT NULL")}
    said_no = {r["key"] for r in db.all("SELECT key FROM dismissed")}
    n_tx = db.one("SELECT COUNT(*) n, MIN(date) first FROM transactions" + (" WHERE account_id=?" if account_id else ""), (account_id,) if account_id else ())
    found, tracked, skipped_no = [], 0, 0
    for r in recurring_found(db, today, account_id, days, loose=thorough):
        if r["direction"] != "out":
            continue
        if r["key"] in hidden or any(recur.same_payee(r["name"], s["name"]) for s in subs):
            tracked += 1
            continue
        if r["key"] in said_no and not include_dismissed:
            skipped_no += 1
            continue
        found.append({**r, "source": "scan", "dismissed": r["key"] in said_no})
    return {"found": found, "already_tracked": tracked, "previously_dismissed": skipped_no, "transactions_checked": n_tx["n"], "since": n_tx["first"], "thorough": thorough, "account_id": account_id}


def suggestions(db, today: date) -> list[dict]:
    """Detected repeating payments not yet tracked, plus anything the agent proposed. Dismissed ones stay hidden."""
    subs = db.all("SELECT name FROM subscriptions")
    hidden = {r["key"] for r in db.all("SELECT key FROM dismissed")} | {r["match_key"] for r in db.all("SELECT match_key FROM subscriptions WHERE match_key IS NOT NULL")}   # accepted under any name stays hidden
    out = []
    for r in recurring_found(db, today):
        if r["direction"] != "out" or r["key"] in hidden or any(recur.same_payee(r["name"], s["name"]) for s in subs):
            continue
        out.append({**r, "source": "detected"})
    for p in db.all("SELECT * FROM proposals WHERE status='pending'"):
        out.append({"key": f"proposal:{p['id']}", "proposal_id": p["id"], "name": p["name"], "amount": p["amount"], "every": p["every"], "unit": p["unit"],
                    "label": recur.label(p["every"], p["unit"]), "account_id": p["account_id"], "reason": p["reason"], "source": "agent", "direction": "out",
                    "next_date": today.isoformat(), "confidence": None, "occurrences": None})
    return out


def alerts(db, today: date) -> list[dict]:
    """Everything that needs attention, most urgent first. Each alert has a stable `key`: the same situation keeps the same key,
    which is how notifications avoid telling you twice."""
    out = _alerts(db, today)
    gone = {r["key"] for r in db.all("SELECT key FROM dismissed WHERE key LIKE 'alert:%'")}
    for a in out:
        a.setdefault("key", f"{a['kind']}:{a.get('account_id') or a.get('sub_id') or ''}:{a.get('date') or ''}")
    out = [a for a in out if f"alert:{a['key']}" not in gone]
    rank = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda a: rank[a["severity"]])


def _alerts(db, today: date) -> list[dict]:
    out = []
    for it in db.all("SELECT item_id, institution, status, status_detail FROM plaid_items WHERE status IS NOT NULL"):
        gone = it["status"] == "revoked"
        out.append({"kind": "bank_login", "key": f"bank_login:{it['item_id']}", "severity": "warning" if it["status"] == "expiring" else "critical",
                    "title": f"{it['institution'] or 'A bank'} {'was disconnected' if gone else 'needs you to sign in again' if it['status'] == 'error' else 'will need you to sign in again soon'}",
                    "detail": "Open Settings, Connect & import and press Relink. Until then its numbers are out of date." + (f" ({it['status_detail']})" if it.get("status_detail") else "")})
    for a in db.all("SELECT * FROM accounts"):
        bal = a["balance"]
        if bal is None or a["kind"] in OUT_KINDS or a["kind"] == "investment":
            continue
        if bal < 0:
            out.append({"kind": "negative_balance", "severity": "critical", "title": f"{a['name']} is overdrawn", "detail": f"Balance {bal:,.2f}", "account_id": a["id"]})
        elif a.get("min_balance") is not None and bal < a["min_balance"]:
            out.append({"kind": "low_balance", "severity": "warning", "title": f"{a['name']} is below your minimum", "detail": f"{bal:,.2f} vs minimum {a['min_balance']:,.2f}", "account_id": a["id"]})
        bound = db.one("SELECT COUNT(*) n FROM subscriptions WHERE active=1 AND account_id=?", (a["id"],))["n"]
        if bound and bal >= 0:
            p = project_balance(db, 14, today, [a["id"]], include_income=False)
            floor = a["min_balance"] or 0
            if p["lowest"] < floor:
                out.append({"kind": "projected_low", "severity": "warning", "title": f"{a['name']} may run short by {p['lowest_date']}",
                            "detail": f"Bills bound to it take it to {p['lowest']:,.2f} within 14 days", "account_id": a["id"]})
    if cfg.get(db, "alert_untracked") != "off":
        found = [x for x in suggestions(db, today) if x["source"] == "detected"]
        if found:
            out.append({"kind": "untracked_recurring", "severity": "info", "title": f"{len(found)} repeating payment{'s' if len(found) != 1 else ''} not in Subscriptions yet",
                        "detail": ", ".join(x["name"] for x in found[:4]) + (" and more" if len(found) > 4 else ""), "count": len(found)})
    for m in (late_payments(db, today) if cfg.get(db, "alert_missed") != "off" else []):
        out.append({"kind": "missed_payment", "severity": "critical" if m["misses"] >= 2 else "warning", "sub_id": m["sub_id"],
                    "title": f"{m['name']} not seen in {m['account']}" + (f" ({m['misses']} times in a row)" if m["misses"] > 1 else ""),
                    "detail": "Expected " + ", ".join(m["missed_dates"])})
    for s in db.all("SELECT * FROM subscriptions WHERE active=1 AND coalesce(payer,'me')='me'"):
        nd = sub_view(s, today)["next_due"]
        if nd and 0 <= (d(nd) - today).days <= (s["notify_days"] if s["notify_days"] is not None else 3):
            out.append({"kind": "due_soon", "severity": "info", "title": f"{s['name']} due {nd}", "detail": f"{s['amount']:,.2f}", "sub_id": s["id"], "date": nd})
    return out


# ------------------------------------------------------------------ budgets, overview
def budget_status(db, month: str, today: date) -> list[dict]:
    start = f"{month}-01"
    y, m = int(month[:4]), int(month[5:7])
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()
    spent = {r["category"]: r["spent"] for r in spending_by_category(db, start, end)}
    out = []
    for b in db.all("SELECT * FROM budgets ORDER BY category"):
        s = spent.get(b["category"], 0.0)
        out.append({"category": b["category"], "budget": b["monthly_amount"], "spent": s, "left": round(b["monthly_amount"] - s, 2), "pct": round(s / b["monthly_amount"], 3) if b["monthly_amount"] else None})
    return out


def overview(db, days: int, today: date) -> dict:
    accts = db.all("SELECT * FROM accounts")
    by_kind = {}
    for a in accts:
        by_kind[a["kind"]] = round(by_kind.get(a["kind"], 0) + mine(a), 2)
    sav = savings_summary(db, today)
    unlinked = sum(i["balance"] for i in sav["items"] if not i["linked"])
    if unlinked:
        by_kind["savings (manual)"] = round(unlinked, 2)
    up = upcoming(db, days, today)
    subs = db.all("SELECT * FROM subscriptions WHERE active=1")
    monthly = sum(sub_view(s, today)["monthly"] for s in subs)
    return {"net_worth": net_worth(db), "by_kind": by_kind, "accounts": accts, "subscriptions_monthly": round(monthly, 2), "upcoming": up,
            "events": db.all("SELECT * FROM events ORDER BY ts DESC LIMIT 10"), "alerts": alerts(db, today), "savings_total": sav["total"]}


# ------------------------------------------------------------------ spending forecast from history
def _months_back(today: date, n: int) -> list[str]:
    """The n full calendar months before this one, oldest first, as YYYY-MM."""
    y, m, out = today.year, today.month, []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}-{m:02d}")
    return out[::-1]


def spending_forecast(db, today: date, months: int = 6) -> dict:
    """Where this month's spending is heading, from your own history.

    Per category: take your typical month (median of the last full months with data, mean when there are fewer than 4),
    then expect the rest of this month at that rate, nudged by how this month is tracking against the usual pace (clamped
    to 0.5x..2x). Categories made of a few big payments (rent, insurance) are 'lumpy': they are not paced, you simply
    expect the usual amount. A forecast never falls below what is already spent plus bills still due this month.
    Compared against your average monthly income (from history, else the income you entered)."""
    import statistics
    months = max(1, min(int(months), 24))
    hist = _months_back(today, months)
    this = today.strftime("%Y-%m")
    first, last_day = date(today.year, today.month, 1), (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - timedelta(days=1))
    rows = db.all("SELECT substr(t.date,1,7) m, t.category, ROUND(-SUM(t.amount),2) spent, COUNT(*) n FROM transactions t JOIN categories c ON c.name=t.category "
                  "WHERE t.amount<0 AND c.kind='expense' AND t.date >= ? AND t.date <= ? GROUP BY m, t.category", (hist[0] + "-01", last_day.isoformat()))
    inc_rows = db.all("SELECT substr(t.date,1,7) m, ROUND(SUM(t.amount),2) income FROM transactions t JOIN categories c ON c.name=t.category "
                      "WHERE t.amount>0 AND c.kind='income' AND t.date >= ? AND t.date <= ? GROUP BY m", (hist[0] + "-01", last_day.isoformat()))
    any_tx = {r["m"] for r in db.all("SELECT DISTINCT substr(date,1,7) m FROM transactions WHERE date >= ?", (hist[0] + "-01",))}
    with_data = [m for m in hist if m in any_tx]                      # a month with no transactions at all is missing data, not a month of zero spending
    by = {}
    for r in rows:
        by.setdefault(r["category"], {})[r["m"]] = (r["spent"], r["n"])
    income_by_month = {r["m"]: r["income"] for r in inc_rows}
    f = max(1, today.day) / last_day.day                                # how much of the month has passed
    days_left = (last_day - today).days
    # bills still to come this month, by category (not yet paid, because their date has not arrived)
    due = {}
    for i in upcoming(db, max(1, days_left), today)["items"]:
        if i["payer"] == "me" and i["date"] <= last_day.isoformat() and i["date"] > today.isoformat():
            due[i["category"]] = due.get(i["category"], 0) + i["amount"]
    out, total_spent, total_forecast, total_typical = [], 0.0, 0.0, 0.0
    for cat in sorted(set(by) | set(due)):
        spent = by.get(cat, {}).get(this, (0.0, 0))[0]
        past = [by.get(cat, {}).get(m, (0.0, 0)) for m in with_data]
        vals = [p[0] for p in past]
        typical = (statistics.median(vals) if len(vals) >= 4 else statistics.fmean(vals)) if vals and any(vals) else None
        lumpy = typical is not None and statistics.fmean(p[1] for p in past) <= 2.5
        if typical is None:
            fc, mode = spent, "no history"
        elif lumpy:
            fc, mode = max(spent, typical), "usual amount"
        else:
            ratio = min(2.0, max(0.5, spent / (typical * f))) if f >= 0.2 and typical > 0 else 1.0
            fc, mode = spent + typical * (1 - f) * ratio, "pace"
        fc = max(fc, spent + due.get(cat, 0.0))
        out.append({"category": cat, "spent": round(spent, 2), "forecast": round(fc, 2), "typical": round(typical, 2) if typical is not None else None, "bills_due": round(due.get(cat, 0.0), 2), "method": mode})
        total_spent += spent
        total_forecast += fc
        total_typical += typical or 0.0
    inc_hist = [income_by_month[m] for m in with_data if income_by_month.get(m, 0) > 0]
    entered = None
    if inc_hist:
        income_avg, source = round(statistics.fmean(inc_hist), 2), "history"
    elif entered:
        income_avg, source = entered, "entered"
    else:
        income_avg, source = None, None
    out.sort(key=lambda r: -r["forecast"])
    total_forecast = round(total_forecast, 2)
    return {"month": this, "days_left": days_left, "elapsed": round(f, 3), "spent_so_far": round(total_spent, 2), "projected": total_forecast,
            "typical_month": round(total_typical, 2) if with_data else None, "months_used": len(with_data),
            "confidence": "high" if len(with_data) >= 3 else "medium" if len(with_data) >= 1 else "low",
            "income_avg": income_avg, "income_source": source, "income_so_far": round(income_by_month.get(this, 0.0), 2),
            "pct_spent": round(total_spent / income_avg, 3) if income_avg else None, "pct_projected": round(total_forecast / income_avg, 3) if income_avg else None,
            "left_after_projected": round(income_avg - total_forecast, 2) if income_avg is not None else None,
            "categories": out,
            "history": [{"month": m, "spent": round(sum(by.get(c, {}).get(m, (0.0, 0))[0] for c in by), 2), "income": round(income_by_month.get(m, 0.0), 2)} for m in with_data],
            "note": "An estimate from your past months plus bills still due. One-off purchases are not predicted."}
