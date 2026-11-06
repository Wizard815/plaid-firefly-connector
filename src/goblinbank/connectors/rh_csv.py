"""rh-main-csv: parse Robinhood activity-report CSVs from a watched folder.

Zero account access: it only reads files you drop into CSV_DIR.
Expected columns (Robinhood activity report): Activity Date, Instrument,
Description, Trans Code, Quantity, Price, Amount. Rows are deduped by content hash,
so re-importing overlapping exports never double counts.
"""
import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path


def _money(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    v = float(re.sub(r"[^0-9.\-]", "", s) or 0)
    return -abs(v) if neg else v


def _qty(s: str) -> float:
    s = (s or "").strip().rstrip("S")  # "10S" style suffixes
    try:
        return float(s.replace(",", "")) if s else 0.0
    except ValueError:
        return 0.0


class CsvConnector:
    name = "rh-main-csv"

    def __init__(self, csv_dir: str):
        self.dir = Path(csv_dir)

    def _rows(self) -> list[dict]:
        seen, out = set(), []
        for f in sorted(self.dir.glob("*.csv")):
            with f.open(newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    key = hashlib.sha256("|".join(f"{k}={v}" for k, v in sorted(r.items())).encode()).hexdigest()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "id": key[:16],
                        "date": r.get("Activity Date", ""),
                        "symbol": (r.get("Instrument") or "").strip(),
                        "code": (r.get("Trans Code") or "").strip(),
                        "description": (r.get("Description") or "").strip(),
                        "quantity": _qty(r.get("Quantity", "")),
                        "price": _money(r.get("Price", "")),
                        "amount": _money(r.get("Amount", "")),
                    })
        return out

    def history(self) -> list[dict]:
        return self._rows()

    def positions(self) -> list[dict]:
        qty, cost = defaultdict(float), defaultdict(float)
        for r in self._rows():
            if not r["symbol"]:
                continue
            if r["code"] == "Buy":
                qty[r["symbol"]] += r["quantity"]
                cost[r["symbol"]] += -r["amount"]
            elif r["code"] == "Sell":
                qty[r["symbol"]] -= r["quantity"]
                cost[r["symbol"]] -= r["amount"]
        return [
            {"symbol": s, "quantity": round(q, 6), "net_cost": round(cost[s], 2)}
            for s, q in sorted(qty.items()) if abs(q) > 1e-9
        ]

    def balances(self) -> dict:
        # Activity reports contain no cash balance; report net deposits instead.
        dep = sum(r["amount"] for r in self._rows() if r["code"] in ("ACH", "RTP", "XENT_CC"))
        return {"net_transfers": round(dep, 2), "note": "cash balance not in activity CSV"}
