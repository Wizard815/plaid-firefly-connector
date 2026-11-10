"""Savings places, goals with allocations, income, and the monthly plan.

The model in one paragraph: money SITS in "places" (savings accounts, CDs, anything you track as savings). Goals are
things you want it FOR (a house, a car, a laptop). An allocation earmarks part of the savings for a goal, so "free"
money is what is saved minus what is earmarked. Free money can fund a new goal now; when there is none, the goal is
created unfunded and the plan says how long it takes to save for it."""
import math
from datetime import date, timedelta

from goblinbank import ledger, links, plaidplan, recur

SAVINGS_SUBTYPES = {"savings", "cd", "money market", "cash management", "hsa", "high yield savings", "529", "cd/money market", "money_market"}


class PlanError(ValueError):
    """A request that would break the books (e.g. earmarking money you do not have)."""


def d(s) -> date:
    return ledger.d(s)


# ------------------------------------------------------------------ places: where the savings are
def places(db, today: date) -> list[dict]:
    """Every savings-type account (auto-detected from the bank's account type, or switched on by you) plus
    savings items you track by hand. A hand-tracked item that follows an account replaces that account's own row."""
    out, followed = [], set()
    for row in db.all("SELECT * FROM savings WHERE active=1 ORDER BY kind, name"):
        v = ledger.savings_view(db, row, today)
        if row.get("account_id"):
            followed.add(row["account_id"])
        out.append({"key": f"s:{row['id']}", "id": row["id"], "name": v["name"], "institution": v["institution"], "kind": v["kind"], "balance": v["balance"], "apy": v["apy"],
                    "maturity": v.get("maturity"), "days_to_maturity": v.get("days_to_maturity"), "value_at_maturity": v.get("value_at_maturity"),
                    "origin": "linked" if v["linked"] else "manual", "account_id": row.get("account_id")})
    for a in db.all("SELECT * FROM accounts WHERE kind IN ('cash','investment') ORDER BY institution, name"):
        if a["id"] in followed or a["balance"] is None:
            continue
        pool = a.get("savings_pool")
        cat = a.get("category") or (db.one("SELECT category FROM plaid_items WHERE item_id=?", (a.get("item_id"),)) or {}).get("category")
        grp = plaidplan.category_group(cat)
        auto = (grp == "savings") if grp else (a["kind"] == "cash" and (a.get("subtype") or "").lower() in SAVINGS_SUBTYPES)       # what you called it beats what the bank calls it
        if pool == 0 or not (pool == 1 or auto):                  # investment accounts count only when you switch them on
            continue
        out.append({"key": f"a:{a['id']}", "id": a["id"], "name": a["name"], "institution": a["institution"], "kind": (a.get("subtype") or "savings").replace(" ", "_"),
                    "balance": round(ledger.mine(a), 2), "apy": None, "maturity": None, "days_to_maturity": None, "value_at_maturity": None, "origin": "account", "account_id": a["id"]})
    return out


# ------------------------------------------------------------------ goals and allocations
def _allocs(db):
    return db.all("SELECT a.* FROM allocations a JOIN goals g ON g.id=a.goal_id WHERE g.status='active' AND a.amount>0")


def goals_view(db, today: date) -> dict:
    pl = places(db, today)
    total = round(sum(p["balance"] for p in pl), 2)
    allocs = _allocs(db)
    by_goal, pinned = {}, {}
    for a in allocs:
        by_goal.setdefault(a["goal_id"], []).append(a)
        if a["place"]:
            pinned[a["place"]] = pinned.get(a["place"], 0) + a["amount"]
    allocated = round(sum(a["amount"] for a in allocs), 2)
    free = round(total - allocated, 2)
    for p in pl:
        p["earmarked"] = round(pinned.get(p["key"], 0), 2)
        p["free"] = round(p["balance"] - p["earmarked"], 2)
    items = []
    team_extra = {r["goal_id"]: r["n"] for r in db.all("SELECT p.goal_id, SUM(p.amount) n FROM goal_partners p JOIN goals g ON g.id=p.goal_id WHERE g.shared_state='active' GROUP BY p.goal_id")}   # what the others on a shared goal have set aside
    for g in db.all("SELECT * FROM goals ORDER BY status='done', priority, deadline IS NULL, deadline, id"):
        got = round(sum(a["amount"] for a in by_goal.get(g["id"], [])), 2)
        team = round(got + team_extra.get(g["id"], 0), 2)
        short = round(max(0.0, g["target"] - team), 2)
        v = {**g, "allocated": got, "team_allocated": team, "shortfall": short, "pct": round(min(1.0, team / g["target"]), 3) if g["target"] else 1.0, "funded": short == 0,
             "allocations": [{"place": a["place"], "amount": a["amount"]} for a in by_goal.get(g["id"], [])], "can_fund_now": g["status"] == "active" and free >= short and short > 0}
        if g["status"] == "active" and short > 0:
            if g.get("deadline"):
                days = (d(g["deadline"]) - today).days
                v["days_left"] = days
                v["monthly_needed"] = round(short / max(1, math.ceil(days / 30.4375)), 2) if days > 0 else short
            if g.get("monthly"):
                months = math.ceil(max(0.0, short - max(0.0, free)) / g["monthly"]) if short > free else 0
                v["ready_date"] = (today + timedelta(days=round(months * 30.4375))).isoformat()
        items.append(v)
    links.decorate(db, items)
    active = [i for i in items if i["status"] == "active" and i.get("shared_state") != "offered"]
    return {"items": items, "places": pl, "total": total, "allocated": allocated, "free": free, "overallocated": free < -0.005,
            "needed": round(sum(i["shortfall"] for i in active), 2), "pinned": {k: round(v, 2) for k, v in pinned.items()}}


def _place_free(view: dict, key: str) -> float:
    p = next((x for x in view["places"] if x["key"] == key), None)
    if p is None:
        raise PlanError(f"unknown savings place {key!r}")
    return p["free"]


def _goal(db, gid: int) -> dict:
    g = db.one("SELECT * FROM goals WHERE id=?", (gid,))
    if not g:
        raise KeyError(gid)
    return g


def set_allocation(db, gid: int, place: str, amount: float, today: date) -> dict:
    """Set what this goal has earmarked from one place ('' = from the savings pool as a whole). Refuses to earmark money that is not free."""
    g = _goal(db, gid)
    if g["status"] != "active":
        raise PlanError("that goal is closed")
    if g.get("shared_state") == "offered":
        raise PlanError("accept the shared goal first")
    if amount < 0:
        raise PlanError("amount must be >= 0")
    v = goals_view(db, today)
    cur = db.one("SELECT amount FROM allocations WHERE goal_id=? AND place=?", (gid, place))
    cur = cur["amount"] if cur else 0.0
    delta = amount - cur
    if delta > 0.005:
        if place and delta > _place_free(v, place) + 0.005:
            raise PlanError(f"only ${_place_free(v, place):,.2f} is free in that account; you asked for ${delta:,.2f} more")
        if delta > v["free"] + 0.005:
            raise PlanError(f"only ${max(v['free'], 0):,.2f} of your savings is free; you asked to earmark ${delta:,.2f} more")
    with db.tx() as c:
        if amount <= 0.005:
            c.execute("DELETE FROM allocations WHERE goal_id=? AND place=?", (gid, place))
        else:
            c.execute("INSERT INTO allocations(goal_id,place,amount) VALUES(?,?,?) ON CONFLICT(goal_id,place) DO UPDATE SET amount=excluded.amount", (gid, place, round(amount, 2)))
    return next(i for i in goals_view(db, today)["items"] if i["id"] == gid)


def fund(db, gid: int, amount: float, today: date, place: str = "") -> dict:
    cur = db.one("SELECT amount FROM allocations WHERE goal_id=? AND place=?", (gid, place))
    return set_allocation(db, gid, place, (cur["amount"] if cur else 0.0) + amount, today)


def release(db, gid: int, today: date, amount: float | None = None, place: str | None = None) -> dict:
    """Give earmarked money back to the free pool (all of it, or `amount` from `place`)."""
    _goal(db, gid)
    if amount is None:
        with db.tx() as c:
            c.execute("DELETE FROM allocations WHERE goal_id=?", (gid,))
    else:
        cur = db.one("SELECT amount FROM allocations WHERE goal_id=? AND place=?", (gid, place or ""))
        set_allocation(db, gid, place or "", max(0.0, (cur["amount"] if cur else 0.0) - amount), today)
    return next(i for i in goals_view(db, today)["items"] if i["id"] == gid)


def add_goal(db, name: str, target: float, today: date, deadline: str | None = None, priority: int = 2, monthly: float = 0.0, note: str = "", auto_allocate: bool = False) -> dict:
    if not name.strip():
        raise PlanError("name required")
    if not target > 0:
        raise PlanError("target must be greater than 0")
    deadline = deadline or None
    if deadline:
        try:
            if d(deadline) <= today:
                raise PlanError("deadline must be in the future")
        except ValueError:
            raise PlanError("deadline must be YYYY-MM-DD")
    if priority not in (1, 2, 3):
        raise PlanError("priority must be 1 (high), 2 or 3 (low)")
    if monthly < 0:
        raise PlanError("monthly must be >= 0")
    with db.tx() as c:
        gid = c.execute("INSERT INTO goals(name,target,deadline,priority,monthly,note,created) VALUES(?,?,?,?,?,?,?)", (name.strip(), round(target, 2), deadline, priority, monthly, note, today.isoformat())).lastrowid
    if auto_allocate:                                     # only when asked: free savings are shared by everything until you assign them
        free = goals_view(db, today)["free"]
        if free > 0.005:
            set_allocation(db, gid, "", min(free, target), today)
    return next(i for i in goals_view(db, today)["items"] if i["id"] == gid)


def update_goal(db, gid: int, today: date, **f) -> dict:
    g = _goal(db, gid)
    new = {**g, **{k: v for k, v in f.items() if v is not None}}
    if not new["name"].strip() or not new["target"] > 0:
        raise PlanError("name and a target above 0 are required")
    if new["deadline"]:
        try:
            d(new["deadline"])
        except ValueError:
            raise PlanError("deadline must be YYYY-MM-DD")
    if new["priority"] not in (1, 2, 3) or new["monthly"] < 0:
        raise PlanError("bad priority or monthly amount")
    with db.tx() as c:
        c.execute("UPDATE goals SET name=?,target=?,deadline=?,priority=?,monthly=?,note=? WHERE id=?", (new["name"].strip(), round(new["target"], 2), new["deadline"] or None, new["priority"], new["monthly"], new["note"] or "", gid))
    return next(i for i in goals_view(db, today)["items"] if i["id"] == gid)


def close_goal(db, gid: int, today: date, status: str = "done") -> dict:
    """Mark a goal bought (or dropped). Its earmark is released; if you really spent the money, your savings balance drops when the bank updates."""
    _goal(db, gid)
    with db.tx() as c:
        c.execute("DELETE FROM allocations WHERE goal_id=?", (gid,))
        c.execute("UPDATE goals SET status=?, closed=? WHERE id=?", (status, today.isoformat(), gid))
    return next(i for i in goals_view(db, today)["items"] if i["id"] == gid)


def delete_goal(db, gid: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM allocations WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM chat_links WHERE kind='goal' AND ref_id=?", (gid,))
        c.execute("DELETE FROM goals WHERE id=?", (gid,))


def move(db, src: int, dst: int, amount: float, today: date) -> dict:
    """Re-earmark money from one goal to another (pool allocations only move as pool money)."""
    if src == dst or amount <= 0:
        raise PlanError("pick two different goals and an amount above 0")
    have = db.one("SELECT COALESCE(SUM(amount),0) s FROM allocations WHERE goal_id=?", (src,))["s"]
    if amount > have + 0.005:
        raise PlanError(f"that goal only has ${have:,.2f} earmarked")
    left = amount
    for a in db.all("SELECT * FROM allocations WHERE goal_id=? ORDER BY place=''", (src,)):    # pinned money first
        take = min(left, a["amount"])
        if take > 0:
            set_allocation(db, src, a["place"], a["amount"] - take, today)
            cur = db.one("SELECT amount FROM allocations WHERE goal_id=? AND place=?", (dst, a["place"]))
            set_allocation(db, dst, a["place"], (cur["amount"] if cur else 0) + take, today)
            left -= take
        if left <= 0.005:
            break
    return goals_view(db, today)


def autofill(db, today: date) -> dict:
    """Give free money to active goals in order of priority, then deadline, until it runs out."""
    v = goals_view(db, today)
    free = max(0.0, v["free"])
    for g in v["items"]:
        if free <= 0.005:
            break
        if g["status"] == "active" and g.get("shared_state") != "offered" and g["shortfall"] > 0:
            put = min(free, g["shortfall"])
            fund(db, g["id"], put, today)
            free -= put
    return goals_view(db, today)


# ------------------------------------------------------------------ income and the monthly plan
def income_view(row: dict, today: date) -> dict:
    return {**row, "label": recur.label(row["every"], row["unit"]), "monthly": round(recur.monthly_equiv(row["amount"], row["every"], row["unit"]), 2) if row["active"] else 0.0,
            "next_due": recur.next_due(d(row["next_date"]), row["every"], row["unit"], today).isoformat()}


def incomes(db, today: date) -> list[dict]:
    return [income_view(r, today) for r in db.all("SELECT * FROM incomes ORDER BY active DESC, name")]


def paydays(row: dict, first: date, last: date) -> list[str]:
    """The dates this income actually lands in [first, last]. A biweekly paycheck lands three times in two months of the year."""
    return [x.isoformat() for x in recur.occurrences(d(row["next_date"]), row["every"], row["unit"], first, last)] if row["active"] else []


def income_suggestions(db, today: date) -> list[dict]:
    have = db.all("SELECT name FROM incomes")
    hidden = {r["key"] for r in db.all("SELECT key FROM dismissed")} | {r["match_key"] for r in db.all("SELECT match_key FROM incomes WHERE match_key IS NOT NULL")}
    return [r for r in ledger.recurring_found(db, today) if r["direction"] == "in" and r["key"] not in hidden and not any(recur.same_payee(r["name"], h["name"]) for h in have)]


def monthly_plan(db, month: str, today: date) -> dict:
    """Income against bills, budgets and goal contributions for one month.

    A category with a budget is planned at max(budget, its bills): the bills sit INSIDE the budget, never on top of it.
    Categories with bills but no budget are planned at their bills. Goals add their planned monthly contribution."""
    y, m = int(month[:4]), int(month[5:7])
    start = f"{month}-01"
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()
    inc = incomes(db, today)
    first_day, last_day = date(y, m, 1), date.fromisoformat(end)
    for i in inc:
        i["paydays"] = paydays(i, first_day, last_day)
        i["in_month"] = round(len(i["paydays"]) * i["amount"], 2)
        usual = round(30.4375 / (recur.shift(date(2001, 1, 1), i["every"], i["unit"], 1) - date(2001, 1, 1)).days) if i["unit"] in ("day", "week") else 1
        i["extra_paycheck"] = len(i["paydays"]) > max(1, usual) if i["unit"] in ("day", "week") else False
    income_month = round(sum(i["in_month"] for i in inc), 2)            # what really lands this month (3 paychecks in a 3-payday month)
    income_smooth = round(sum(i["monthly"] for i in inc), 2)           # the steady monthly equivalent, for budgeting and "can I afford"
    bills = {}
    for s in ledger.subscriptions(db, today):
        if s["active"] and s["payer"] == "me":
            bills[s["category"]] = round(bills.get(s["category"], 0) + s["monthly"], 2)
    budgets = {b["category"]: b["monthly_amount"] for b in db.all("SELECT * FROM budgets")}
    spent = {r["category"]: r["spent"] for r in ledger.spending_by_category(db, start, end)}
    rows, warnings = [], []
    for cat in sorted(set(budgets) | set(bills)):
        b, bl = budgets.get(cat), bills.get(cat, 0.0)
        planned = max(b, bl) if b is not None else bl
        if b is not None and bl > b + 0.005:
            warnings.append(f"Your {cat} budget (${b:,.2f}) is lower than its bills (${bl:,.2f}); the plan uses the bills.")
        s = spent.get(cat, 0.0)
        rows.append({"category": cat, "budget": b, "bills": bl, "planned": round(planned, 2), "spent": s, "left": round(planned - s, 2), "pct": round(s / planned, 3) if planned else None})
    outflow = round(sum(r["planned"] for r in rows), 2)
    active = [g for g in db.all("SELECT * FROM goals WHERE status='active'")]
    goals_month = round(sum(g["monthly"] or 0 for g in active), 2)
    left = round(income_month - outflow - goals_month, 2)
    left_smooth = round(income_smooth - outflow - goals_month, 2)
    extra = round(max(0.0, income_month - income_smooth), 2) if any(i["extra_paycheck"] for i in inc) else 0.0
    extra_goal = None
    if extra > 0:                                       # an extra paycheck is a bonus: point it at the highest-priority goal that still needs money
        gs = sorted((g for g in active if g["status"] == "active"), key=lambda g: (g["priority"], g["deadline"] or "9999-12-31", g["id"]))
        got = {a["goal_id"]: a["amount"] for a in _allocs(db)}
        for g in gs:
            need = round(g["target"] - got.get(g["id"], 0.0), 2)
            if need > 0.005:
                extra_goal = {"goal_id": g["id"], "goal": g["name"], "amount": round(min(extra, need), 2)}
                break
    if income_month and left < 0:
        warnings.append(f"The plan spends ${-left:,.2f} more than your expected income.")
    got = db.one("SELECT COALESCE(SUM(t.amount),0) s FROM transactions t JOIN categories c ON c.name=t.category WHERE t.amount>0 AND c.kind='income' AND t.date BETWEEN ? AND ?", (start, end))["s"]
    other_spent = round(sum(v for k, v in spent.items() if k not in {r["category"] for r in rows}), 2)
    return {"month": month, "income_month": income_month, "income_smoothed": income_smooth, "left_smoothed": left_smooth, "extra_paycheck_month": any(i["extra_paycheck"] for i in inc), "extra_income": extra, "extra_for_goal": extra_goal, "incomes": inc, "outflow_planned": outflow, "goals_month": goals_month, "left": left, "rows": rows, "warnings": warnings,
            "actual": {"income": round(got, 2), "spent": round(sum(spent.values()), 2), "unbudgeted_spent": other_spent},
            "income_suggestions": income_suggestions(db, today)}


def afford(db, amount: float, today: date) -> dict:
    """Could I buy this? Uses FREE savings (not earmarked) and the monthly plan's leftover to say how long saving would take."""
    v = goals_view(db, today)
    free = max(0.0, v["free"])
    plan = monthly_plan(db, today.strftime("%Y-%m"), today)
    surplus = plan["left_smoothed"]            # a three-paycheck month is a bonus, not the pace you can count on
    short = round(max(0.0, amount - free), 2)
    out = {"amount": round(amount, 2), "free_savings": round(free, 2), "earmarked": v["allocated"], "total_savings": v["total"], "can_buy_now": short == 0, "shortfall": short, "monthly_left_in_plan": surplus}
    if short:
        if surplus > 0:
            months = math.ceil(short / surplus)
            out.update({"months_to_save": months, "ready_date": (today + timedelta(days=round(months * 30.4375))).isoformat()})
        else:
            out["note"] = "The monthly plan has nothing left over, so there is no saving pace to measure. Raise income or trim a budget, or set a monthly amount on the goal."
    else:
        out["note"] = "Free savings cover it. Buying it would leave $%.2f free." % (free - amount)
    return out


def maturity_outlook(db, today: date, horizon_days: int = 365) -> list[dict]:
    """For every CD / bond / savings item with a maturity date: what it is worth then, what is already pinned to goals,
    what is free (interest plus unpinned principal), and which goals that free money would reach, in priority order."""
    v = goals_view(db, today)
    goals = [g for g in v["items"] if g["status"] == "active" and g.get("shared_state") != "offered"]
    order = sorted(goals, key=lambda g: (g["priority"], g["deadline"] or "9999-12-31", g["id"]))
    short = {g["id"]: g["shortfall"] for g in goals}
    names = {g["id"]: g["name"] for g in goals}
    pin = {}
    for a in _allocs(db):
        if a["place"]:
            pin.setdefault(a["place"], []).append({"goal_id": a["goal_id"], "goal": names.get(a["goal_id"], "?"), "amount": a["amount"]})
    best = max((p for p in v["places"] if p.get("apy")), key=lambda p: p["apy"], default=None)
    out = []
    for p in sorted((p for p in v["places"] if p.get("maturity") and p["days_to_maturity"] is not None and 0 <= p["days_to_maturity"] <= horizon_days), key=lambda p: p["maturity"]):
        value = p["value_at_maturity"] or p["balance"]
        pinned = pin.get(p["key"], [])
        free_at = round(max(0.0, value - sum(x["amount"] for x in pinned)), 2)
        rest, sugg = free_at, []
        for g in order:
            if rest <= 0.005:
                break
            if short.get(g["id"], 0) > 0.005:
                put = round(min(rest, short[g["id"]]), 2)
                sugg.append({"goal_id": g["id"], "goal": g["name"], "amount": put})
                short[g["id"]] -= put
                rest -= put
        rest = round(rest, 2)
        if rest > 0.005:
            tip = f"Nothing else needs it. Roll it into a new CD or move it to {best['name']} ({best['apy']}% APY)." if best and best["key"] != p["key"] else "Nothing else needs it. Roll it into a new CD or a high-yield savings account."
        elif sugg:
            tip = "All of the free money is spoken for by your goals."
        else:
            tip = "Everything on it is already pinned to goals."
        out.append({"key": p["key"], "name": p["name"], "institution": p["institution"], "maturity": p["maturity"], "days": p["days_to_maturity"], "balance": p["balance"], "value_at_maturity": round(value, 2),
                    "interest": round(value - p["balance"], 2), "pinned": pinned, "free_at_maturity": free_at, "suggestions": sugg, "leftover": rest, "advice": tip})
    return out


def matured_idle(db, today: date, within_days: int = 60) -> list[dict]:
    """CDs and bonds whose maturity date has passed but which are still listed with a balance: the money is sitting there with no new plan.
    Shows what is pinned to goals and where the free part would reach, so the alert says what to do."""
    v = goals_view(db, today)
    goals = [g for g in v["items"] if g["status"] == "active" and g.get("shared_state") != "offered"]
    order = sorted(goals, key=lambda g: (g["priority"], g["deadline"] or "9999-12-31", g["id"]))
    short = {g["id"]: g["shortfall"] for g in goals}
    names = {g["id"]: g["name"] for g in goals}
    pin = {}
    for a in _allocs(db):
        if a["place"]:
            pin.setdefault(a["place"], []).append({"goal": names.get(a["goal_id"], "?"), "amount": a["amount"]})
    out = []
    for p in sorted((p for p in v["places"] if p.get("maturity")), key=lambda p: p["maturity"]):
        ago = (today - d(p["maturity"])).days
        if ago < 0 or ago > within_days or p["balance"] <= 0.005:
            continue
        pinned = pin.get(p["key"], [])
        free = round(max(0.0, p["balance"] - sum(x["amount"] for x in pinned)), 2)
        rest, sugg = free, []
        for g in order:
            if rest <= 0.005:
                break
            if short.get(g["id"], 0) > 0.005:
                put = round(min(rest, short[g["id"]]), 2)
                sugg.append({"goal": g["name"], "amount": put})
                short[g["id"]] -= put
                rest -= put
        out.append({"key": p["key"], "name": p["name"], "maturity": p["maturity"], "days_ago": ago, "balance": p["balance"], "pinned": pinned, "free": free, "suggestions": sugg, "leftover": round(rest, 2)})
    return out
