"""Seeded random data with planted patterns, so tests can compare code output against a known truth."""
import random
from datetime import date, timedelta

from goblinbank import ledger, recur
from goblinbank.db import DB

MERCHANTS = ["Kroger", "Shell Oil", "Chipotle", "Target", "Amazon", "Starbucks", "Costco", "Home Depot", "Uber", "Walgreens", "Best Buy", "Panera",
             "Lowes", "Wendys", "Steam Games", "Old Navy", "CVS", "Sheetz", "Dominos", "Trader Joes", "Marathon Gas", "Zara", "Aldi", "Five Guys"]


def seed(db: DB, seed_no: int, today: date, months: int = 14) -> dict:
    rng = random.Random(seed_no)
    truth = {"planted": {}, "tx_ids": []}
    accts = [("chk", "Huntington", "Checking", "cash", round(rng.uniform(300, 6000), 2)), ("sav", "Axos", "Savings", "cash", round(rng.uniform(1000, 30000), 2)),
             ("crd", "Capital One", "Card", "credit", round(rng.uniform(0, 3000), 2)), ("brk", "Robinhood", "Brokerage", "investment", round(rng.uniform(500, 40000), 2)),
             ("own-cash", "Manual", "Cash jar", "cash", round(rng.uniform(0, 400), 2))]
    with db.tx() as c:
        for i, inst, n, kind, bal in accts:
            c.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)", (i, "manual" if i.startswith("own") else "plaid", inst, n, kind, bal, today.isoformat()))
    start = today - timedelta(days=30 * months)
    n = [0]

    def add(acct, dt, name, amt):
        n[0] += 1
        tid = f"s{seed_no}-{n[0]}"
        cat = ledger.categorize(db, name, amt)
        with db.tx() as c:
            c.execute("INSERT INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)", (tid, acct, dt.isoformat(), name, amt, "seed", cat))
        truth["tx_ids"].append(tid)

    # planted steady patterns (jitter of a day or two like real bank posting dates)
    k = 0
    while (d := recur.shift(start + timedelta(days=5), 1, "month", k)) <= today:
        add("chk", d + timedelta(days=rng.choice([0, 0, 1])), "NETFLIX.COM", -15.49); k += 1
    truth["planted"]["netflix"] = {"amount": 15.49, "every": 1, "unit": "month"}
    d = start + timedelta(days=2)
    while d <= today:
        add("chk", d, "ACME CORP PAYROLL", 2150.00 + rng.choice([0, 0, 0.5])); d += timedelta(days=14)
    truth["planted"]["payroll"] = {"amount": 2150.0, "every": 2, "unit": "week", "direction": "in"}
    d = start + timedelta(days=1)
    while d <= today:
        add("crd", d, "Weekly Farm Box", -32.0); d += timedelta(days=7)
    truth["planted"]["farm"] = {"amount": 32.0, "every": 1, "unit": "week"}
    for yr in range(2):
        d = today - timedelta(days=330 + 365 * yr)
        if d >= start - timedelta(days=400):
            add("chk", d, "STATE FARM INSURANCE", -1180.0)
    truth["planted"]["insurance"] = {"amount": 1180.0, "every": 1, "unit": "year"}
    # a bill that STOPPED arriving 70 days ago (for late-payment flags)
    k = 0
    while (d := recur.shift(start + timedelta(days=9), 1, "month", k)) <= today - timedelta(days=70):
        add("chk", d, "CITY WATER UTILITY", -61.25); k += 1
    truth["water_last_seen"] = d - timedelta(days=0)
    # noise
    for _ in range(rng.randint(180, 320)):
        dt = start + timedelta(days=rng.randint(0, (today - start).days))
        m = rng.choice(MERCHANTS)
        add(rng.choice(["chk", "crd", "own-cash"]), dt, f"{m} #{rng.randint(100, 9999)}", -round(rng.uniform(2, 180), 2))
    for _ in range(rng.randint(3, 8)):
        add("chk", start + timedelta(days=rng.randint(0, (today - start).days)), f"Refund {rng.choice(MERCHANTS)}", round(rng.uniform(5, 90), 2))
    return truth
