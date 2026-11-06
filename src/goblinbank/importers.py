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
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)",
                  (acct, "csv", provider.title(), f"{provider.title()} (CSV import)", "cash", None, now()))
    added = 0
    for r in rows:
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount,source) VALUES(?,?,?,?,?,?)",
                            (r["id"], acct, r["date"], r["name"], r["amount"], "csv"))
            if cur.rowcount:
                added += 1
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                          (f"tx:{r['id']}", f"{r['date']}T00:00:00+00:00", "transaction",
                           f"{provider.title()}: {r['name']} {money(r['amount'])}", r["amount"], acct, "imported"))
    return added
