"""Plaid Firefly Connector as a Plaid router for Firefly III: bank accounts and transactions that Plaid delivers are pushed into your Firefly III through its API.

One way only (Plaid -> Plaid Firefly Connector -> Firefly III), so Firefly III's own data is never read back except to find an account or a duplicate, and nothing in a Firefly
III installation needs changing. What it needs: your Firefly III address and a Personal Access Token (Firefly III: Options, Profile, OAuth, Personal Access Tokens).
Idempotent: every Plaid transaction id is remembered together with the Firefly III transaction it became, so running it twice never duplicates anything.
"""
import hashlib
import json
import math
import re
import time
from datetime import date, timedelta

import httpx

from plaidproxy import crypto, settings as cfg

MARK = "goblin-bank"          # the tag and internal reference on every transaction already in Firefly III: kept so nothing old looks new
RUN_LIMIT = 300                      # transactions created or changed per run; the rest carries on at the next run
MAX_ATTEMPTS = 5


class FireflyError(Exception):
    def __init__(self, message: str, status: int = 0, body: dict | None = None):
        super().__init__(message)
        self.status, self.body = status, body or {}


class Firefly:
    """A thin client for the parts of the Firefly III API this needs."""

    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.base = url.rstrip("/") + "/api/v1"
        self.http = httpx.Client(timeout=30, transport=transport, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.api+json", "Content-Type": "application/json"})

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        try:
            r = self.http.request(method, self.base + path, json=body, params=params)
        except httpx.HTTPError as e:
            raise FireflyError(f"could not reach Firefly III ({type(e).__name__})")
        if r.status_code in (401, 403):
            raise FireflyError("Firefly III refused the token: make a new Personal Access Token (they last at most a year)", r.status_code)
        if r.status_code >= 400:
            try:
                j = r.json()
            except ValueError:
                j = {}
            raise FireflyError(str(j.get("message") or f"Firefly III answered {r.status_code}")[:300], r.status_code, j)
        return r.json() if r.content else {}

    def about(self) -> dict:
        return self.call("GET", "/about").get("data", {})

    def accounts(self, kind: str = "asset") -> list[dict]:
        out, page = [], 1
        while True:
            j = self.call("GET", "/accounts", params={"type": kind, "page": page, "limit": 50})
            out += [{"id": int(a["id"]), **a["attributes"]} for a in j.get("data", [])]
            if page >= int((j.get("meta", {}).get("pagination", {}) or {}).get("total_pages", 1)):
                return out
            page += 1

    def account(self, ff_id: int) -> dict:
        a = self.call("GET", f"/accounts/{ff_id}")["data"]
        return {"id": int(a["id"]), **a["attributes"]}

    def account_transactions(self, ff_id: int):
        """Every transaction in one Firefly III account, a page at a time: (group id, external id, the split itself)."""
        page = 1
        while True:
            j = self.call("GET", f"/accounts/{ff_id}/transactions", params={"page": page, "limit": 50, "type": "all"})
            for g in j.get("data", []):
                for t in g["attributes"].get("transactions", []):
                    yield int(g["id"]), t.get("external_id"), t
            if page >= int((j.get("meta", {}).get("pagination", {}) or {}).get("total_pages", 1)):
                return
            page += 1

    def set_opening(self, ff_id: int, amount: float, on: str, direction: str | None = None) -> None:
        """A liability's opening is only accepted with its direction (Firefly III 6.7.9 fails with "Undefined array key liability_direction" without it)."""
        self.call("PUT", f"/accounts/{ff_id}", {"opening_balance": f"{amount:.2f}", "opening_balance_date": on, **({"liability_direction": direction} if direction else {})})

    def update_account(self, ff_id: int, fields: dict) -> None:
        if not fields:
            raise FireflyError("nothing to change")
        self.call("PUT", f"/accounts/{ff_id}", fields)

    def create_account(self, payload: dict) -> int:
        return int(self.call("POST", "/accounts", payload)["data"]["id"])

    def create_tx(self, payload: dict) -> int:
        return int(self.call("POST", "/transactions", payload)["data"]["id"])

    def update_tx(self, ff_id: int, payload: dict) -> None:
        self.call("PUT", f"/transactions/{ff_id}", payload)

    def delete_tx(self, ff_id: int) -> None:
        try:
            self.call("DELETE", f"/transactions/{ff_id}")
        except FireflyError as e:
            if e.status != 404:
                raise

    def find_by_external_id(self, external_id: str) -> int | None:
        j = self.call("GET", "/search/transactions", params={"query": f'external_id_is:"{external_id}"'})
        data = j.get("data", [])
        return int(data[0]["id"]) if data else None


# ---------------------------------------------------------------- settings
def conf(db) -> dict:
    url, token = cfg.get(db, "firefly_url").strip(), cfg.get(db, "firefly_token")
    return {"url": url, "token": token, "enabled": bool(url and token) and cfg.get(db, "firefly_enabled") != "off",
            "send_categories": cfg.get(db, "firefly_send_categories") == "on", "include_pending": cfg.get(db, "firefly_include_pending") == "on", "apply_rules": cfg.get(db, "firefly_apply_rules") != "off"}


def _state_set(db, key: str, value: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO ff_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _state(db) -> dict:
    return {r["key"]: r["value"] for r in db.all("SELECT key, value FROM ff_state")}


# ---------------------------------------------------------------- mapping accounts
# A new bank account is NOT sent anywhere until the admin approves it: pending -> create / link / skip. (A person who links a bank cannot approve their own accounts.)
DEFAULT_MODE = "pending"


def ensure_mappings(db) -> None:
    """Every account Plaid Firefly Connector knows gets a row saying what to do with it. New ones wait for approval: nothing is sent until the admin chooses create, link or skip."""
    for a in db.all("SELECT id FROM accounts"):
        if not db.one("SELECT 1 FROM ff_accounts WHERE account_id=?", (a["id"],)):
            with db.tx() as c:
                c.execute("INSERT INTO ff_accounts(account_id, mode) VALUES(?,?)", (a["id"], DEFAULT_MODE))


class LooksDuplicate(ValueError):
    """Approving an account that looks like another one already known (same bank, last digits, name and kind)."""


def set_mapping(db, account_id: str, mode: str, ff_id: int | None = None, confirm_duplicate: bool = False) -> None:
    """The admin's decision for one bank account. create: make it in Firefly III (once). link: use this Firefly III account that already exists (one Firefly III account
    receives one bank account, never several). skip: send nothing. pending: back to waiting for a decision."""
    if mode not in ("create", "link", "skip", "pending"):
        raise ValueError("mode must be create, link, skip or pending")
    if mode == "link" and not ff_id:
        raise ValueError("choose the Firefly III account to link to")
    if not db.one("SELECT 1 FROM accounts WHERE id=?", (account_id,)):
        raise KeyError(account_id)
    ensure_mappings(db)
    cur = db.one("SELECT * FROM ff_accounts WHERE account_id=?", (account_id,))
    twins = duplicates(db).get(account_id)
    if twins and mode in ("create", "link") and not confirm_duplicate:
        names = ", ".join(account_label(db.one("SELECT * FROM accounts WHERE id=?", (t,))) for t in twins)
        raise LooksDuplicate(f"this looks like the same bank account as {names} (same bank, last digits and name). Approving both would send every transaction twice: skip one, or confirm it really is a different account")
    if mode == "link":
        other = db.one("SELECT a.institution, a.name FROM ff_accounts m JOIN accounts a ON a.id=m.account_id WHERE m.ff_id=? AND m.account_id!=? AND m.mode IN ('create','link')", (int(ff_id), account_id))
        if other:
            raise ValueError(f"that Firefly III account already receives {other['institution']} - {other['name']}; one Firefly III account takes one bank account")
    with db.tx() as c:
        if mode == "pending":
            c.execute("UPDATE ff_accounts SET mode='pending' WHERE account_id=?", (account_id,))
        elif mode == "link":
            c.execute("UPDATE ff_accounts SET mode='link', ff_id=?, created=0, adopted=0, ff_type=NULL WHERE account_id=?", (int(ff_id), account_id))
        elif mode == "create":
            if cur.get("created") and cur.get("ff_id"):                  # already created by us: keep using it
                c.execute("UPDATE ff_accounts SET mode='create' WHERE account_id=?", (account_id,))
            else:
                c.execute("UPDATE ff_accounts SET mode='create', ff_id=NULL, created=0, ff_type=NULL WHERE account_id=?", (account_id,))
        else:
            c.execute("UPDATE ff_accounts SET mode='skip' WHERE account_id=?", (account_id,))


def duplicates(db) -> dict[str, list[str]]:
    """Bank accounts that look like the same real account twice: same bank, same last digits, same name and kind (Plaid has sent one real account under
    two ids after a bank's re-sign-in, and a bank linked twice does the same). account id -> the other ids that look the same."""
    groups: dict = {}
    for a in db.all("SELECT id, institution, name, nickname, mask, kind FROM accounts WHERE source='plaid'"):
        if not a.get("mask"):
            continue                                              # without last digits there is too little to go on
        groups.setdefault((str(a["institution"] or "").lower(), a["mask"], _norm(a.get("nickname") or a["name"]), a["kind"]), []).append(a["id"])
    return {i: [j for j in ids if j != i] for ids in groups.values() if len(ids) > 1 for i in ids}


def account_label(a: dict) -> str:
    nick = a.get("nickname") or a["name"]
    return f"{a['institution']} - {nick}" + (f" ••{a['mask']}" if a.get("mask") and not a.get("nickname") and a["mask"] not in nick else "")


def _role(a: dict) -> dict:
    sub = (a.get("subtype") or "").lower()
    cat = (a.get("category") or "").lower()
    if a["kind"] == "credit":
        return {"account_role": "ccAsset", "credit_card_type": "monthlyFull", "monthly_payment_date": date.today().replace(day=1).isoformat()}
    if cat in ("savings", "emergency fund") or sub in ("savings", "cd", "money market", "cash management"):
        return {"account_role": "savingAsset"}
    return {"account_role": "defaultAsset"}


def is_liability(a: dict) -> bool:
    """A bank loan (auto, student, mortgage, personal) goes to Firefly III as a liability you owe, not as an asset account."""
    return a.get("kind") == "loan"


def ff_type_of(x: dict) -> str:
    """A Firefly III account's attributes -> 'liability' or 'asset'."""
    return "liability" if str(x.get("type") or "").startswith("liabilit") else "asset"


def _signed(a: dict) -> float:
    """What the account's balance is, as an asset: what is owed on a card or a loan is a negative balance."""
    return -(a["balance"] or 0.0) if a["kind"] in ("credit", "loan") else (a["balance"] or 0.0)


def opening_for(db, a: dict, include_pending: bool) -> tuple[float, str]:
    """Opening balance so that Firefly III's balance equals the bank's current one once the pushed transactions are in."""
    rows = db.all("SELECT date, amount FROM transactions WHERE account_id=?" + ("" if include_pending else " AND pending=0"), (a["id"],))
    net = sum(round(r["amount"], 2) for r in rows)                  # each one as Firefly III gets it: whole cents
    first = min((r["date"] for r in rows), default=date.today().isoformat())
    return round(_signed(a) - net, 2), (date.fromisoformat(first) - timedelta(days=1)).isoformat()


def ensure_account(db, ff: Firefly, a: dict, m: dict, c: dict) -> int:
    if m.get("ff_id"):
        return m["ff_id"]
    ff_id = create_for(db, ff, a, c, account_label(a))
    with db.tx() as c2:
        c2.execute("UPDATE ff_accounts SET ff_id=?, created=1, adopted=1, ff_type=? WHERE account_id=?", (ff_id, "liability" if is_liability(a) else "asset", a["id"]))
    return ff_id


def create_for(db, ff: Firefly, a: dict, c: dict, name: str) -> int:
    """Make the Firefly III account for one bank account: an asset account, or for a loan a liability you owe ("debit": "I owe this debt to
    somebody else"). Its opening balance is set so Firefly III's balance equals the bank's once the transactions are in; a debt's balance is
    negative in Firefly III, as here."""
    opening, odate = opening_for(db, a, c["include_pending"])
    common = {"name": name, "currency_code": a.get("currency") or "USD", "opening_balance": f"{opening:.2f}", "opening_balance_date": odate,
              "notes": f"Created by Plaid Firefly Connector from a linked bank account ({a['institution']}).", "include_net_worth": True}
    if is_liability(a):
        if opening > 0:          # the history says the debt was below zero before it starts (an overpaid loan, or figures that do not add up):
            common["opening_balance"] = "0.00"           # a debt you owe cannot start there, and Firefly III would flip the sign. 0, and the balance shows as different
        payload = {**common, "type": "liability", "liability_type": "mortgage" if "mortgage" in (a.get("subtype") or "").lower() else "loan",
                   "liability_direction": "debit", "interest": "0", "interest_period": "monthly"}
    else:
        payload = {**common, "type": "asset", **_role(a)}
    try:
        return ff.create_account(payload)
    except FireflyError as e:
        if e.status == 422 and "name" in json.dumps(e.body).lower():          # the name is taken: add the account's own id to make it unique
            payload["name"] = f"{name} [{a['id'][-4:]}]"
            return ff.create_account(payload)
        raise


def _norm(s) -> str:
    """A description for matching: lower case, letters and digits only ("SQ *Coffee-Shop #12" and "sq coffee shop 12" match)."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _ff_split(ff_id: int, t: dict) -> tuple[str, float, str] | None:
    """One Firefly III split as seen from account ff_id: (day, signed amount, description). Money out of the account is negative, as in the transactions table."""
    try:
        amt = abs(float(t.get("amount") or 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amt):
        return None
    if t.get("source_id") is not None or t.get("destination_id") is not None:        # which side this account is on: a payment into a loan is a withdrawal elsewhere, money in here
        out = str(t.get("source_id")) == str(ff_id)
    else:
        out = t.get("type") == "withdrawal"
    return str(t.get("date") or "")[:10], -amt if out else amt, str(t.get("description") or "")


def adopt_existing(db, ff: Firefly, account_id: str, ff_id: int) -> int:
    """An account that already holds transactions in Firefly III (linked to an existing one, restored from a backup, or the same bank account again after
    a bank was removed and linked again): take over what is already there so nothing is sent twice. Done once per account.

    First by Plaid id (external_id). After a remove-and-relink Plaid sends the whole history again under NEW transaction ids, so the ids never match;
    then a transaction is matched by what it is: same Firefly III account, same day, same amount and direction, same description (letters and digits
    only), and failing that the same amount and description within 3 days (a pending charge that posted a day later). Each Firefly III transaction
    is taken once and each local one once, so two identical coffees on one day match two, not one."""
    send_cat = conf(db)["send_categories"]
    mine = {r["id"]: r for r in db.all("SELECT * FROM transactions WHERE account_id=?", (account_id,))}
    taken_tx = {r["tx_id"] for r in db.all("SELECT tx_id FROM ff_tx")}
    # Firefly III transactions already claimed by a transaction that is still here (a bank removed here leaves its old claims behind; those don't count)
    taken_ff = {r["ff_id"] for r in db.all("SELECT f.ff_id FROM ff_tx f JOIN transactions t ON t.id=f.tx_id WHERE f.ff_id IS NOT NULL")}
    n, seen, theirs = 0, 0, []

    def take(tx_id: str, gid: int):
        with db.tx() as c:              # taken over as it is (not rewritten): it only changes later if the bank changes it
            c.execute("INSERT INTO ff_tx(tx_id,ff_id,state,sig,pushed) VALUES(?,?, 'ok', ?, ?) ON CONFLICT(tx_id) DO NOTHING", (tx_id, gid, _sig(mine[tx_id], send_cat), date.today().isoformat()))
        taken_tx.add(tx_id)
        taken_ff.add(gid)

    for gid, ext, t in ff.account_transactions(ff_id):
        seen += 1
        if ext in mine and ext not in taken_tx:
            take(ext, gid)
            n += 1
        elif gid not in taken_ff and not (ext and ext in mine):
            theirs.append((gid, t))
    free = [r for r in mine.values() if r["id"] not in taken_tx]
    for near in (0, 3):                                  # exact day first, then a few days either side
        by: dict = {}
        for r in free:
            by.setdefault((round(r["amount"], 2), _norm(r["name"])), []).append(r)
        for gid, t in theirs:
            if gid in taken_ff:
                continue
            s = _ff_split(ff_id, t)
            if not s or not s[0]:
                continue
            cands = [r for r in by.get((round(s[1], 2), _norm(s[2])), []) if r["id"] not in taken_tx]
            try:
                d = date.fromisoformat(s[0])
                hit = next((r for r in sorted(cands, key=lambda r: abs((date.fromisoformat(r["date"]) - d).days)) if abs((date.fromisoformat(r["date"]) - d).days) <= near), None)
            except ValueError:
                hit = None
            if hit:
                take(hit["id"], gid)
                n += 1
        free = [r for r in free if r["id"] not in taken_tx]
    with db.tx() as c:
        c.execute("UPDATE ff_accounts SET adopted=1, ff_seen=? WHERE account_id=?", (seen, account_id))
    return n


def rebalance(db, ff: Firefly, account_id: str) -> dict:
    """Make Firefly III's balance equal the bank's by moving the account's opening balance (never by inventing a transaction). A button, not automatic: if you also record things by hand in that account, you decide."""
    a = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    m = db.one("SELECT * FROM ff_accounts WHERE account_id=?", (account_id,))
    if not a or not m or not m.get("ff_id"):
        raise KeyError(account_id)
    cur = ff.account(m["ff_id"])
    have = float(cur.get("current_balance") or 0)
    diff = round(_signed(a) - have, 2)
    if abs(diff) < 0.005:
        return {"changed": False, "difference": 0.0}
    opening = float(cur.get("opening_balance") or 0) + diff
    on = (cur.get("opening_balance_date") or date.today().isoformat())[:10]
    liab = ff_type_of(cur) == "liability"
    if liab and (cur.get("liability_direction") or "debit") != "debit":
        raise FireflyError("that Firefly III liability is set as \"I am owed this debt\"; a bank loan is a debt you owe (\"I owe this debt\"): change it in Firefly III first")
    if liab and opening > 0:
        raise FireflyError("the bank says less is owed than the transactions in Firefly III add up to; check that loan's transactions in Firefly III")
    ff.set_opening(m["ff_id"], opening, on, (cur.get("liability_direction") or "debit") if liab else None)
    return {"changed": True, "difference": diff}


def to_liability(db, ff: Firefly, account_id: str) -> dict:
    """A bank loan that was sent to an ASSET account (before loans went as liabilities): make the Firefly III liability and move it there.

    1. the old account is renamed "... (old asset)" so the new one can have its name, and the liability is made (opening from the bank's balance);
    2. every transaction THIS connector put in the old account (its tag) is deleted, both halves of a transfer included, and forgotten here, so
       the next push sends them again: to the liability, and a payment from checking as a withdrawal into it;
    3. the old account is switched off (not deleted): anything you entered there yourself stays where it is.
    Safe to run again if it stops half way: it carries on from where it was."""
    a = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    m = db.one("SELECT * FROM ff_accounts WHERE account_id=?", (account_id,))
    if not a or not m or not m.get("ff_id") or m["mode"] not in ("create", "link"):
        raise KeyError(account_id)
    if not is_liability(a):
        raise ValueError("only a bank loan becomes a liability")
    c = conf(db)
    st = _state(db)
    old = int(st.get(f"old_ff:{account_id}") or 0)
    cur = ff.account(m["ff_id"])
    if ff_type_of(cur) == "liability" and not old:
        raise ValueError("it already goes to a Firefly III liability")
    out = {"deleted": 0, "kept": 0}
    if not old:                                                  # step 1, once
        old = m["ff_id"]
        _state_set(db, f"old_ff:{account_id}", str(old))
        name = cur.get("name") or account_label(a)
        ff.update_account(old, {"name": (name + " (old asset)")[:255]})
        new = create_for(db, ff, a, c, account_label(a))
        with db.tx() as c2:
            c2.execute("UPDATE ff_accounts SET ff_id=?, ff_type='liability', created=1, adopted=1, ff_seen=0, held=0, release=0 WHERE account_id=?", (new, account_id))
        out["liability"] = new
    gone = set()
    for gid, _ext, t in list(ff.account_transactions(old)):      # step 2: only what this connector made (its tag); what you entered stays
        if gid in gone or t.get("type") == "opening balance":       # Firefly III's own opening entry goes with the account, it is nobody's
            continue
        if t.get("internal_reference") == MARK or MARK in (t.get("tags") or []):
            ff.delete_tx(gid)
            gone.add(gid)
            with db.tx() as c2:
                c2.execute("DELETE FROM ff_tx WHERE ff_id=?", (gid,))
            out["deleted"] += 1
        else:
            out["kept"] += 1
    with db.tx() as c2:                                           # this loan's own rows are sent again to the liability
        c2.execute("DELETE FROM ff_tx WHERE tx_id IN (SELECT id FROM transactions WHERE account_id=?)", (account_id,))
    ff.update_account(old, {"active": False})                     # step 3
    with db.tx() as c2:
        c2.execute("DELETE FROM ff_state WHERE key=?", (f"old_ff:{account_id}",))
    out["old_account"] = old
    return out


# ---------------------------------------------------------------- transactions
def _sig(t: dict, send_cat: bool = False) -> str:
    # The category is never part of it: it is sent once, with a new transaction, and from then on it is Firefly III's (yours, its rules,
    # Coinpile's). So switching "send Plaid's categories" on or off never rewrites what is already in Firefly III.
    return hashlib.sha256(json.dumps([t["date"], round(t["amount"], 2), t["name"], "", t["account_id"], t["pending"]]).encode()).hexdigest()[:16]


def category_to_send(t: dict) -> str | None:
    """Plaid's category for a new transaction, only when it says something: the bare fallbacks (Misc, or Income on money Plaid did not call
    income) are left blank, so Firefly III's rules and Coinpile still see the transaction as uncategorized."""
    cat = (t.get("category") or "").strip()
    if not cat or cat == "Misc":
        return None
    if cat == "Income" and not str(t.get("plaid_category") or "").startswith("INCOME"):
        return None
    return cat[:255]


def payload_for(t: dict, ff_account: int, c: dict, currency: str) -> dict:
    """One Plaid transaction as a Firefly III withdrawal or deposit. Amounts are positive there; the direction is the type."""
    out = t["amount"] < 0
    leg = {"type": "withdrawal" if out else "deposit", "date": t["date"], "amount": f"{abs(t['amount']):.2f}", "description": (t["name"] or "(no description)")[:1000], "currency_code": currency,
           "external_id": t["id"], "internal_reference": MARK, "tags": [MARK]}
    if out:
        leg.update({"source_id": ff_account, "destination_name": (t["name"] or "Unknown")[:255]})
    else:
        leg.update({"source_name": (t["name"] or "Unknown")[:255], "destination_id": ff_account})
    if c["send_categories"] and category_to_send(t):
        leg["category_name"] = category_to_send(t)
    return {"error_if_duplicate_hash": True, "apply_rules": c["apply_rules"], "fire_webhooks": True, "group_title": None, "transactions": [leg]}


def _pairs(rows: list[dict]) -> dict[str, str]:
    """Transfers between two of your own mapped accounts show up as an outflow on one and an inflow on the other. Pair them so Firefly III gets ONE transfer."""
    outs = sorted((r for r in rows if r["amount"] < 0 and r["category"] == "Transfer"), key=lambda r: r["date"])
    ins = [r for r in rows if r["amount"] > 0 and r["category"] == "Transfer"]
    used, pairs = set(), {}
    for o in outs:
        for i in ins:
            if i["id"] in used or i["account_id"] == o["account_id"] or abs(i["amount"] + o["amount"]) > 0.005:
                continue
            if abs((date.fromisoformat(i["date"]) - date.fromisoformat(o["date"])).days) <= 4:
                used.add(i["id"])
                pairs[o["id"]] = i["id"]
                break
    return pairs


# Data safety, like the Plaid budget cap is for cost: an account that already has history in Firefly III is not sent a batch big enough to
# roughly double it (what a relink that slipped past matching looks like). The account is held until the admin says send anyway.
HOLD_MIN_EXISTING, HOLD_MIN_NEW, HOLD_SHARE = 10, 20, 0.5


def held(db) -> list[dict]:
    """Accounts whose new transactions are held back, with how many would go and how many are already there."""
    return [{"account_id": r["account_id"], "label": account_label(r), "waiting": r["held"], "existing": r["ff_seen"]}
            for r in db.all("SELECT m.account_id, m.held, m.ff_seen, a.institution, a.name, a.nickname, a.mask FROM ff_accounts m JOIN accounts a ON a.id=m.account_id WHERE m.held > 0")]


def release(db, account_id: str) -> None:
    """The admin looked and wants the held batch sent: the next push sends it (once)."""
    with db.tx() as c:
        if not c.execute("UPDATE ff_accounts SET release=1 WHERE account_id=? AND held > 0", (account_id,)).rowcount:
            raise KeyError(account_id)


def push(db, ff: Firefly, limit: int | None = None) -> dict:
    """Send what is new, changed or removed to Firefly III. Safe to run any time and as often as you like."""
    limit = limit or RUN_LIMIT
    c = conf(db)
    res = {"accounts_created": 0, "created": 0, "updated": 0, "deleted": 0, "errors": 0, "remaining": 0, "error_samples": [], "held": []}
    ensure_mappings(db)
    maps = {r["account_id"]: r for r in db.all("SELECT * FROM ff_accounts WHERE mode IN ('create','link')")}
    accts = {a["id"]: a for a in db.all("SELECT * FROM accounts")}
    done = 0

    def fail(tx_id: str, e: Exception):
        res["errors"] += 1
        msg = str(e)[:200]
        if len(res["error_samples"]) < 3:
            res["error_samples"].append(msg)
        with db.tx() as c2:
            c2.execute("INSERT INTO ff_tx(tx_id,state,error,attempts) VALUES(?, 'error', ?, 1) ON CONFLICT(tx_id) DO UPDATE SET state='error', error=excluded.error, attempts=ff_tx.attempts+1", (tx_id, msg))

    # 1. removed at the bank: remove from Firefly III
    for r in db.all("SELECT tx_id, ff_id FROM ff_tx WHERE state='deleted'"):
        try:
            if r["ff_id"]:
                ff.delete_tx(r["ff_id"])
            with db.tx() as c2:
                c2.execute("DELETE FROM ff_tx WHERE tx_id=?", (r["tx_id"],))
            res["deleted"] += 1
        except FireflyError as e:
            if e.status in (401, 403):
                raise                                              # a refused token: stop everything and say so
            fail(r["tx_id"], e)
    # 2. accounts that need creating
    created_now = set()
    for aid, m in maps.items():
        if aid in accts and not m.get("ff_id") and m["mode"] == "create":
            try:
                ensure_account(db, ff, accts[aid], m, c)
                res["accounts_created"] += 1
                created_now.add(aid)
            except FireflyError as e:
                if e.status in (401, 403):
                    raise
                res["errors"] += 1
                res["error_samples"].append(f"account {account_label(accts[aid])}: {str(e)[:150]}")
    maps = {r["account_id"]: r for r in db.all("SELECT * FROM ff_accounts WHERE mode IN ('create','link') AND ff_id IS NOT NULL")}
    for aid, m in maps.items():                                  # an account linked to one already in Firefly III: is it an asset or a liability (looked up once)
        if not m.get("ff_type"):
            try:
                m["ff_type"] = ff_type_of(ff.account(m["ff_id"]))
            except FireflyError as e:
                if e.status in (401, 403):
                    raise
                continue
            with db.tx() as c2:
                c2.execute("UPDATE ff_accounts SET ff_type=? WHERE account_id=?", (m["ff_type"], aid))
    for aid, m in maps.items():
        known_rows = db.one("SELECT COUNT(*) n FROM ff_tx f JOIN transactions t ON t.id=f.tx_id WHERE t.account_id=?", (aid,))["n"]
        has_tx = db.one("SELECT COUNT(*) n FROM transactions WHERE account_id=?", (aid,))["n"]
        if aid not in created_now and (not m.get("adopted") or (known_rows == 0 and has_tx)):          # first time here, or the memory of what was sent is gone: look at what is already there
            try:
                adopt_existing(db, ff, aid, m["ff_id"])
            except FireflyError as e:
                if e.status in (401, 403):
                    raise
                res["errors"] += 1
                res["error_samples"].append(f"looking through an existing account: {str(e)[:150]}")
    if not maps:
        return res
    ph = ",".join("?" * len(maps))
    rows = db.all(f"SELECT * FROM transactions WHERE account_id IN ({ph}) ORDER BY date, id", tuple(maps))
    if not c["include_pending"]:
        rows = [r for r in rows if not r["pending"]]
    known = {r["tx_id"]: r for r in db.all("SELECT * FROM ff_tx")}
    for o_id, i_id in _pairs(rows).items():                      # a transfer already in Firefly III (adopted) covers its other half too
        if o_id in known and known[o_id]["state"] == "ok" and known[o_id]["ff_id"] and i_id not in known:
            with db.tx() as c2:
                c2.execute("INSERT INTO ff_tx(tx_id,ff_id,sig,state) VALUES(?,?,NULL,'paired') ON CONFLICT(tx_id) DO NOTHING", (i_id, known[o_id]["ff_id"]))
            known[i_id] = {"tx_id": i_id, "ff_id": known[o_id]["ff_id"], "state": "paired", "sig": None, "attempts": 0}
    byid = {r["id"]: r for r in rows}
    liab = {aid for aid, m in maps.items() if m.get("ff_type") == "liability"}
    pairs = {o: i for o, i in _pairs([r for r in rows if r["id"] not in known]).items()
             if not (byid[o]["account_id"] in liab and byid[i]["account_id"] in liab)}        # Firefly III has nothing for money from one debt to another: each half goes alone
    paired_in = set(pairs.values())
    # the data-safety hold: per account, how many would be created against how many are already there
    holding = set()
    for aid, m in maps.items():
        new = sum(1 for r in rows if r["account_id"] == aid and r["id"] not in known and r["id"] not in paired_in)
        m = db.one("SELECT * FROM ff_accounts WHERE account_id=?", (aid,)) or m          # adoption above has just counted what Firefly III holds
        have = max(m.get("ff_seen") or 0, db.one("SELECT COUNT(*) n FROM ff_tx f JOIN transactions t ON t.id=f.tx_id WHERE t.account_id=? AND f.state IN ('ok','paired')", (aid,))["n"])
        # an account this connector made itself, empty when it looked (ff_seen 0), only ever holds what it sent: nothing there to send twice.
        # (A big first history goes over several rounds; without this the second round looked like a relink and was held.)
        ours = bool(m.get("created")) and not (m.get("ff_seen") or 0)
        big = aid not in created_now and not ours and have >= HOLD_MIN_EXISTING and new >= max(HOLD_MIN_NEW, have * HOLD_SHARE)
        with db.tx() as c2:
            if big and not m.get("release"):
                holding.add(aid)
                c2.execute("UPDATE ff_accounts SET held=? WHERE account_id=?", (new, aid))
            elif big:                                               # released by the admin: send, and keep the release until the batch is through
                c2.execute("UPDATE ff_accounts SET held=0 WHERE account_id=?", (aid,))
            else:
                c2.execute("UPDATE ff_accounts SET held=0, release=0 WHERE account_id=?", (aid,))
        if aid in holding:
            res["held"].append({"account_id": aid, "label": account_label(accts[aid]), "waiting": new, "existing": have})
    for t in rows:
        if done >= limit:
            res["remaining"] += 1
            continue
        k = known.get(t["id"])
        if k and k["state"] == "paired":                            # the other half of a transfer that is already sent
            continue
        if k and k["state"] == "error" and (k["attempts"] or 0) >= MAX_ATTEMPTS:
            continue
        if t["account_id"] in holding and not (k and k["state"] == "ok"):
            continue                                                # held: nothing new goes to this account until the admin says so
        sig = _sig(t, c["send_categories"])
        cur = (accts[t["account_id"]].get("currency") or "USD")
        if t["id"] in paired_in:                                    # the inflow side of a transfer: the outflow carries both
            if not k:
                with db.tx() as c2:
                    c2.execute("INSERT INTO ff_tx(tx_id,state,sig) VALUES(?, 'paired', ?) ON CONFLICT(tx_id) DO NOTHING", (t["id"], sig))    # (the outflow's turn may already have recorded it)
            continue
        try:
            if k and k["state"] == "ok" and k["sig"] == sig:
                continue
            if k and k["state"] == "ok" and k["ff_id"]:             # changed at the bank: update the Firefly III copy
                p = payload_for(t, maps[t["account_id"]]["ff_id"], {**c, "send_categories": False}, cur)      # a category set in Firefly III is never overwritten
                ff.update_tx(k["ff_id"], {"apply_rules": False, "fire_webhooks": True, "transactions": p["transactions"]})
                with db.tx() as c2:
                    c2.execute("UPDATE ff_tx SET sig=?, pushed=? WHERE tx_id=?", (sig, date.today().isoformat(), t["id"]))
                res["updated"] += 1
                done += 1
                continue
            if t["id"] in pairs and pairs[t["id"]] in byid:         # outflow of a transfer between two of your accounts: one transfer
                i = byid[pairs[t["id"]]]
                leg = payload_for(t, maps[t["account_id"]]["ff_id"], c, cur)["transactions"][0]
                # Firefly III refuses a transfer that touches a liability: a payment into a loan is a withdrawal from the bank account into it,
                # money lent out of a loan into a bank account a deposit from it (both move both balances, as a transfer would)
                kind = "withdrawal" if i["account_id"] in liab else "deposit" if t["account_id"] in liab else "transfer"
                leg.update({"type": kind, "source_id": maps[t["account_id"]]["ff_id"], "destination_id": maps[i["account_id"]]["ff_id"]})
                leg.pop("destination_name", None); leg.pop("source_name", None)
                p = {"error_if_duplicate_hash": True, "apply_rules": c["apply_rules"], "fire_webhooks": True, "group_title": None, "transactions": [leg]}
            else:
                p = payload_for(t, maps[t["account_id"]]["ff_id"], c, cur)
            try:
                ff_id = ff.create_tx(p)
            except FireflyError as e:
                if e.status == 422 and "duplicate" in json.dumps(e.body).lower():       # it is already there (an earlier run lost track): find it and adopt it
                    ff_id = ff.find_by_external_id(t["id"])
                    if not ff_id:
                        raise
                else:
                    raise
            with db.tx() as c2:
                c2.execute("INSERT INTO ff_tx(tx_id,ff_id,sig,state,error,attempts,pushed) VALUES(?,?,?, 'ok', NULL, 0, ?) ON CONFLICT(tx_id) DO UPDATE SET ff_id=excluded.ff_id, sig=excluded.sig, state='ok', error=NULL, attempts=0, pushed=excluded.pushed",
                           (t["id"], ff_id, sig, date.today().isoformat()))
                if t["id"] in pairs:
                    c2.execute("INSERT INTO ff_tx(tx_id,ff_id,sig,state,pushed) VALUES(?,?,?, 'ok', ?) ON CONFLICT(tx_id) DO UPDATE SET ff_id=excluded.ff_id, state='ok'", (pairs[t["id"]], ff_id, _sig(byid[pairs[t["id"]]], c["send_categories"]), date.today().isoformat()))
            res["created"] += 1
            done += 1
        except FireflyError as e:
            fail(t["id"], e)
            if e.status in (401, 403):
                break
    return res


def run(db, transport=None, budget_seconds: float = 40.0) -> dict:
    """Push, remember how it went. Never raises: a problem with Firefly III must not break the bank sync."""
    c = conf(db)
    if not c["enabled"]:
        return {"skipped": "not set up"}
    try:
        ff = Firefly(c["url"], c["token"], transport)
        res, t0 = push(db, ff), time.time()
        while res["remaining"] and not res["errors"] and time.time() - t0 < budget_seconds:          # a long history goes across in one go, within a time budget; the rest follows at the next push
            more = push(db, ff)
            for k in ("accounts_created", "created", "updated", "deleted", "errors"):
                res[k] += more[k]
            res["remaining"], res["error_samples"] = more["remaining"], (res["error_samples"] + more["error_samples"])[:3]
        _state_set(db, "last_run", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        _state_set(db, "last_error", res["error_samples"][0] if res["errors"] and res["error_samples"] else "")
        if not res["errors"]:
            _state_set(db, "last_ok", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return res
    except FireflyError as e:
        _state_set(db, "last_error", str(e)[:300])
        _state_set(db, "last_run", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return {"error": str(e)}
    except Exception as e:                                       # noqa: BLE001
        _state_set(db, "last_error", f"unexpected: {type(e).__name__}")
        return {"error": type(e).__name__}


def status(db) -> dict:
    c = conf(db)
    ensure_mappings(db)
    counts = {r["state"]: r["n"] for r in db.all("SELECT f.state, COUNT(*) n FROM ff_tx f JOIN transactions t ON t.id=f.tx_id GROUP BY f.state")}     # a removed bank's old rows don't count
    pending = db.one("SELECT COUNT(*) n FROM transactions t JOIN ff_accounts m ON m.account_id=t.account_id AND m.mode IN ('create','link') AND m.ff_id IS NOT NULL LEFT JOIN ff_tx f ON f.tx_id=t.id WHERE f.tx_id IS NULL" + ("" if c["include_pending"] else " AND t.pending=0"))["n"]
    s = _state(db)
    host = c["url"].split("//", 1)[-1].split("/")[0] if c["url"] else ""
    return {"configured": bool(c["url"] and c["token"]), "enabled": c["enabled"], "host": host, "token_set": bool(c["token"]), "last_run": s.get("last_run"), "last_ok": s.get("last_ok"), "last_error": s.get("last_error") or None,
            "sent": counts.get("ok", 0) + counts.get("paired", 0), "failed": counts.get("error", 0), "waiting": pending,
            "awaiting_approval": db.one("SELECT COUNT(*) n FROM ff_accounts WHERE mode='pending'")["n"],
            "options": {k: c[k] for k in ("send_categories", "include_pending", "apply_rules")}}
