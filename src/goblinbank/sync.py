"""Pull data from Plaid into the goblin DB and record money-movement events."""
from datetime import datetime, timezone

from goblinbank import crypto
from goblinbank.plaid import Plaid

KIND = {"depository": "cash", "credit": "credit", "investment": "investment", "loan": "loan", "brokerage": "investment"}
CYCLE = {"WEEKLY": "weekly", "BIWEEKLY": "biweekly", "SEMI_MONTHLY": "monthly", "MONTHLY": "monthly", "ANNUALLY": "yearly"}


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


def sync_item(db, plaid: Plaid, item_id: str) -> dict:
    row = db.one("SELECT * FROM plaid_items WHERE item_id=?", (item_id,))
    if not row:
        return {"item": item_id, "error": "unknown item"}
    token = crypto.decrypt(row["token_enc"])
    inst = row["institution"] or "Bank"
    stats = {"item": item_id, "added": 0, "modified": 0, "removed": 0}

    for a in plaid.accounts(token)["accounts"]:
        kind = KIND.get(a["type"], "cash")
        bal = a["balances"].get("current")
        with db.tx() as c:
            c.execute("""INSERT INTO accounts(id,source,institution,name,kind,balance,currency,updated) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,name=excluded.name,updated=excluded.updated""",
                      (a["account_id"], "plaid", inst, a.get("official_name") or a["name"], kind, bal,
                       a["balances"].get("iso_currency_code") or "USD", now()))
        record_balance(db, a["account_id"], a["name"], bal, kind)

    cursor, more = row["cursor"], True
    while more:
        r = plaid.sync(token, cursor)
        for t in r.get("added", []) + r.get("modified", []):
            amt = -float(t["amount"])        # Plaid: positive = outflow. We store positive = inflow.
            is_new = db.one("SELECT 1 FROM transactions WHERE id=?", (t["transaction_id"],)) is None
            with db.tx() as c:
                c.execute("INSERT OR REPLACE INTO transactions VALUES(?,?,?,?,?,?,?,?,?)",
                          (t["transaction_id"], t["account_id"], t["date"], t.get("merchant_name") or t["name"], amt,
                           t.get("iso_currency_code") or "USD",
                           (t.get("personal_finance_category") or {}).get("primary", ""), int(t.get("pending", False)), "plaid"))
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

    try:
        sync_recurring(db, plaid, token)
    except Exception as e:  # recurring needs history; don't fail the whole sync
        stats["recurring_error"] = str(e)[:120]
    return stats


def sync_recurring(db, plaid: Plaid, token: str):
    ids = [a["id"] for a in db.all("SELECT id FROM accounts WHERE source='plaid'")]
    r = plaid.recurring(token, ids)
    for s in r.get("outflow_streams", []):
        if not s.get("is_active", True) or s["frequency"] not in CYCLE:
            continue
        amt = abs(float((s.get("last_amount") or s.get("average_amount") or {}).get("amount", 0)))
        nxt = s.get("predicted_next_date") or s["last_date"]
        with db.tx() as c:
            c.execute("""INSERT INTO subscriptions(name,amount,cycle,next_date,category,source,plaid_stream_id)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(plaid_stream_id) DO UPDATE SET amount=excluded.amount,next_date=excluded.next_date""",
                      (s.get("merchant_name") or s["description"], amt, CYCLE[s["frequency"]], nxt,
                       (s.get("personal_finance_category") or {}).get("primary", "").title(), "plaid", s["stream_id"]))


def sync_all(db, plaid: Plaid) -> list[dict]:
    return [sync_item(db, plaid, r["item_id"]) for r in db.all("SELECT item_id FROM plaid_items")]
