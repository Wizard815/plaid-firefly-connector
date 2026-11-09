"""CSV importers for accounts with no usable API: PayPal, Venmo, Cash App.

Honest scope: Venmo and Cash App have no public API for personal accounts, and PayPal's
Transaction Search API needs a business account. Download the CSV from each app and import it.
Signed amounts (positive = in). Re-importing is safe (stable ids, INSERT OR IGNORE).
"""
import csv
import hashlib
import io
import re
from datetime import datetime


def _num(s: str) -> float:
    s = (s or "").strip()
    neg = s.startswith("(") or s.startswith("-") or s.startswith("- ")
    v = re.sub(r"[^0-9.]", "", s)
    return -float(v) if neg and v else float(v or 0)


def _date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10]


def detect(headers: list[str]) -> str | None:
    h = {x.strip().lower() for x in headers}
    if {"transaction id", "transaction type", "net amount"} <= h:
        return "cashapp"
    if {"datetime", "from", "to", "note"} <= h:
        return "venmo"
    if {"gross", "net"} <= h or ({"name", "type", "status"} <= h and "gross" in h):
        return "paypal"
    return None


def parse(text: str, provider: str | None = None) -> tuple[str, list[dict]]:
    text = text.lstrip("\ufeff")
    if provider == "venmo":  # Venmo exports have preamble lines before the real header row
        lines = text.splitlines()
        i = next((n for n, l in enumerate(lines) if "Datetime" in l and "Note" in l), 0)
        text = "\n".join(lines[i:])
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return provider or "unknown", []
    prov = provider or detect(list(rows[0].keys()))
    if not prov:
        raise ValueError("unrecognized CSV format (expected PayPal, Venmo or Cash App export)")
    out = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if prov == "paypal":
            if r.get("Status", "Completed") not in ("Completed", ""):
                continue
            amt, name, date = _num(r.get("Net") or r.get("Gross", "0")), r.get("Name") or r.get("Type", ""), _date(r.get("Date", ""))
            tid = r.get("Transaction ID", "")
        elif prov == "venmo":
            if not r.get("Datetime") or r.get("Status") not in ("Complete", ""):
                continue
            amt = _num(r.get("Amount (total)") or r.get("Amount", "0"))
            name = (r.get("To") if amt < 0 else r.get("From")) or r.get("Type", "")
            name = f"{name}: {r.get('Note', '')}".strip(": ")
            date, tid = _date(r["Datetime"]), r.get("ID", "")
        else:  # cashapp
            amt, name, date = _num(r.get("Net Amount") or r.get("Amount", "0")), r.get("Notes") or r.get("Transaction Type", ""), _date(r.get("Date", ""))
            tid = r.get("Transaction ID", "")
        tid = tid or hashlib.sha256(f"{date}|{name}|{amt}".encode()).hexdigest()[:16]
        out.append({"id": f"{prov}:{tid}", "date": date, "name": name or prov, "amount": amt})
    return prov, out


def store(db, provider: str, rows: list[dict]) -> int:
    acct = f"manual:{provider}"
    from goblinbank.sync import money, now
    from goblinbank import ledger
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)",
                  (acct, "csv", provider.title(), f"{provider.title()} (CSV import)", "cash", None, now()))
    added = 0
    for r in rows:
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)",
                            (r["id"], acct, r["date"], r["name"], r["amount"], "csv", ledger.categorize(db, r["name"], r["amount"])))
            if cur.rowcount:
                added += 1
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                          (f"tx:{r['id']}", f"{r['date']}T00:00:00+00:00", "transaction",
                           f"{provider.title()}: {r['name']} {money(r['amount'])}", r["amount"], acct, "imported"))
    return added


# ---------------------------------------------------------------- a bank's own CSV, into an account you track by hand
DATE_COLS = ("posted date", "post date", "posting date", "trans. date", "transaction date", "date")
DESC_COLS = ("description", "payee", "memo", "name", "details", "transaction description")


def _col(headers: dict, names: tuple) -> str | None:
    return next((headers[n] for n in names if n in headers), None)


def parse_bank(text: str, flip: bool = False) -> tuple[list[dict], float | None]:
    """Read a CSV downloaded from a bank or card site (Capital One, Discover, Huntington, Axos...). The columns are found by name:
    a date, a description, and either one Amount column or Debit/Credit columns. `flip` is for files where purchases are positive (typical for credit cards).
    Returns the rows (positive = money in) and, if the file has a running Balance column, the balance on its newest row."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    i = next((n for n, l in enumerate(lines) if any(d in l.lower() for d in DATE_COLS) and ("," in l or "\t" in l)), 0)      # some banks put notes above the header
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[i:]))))
    if not rows:
        return [], None
    heads = {k.strip().lower(): k for k in rows[0].keys() if k}
    dcol, ncol = _col(heads, DATE_COLS), _col(heads, DESC_COLS)
    acol, debit, credit, bcol = heads.get("amount"), heads.get("debit"), heads.get("credit"), heads.get("balance")
    if not dcol or not ncol or not (acol or debit or credit):
        raise ValueError("could not find a date, a description and an amount in this file")
    out, seen, newest = [], {}, None
    for r in rows:
        date = _date((r.get(dcol) or "").strip())
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            continue
        name = (r.get(ncol) or "").strip()
        if acol and (r.get(acol) or "").strip():
            amt = _num(r[acol]) * (-1 if flip else 1)
        else:
            amt = abs(_num(r.get(credit) or "")) - abs(_num(r.get(debit) or ""))
        key = (date, name, round(amt, 2))
        seen[key] = seen.get(key, 0) + 1            # two identical purchases on one day are both real: number them
        tid = hashlib.sha256(f"{date}|{name}|{amt:.2f}|{seen[key]}".encode()).hexdigest()[:20]
        out.append({"id": f"bank:{tid}", "date": date, "name": name or "(no description)", "amount": round(amt, 2)})
        if bcol and (r.get(bcol) or "").strip() and (newest is None or date >= newest[0]):
            newest = (date, _num(r[bcol]))
    return out, (newest[1] if newest else None)


def store_bank(db, account_id: str, rows: list[dict], balance: float | None = None) -> int:
    from goblinbank import ledger
    from goblinbank.sync import money, now, record_balance
    acct = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    added = 0
    for r in rows:
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)",
                            (r["id"], account_id, r["date"], r["name"], r["amount"], "csv", ledger.categorize(db, r["name"], r["amount"], None, acct["kind"])))
            if cur.rowcount:
                added += 1
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)", (f"tx:{r['id']}", f"{r['date']}T00:00:00+00:00", "transaction", f"{acct['name']}: {r['name']} {money(r['amount'])}", r["amount"], account_id, "imported"))
    if balance is not None:
        with db.tx() as c:
            c.execute("UPDATE accounts SET balance=?, updated=? WHERE id=?", (balance, now(), account_id))
        record_balance(db, account_id, acct["name"], balance, acct["kind"])
    return added
