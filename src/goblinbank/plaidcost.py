"""What Plaid charges on the pay-as-you-go plan (the list price you pasted), and an estimate of this goblin's month from what it really uses.

Billed per linked item per month: Transactions $0.30, Recurring Transactions $0.15 (only if used), Investments Holdings $0.18 (only for items with an investment account).
Billed per call: Transactions Refresh $0.12 (successful), Balance $0.10. Auth, Identity, Income, Assets and the rest are not used by this app.
"""
PRICE = {"transactions": 0.30, "recurring": 0.15, "investments": 0.18, "refresh_call": 0.12, "balance_call": 0.10}
PER_CALL = {"/transactions/refresh": 0.12, "/accounts/balance/get": 0.10, "/identity/get": 1.50, "/auth/get": 1.50, "/liabilities/get": 0.20}     # billed per use, so they are the ones a budget must stop
LIST_AS_OF = "pay-as-you-go list price you provided"


def estimate(db, use_recurring: bool, by_endpoint: dict) -> dict:
    items = db.all("SELECT item_id, institution FROM plaid_items")
    lines = []
    n = len(items)
    if n:
        lines.append({"label": "Transactions", "qty": n, "unit": "item / month", "rate": PRICE["transactions"], "cost": round(n * PRICE["transactions"], 2)})
    if n and use_recurring:
        lines.append({"label": "Recurring transactions", "qty": n, "unit": "item / month", "rate": PRICE["recurring"], "cost": round(n * PRICE["recurring"], 2),
                      "optional": "Turn off to rely on this app's own detection of repeating payments."})
    inv = [i for i in items if db.one("SELECT 1 FROM accounts WHERE item_id=? AND kind='investment'", (i["item_id"],))]
    if inv:
        lines.append({"label": "Investments holdings", "qty": len(inv), "unit": "item / month", "rate": PRICE["investments"], "cost": round(len(inv) * PRICE["investments"], 2)})
    for ep, key, label in (("/transactions/refresh", "refresh_call", "Transactions refresh"), ("/accounts/balance/get", "balance_call", "Balance")):
        calls = by_endpoint.get(ep, 0)
        if calls:
            lines.append({"label": label, "qty": calls, "unit": "call", "rate": PRICE[key], "cost": round(calls * PRICE[key], 2)})
    return {"lines": lines, "total": round(sum(x["cost"] for x in lines), 2), "per_item": round(PRICE["transactions"] + (PRICE["recurring"] if use_recurring else 0), 2)}
