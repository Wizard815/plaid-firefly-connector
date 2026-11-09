"""Pull data from Plaid into the goblin DB and record money-movement events."""
from datetime import datetime, timezone

from goblinbank import crypto, ledger, recur, settings
from goblinbank.plaid import Plaid

KIND = {"depository": "cash", "credit": "credit", "investment": "investment", "loan": "loan", "brokerage": "investment"}
CYCLE = {"WEEKLY": (1, "week"), "BIWEEKLY": (2, "week"), "SEMI_MONTHLY": (1, "month"), "MONTHLY": (1, "month"), "ANNUALLY": (1, "year")}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(v: float, cur: str = "USD") -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}" if cur == "USD" else f"{v:+,.2f} {cur}"


def record_balance(db, acct_id: str, name: str, balance: float | None, kind: str):
    """Snapshot a balance and emit an event when it changed."""
    if balance is None:
        return
    prev = db.one("SELECT balance FROM snapshots WHERE account_id=? ORDER BY ts DESC LIMIT 1", (acct_id,))
    if prev is not None and abs(prev["balance"] - balance) < 0.005:
        return
    ts = now()
    with db.tx() as c:
        c.execute("INSERT INTO snapshots VALUES(?,?,?)", (ts, acct_id, balance))
        if prev is not None:
            d = balance - prev["balance"]
            c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                      (f"bal:{acct_id}:{ts}", ts, "balance", f"{name}: balance {money(d)}", d, acct_id,
                       f"{prev['balance']:,.2f} -> {balance:,.2f}"))


import time

from goblinbank import plaidplan

clock = time.time          # tests replace this


def sync_item(db, plaid: Plaid, item_id: str, force: bool = False) -> dict:
    """Pull one bank. Unless `force` (first link, a Plaid webhook, or you pressing Sync now) a bank is skipped until its own schedule says it is due
    (daily for a checking account or card, weekly for savings, monthly for an investment account, or whatever you chose), and recurring payments
    are asked for at most weekly and only on banks where they are switched on."""
    row = db.one("SELECT * FROM plaid_items WHERE item_id=?", (item_id,))
    if not row:
        return {"item": item_id, "error": "unknown item"}
    t0 = clock()
    floor = float(settings.get(db, "plaid_min_minutes") or 0) * 60
    gap = plaidplan.interval(db, row, floor)
    if not force and row.get("last_sync"):
        if gap is None:
            return {"item": item_id, "skipped": "manual"}
        if t0 - row["last_sync"] < gap:
            return {"item": item_id, "skipped": "not due", "next_in_minutes": round((gap - (t0 - row["last_sync"])) / 60)}
    token = crypto.decrypt(row["token_enc"])
    inst = row["institution"] or "Bank"
    stats = {"item": item_id, "added": 0, "modified": 0, "removed": 0}

    for a in plaid.accounts(token)["accounts"]:
        kind = KIND.get(a["type"], "cash")
        bal = a["balances"].get("current")
        with db.tx() as c:
            c.execute("""INSERT INTO accounts(id,item_id,source,institution,name,kind,balance,currency,updated,subtype,mask) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,name=excluded.name,updated=excluded.updated,subtype=excluded.subtype,mask=excluded.mask""",
                      (a["account_id"], item_id, "plaid", inst, a.get("official_name") or a["name"], kind, bal,
                       a["balances"].get("iso_currency_code") or "USD", now(), a.get("subtype"), a.get("mask")))
        record_balance(db, a["account_id"], a["name"], bal, kind)

    cursor, more = row["cursor"], True
    while more:
        r = plaid.sync(token, cursor)
        for t in r.get("added", []) + r.get("modified", []):
            amt = -float(t["amount"])        # Plaid: positive = outflow. We store positive = inflow.
            is_new = db.one("SELECT 1 FROM transactions WHERE id=?", (t["transaction_id"],)) is None
            pfc = t.get("personal_finance_category") or {}
            pcat = pfc.get("detailed") or pfc.get("primary") or ""
            akind = (db.one("SELECT kind FROM accounts WHERE id=?", (t["account_id"],)) or {}).get("kind")
            cat = ledger.categorize(db, t.get("merchant_name") or t["name"], amt, pcat, akind)
            with db.tx() as c:
                c.execute("""INSERT INTO transactions(id,account_id,date,name,amount,currency,category,pending,source,plaid_category) VALUES(?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id, date=excluded.date, name=excluded.name, amount=excluded.amount,
                               pending=excluded.pending, plaid_category=excluded.plaid_category,
                               category=CASE WHEN transactions.category_locked=1 THEN transactions.category ELSE excluded.category END""",
                          (t["transaction_id"], t["account_id"], t["date"], t.get("merchant_name") or t["name"], amt,
                           t.get("iso_currency_code") or "USD", cat, int(t.get("pending", False)), "plaid", pcat))
                if is_new:
                    c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                              (f"tx:{t['transaction_id']}", f"{t['date']}T00:00:00+00:00", "transaction",
                               f"{inst}: {t.get('merchant_name') or t['name']} {money(amt)}", amt, t["account_id"],
                               "pending" if t.get("pending") else "posted"))
            stats["added" if is_new else "modified"] += 1
        for t in r.get("removed", []):
            with db.tx() as c:
                c.execute("DELETE FROM transactions WHERE id=?", (t["transaction_id"],))
            stats["removed"] += 1
        cursor, more = r["next_cursor"], r.get("has_more", False)
    with db.tx() as c:
        c.execute("UPDATE plaid_items SET cursor=? WHERE item_id=?", (cursor, item_id))

    if db.one("SELECT 1 FROM accounts WHERE item_id=? AND kind='investment'", (item_id,)):
        try:
            stats["holdings"] = sync_holdings(db, plaid, token)
        except Exception as e:  # not every institution supports investments
            stats["holdings_error"] = str(e)[:120]
    row = db.one("SELECT * FROM plaid_items WHERE item_id=?", (item_id,))          # the accounts are stored now, so what the bank holds decides
    rec_due = plaidplan.recurring_on(db, row, settings.get(db, "plaid_use_recurring") != "off") and (not row.get("last_slow") or t0 - row["last_slow"] >= plaidplan.RECURRING_EVERY)
    try:
        if rec_due:                                       # Plaid bills Recurring Transactions per bank per month; the app's own detection works without it
            sync_recurring(db, plaid, token)
    except Exception as e:  # recurring needs history; don't fail the whole sync
        stats["recurring_error"] = str(e)[:120]
    with db.tx() as c:
        c.execute("UPDATE plaid_items SET last_sync=?" + (", last_slow=?" if rec_due else "") + " WHERE item_id=?", (t0, t0, item_id) if rec_due else (t0, item_id))
    return stats


def sync_holdings(db, plaid: Plaid, token: str) -> int:
    r = plaid.holdings(token)
    secs = {x["security_id"]: x for x in r.get("securities", [])}
    n = 0
    for h in r.get("holdings", []):
        sec = secs.get(h["security_id"], {})
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?,?)",
                      (h["account_id"], sec.get("ticker_symbol") or sec.get("name") or h["security_id"], sec.get("name"),
                       float(h["quantity"]), float(h.get("institution_price") or 0), float(h.get("institution_value") or 0)))
        n += 1
    return n


def sync_recurring(db, plaid: Plaid, token: str):
    ids = [a["id"] for a in db.all("SELECT id FROM accounts WHERE source='plaid'")]
    r = plaid.recurring(token, ids)
    for s in r.get("outflow_streams", []):
        if not s.get("is_active", True) or s["frequency"] not in CYCLE:
            continue
        amt = abs(float((s.get("last_amount") or s.get("average_amount") or {}).get("amount", 0)))
        nxt = s.get("predicted_next_date") or s["last_date"]
        with db.tx() as c:
            ev, un = CYCLE[s["frequency"]]
            c.execute("""INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,category,source,plaid_stream_id,created)
                         VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(plaid_stream_id) DO UPDATE SET amount=excluded.amount,next_date=excluded.next_date""",
                      (s.get("merchant_name") or s["description"], amt, recur.label(ev, un), ev, un, nxt, "Subscriptions", "plaid", s["stream_id"], now()))


def sync_all(db, plaid: Plaid, force: bool = False) -> list[dict]:
    return [sync_item(db, plaid, r["item_id"], force) for r in db.all("SELECT item_id FROM plaid_items")]
