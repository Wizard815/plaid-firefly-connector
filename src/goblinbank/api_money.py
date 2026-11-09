"""HTTP routes for accounts, transactions, categories, subscriptions, savings, planner and overview.
Thin wrappers: the logic lives in ledger.py / recur.py so the agent uses exactly the same code."""
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Request
from pydantic import BaseModel

from goblinbank import feed, ledger, recur, sync


class Sub(BaseModel):
    name: str
    amount: float
    next_date: str
    cycle: str | None = None            # preset: weekly, monthly, quarterly, yearly ...
    every: int | None = None            # or custom: every N units
    unit: str | None = None
    currency: str = "USD"
    category: str = "Subscriptions"
    url: str = ""
    notes: str = ""
    active: bool = True
    account_id: str | None = None       # bound account: a Plaid one or one you made by hand
    payment_method: str = ""            # free text when there is no account to bind (cash, a friend's card)
    payer: str = "me"                   # me | other
    payer_name: str = ""
    notify_days: int = 3
    end_date: str | None = None


class AccountIn(BaseModel):
    name: str | None = None
    institution: str | None = None
    kind: str | None = None
    balance: float | None = None
    min_balance: float | None = None
    savings_mode: str | None = None        # auto | yes | no: does this account count as savings?
    nickname: str | None = None            # your own label for any account ("" clears it)
    pinned: bool | None = None             # show its balance on the Overview
    joint_with: str | None = None          # "" clears; the other goblin that also links this account
    my_share: float | None = None          # percent of its balance that is mine (joint accounts); -1 clears


class TxIn(BaseModel):
    account_id: str
    date: str
    name: str
    amount: float
    category: str | None = None


class SavingsIn(BaseModel):
    name: str
    kind: str = "savings"
    institution: str = ""
    balance: float = 0
    apy: float = 0
    opened: str | None = None
    maturity: str | None = None
    term_months: int | None = None
    account_id: str | None = None
    goal: float | None = None
    notes: str = ""
    active: bool = True


class NoteIn(BaseModel):
    title: str
    body: str = ""
    pinned: bool = False


KINDS = {"cash", "credit", "investment", "loan"}
SAVINGS_KINDS = {"savings", "cd", "money_market", "bond", "hsa", "529", "cash", "other"}


def make_ctx(store, auth, today, **kw):
    return SimpleNamespace(store=store, auth=auth, today=today, **kw)


def register(app, ctx):
    store, auth = ctx.store, ctx.auth

    def bad(msg, code=422):
        raise HTTPException(code, msg)

    def date_ok(v, field):
        try:
            date.fromisoformat(v)
        except (TypeError, ValueError):
            bad(f"{field} must be YYYY-MM-DD")

    # ------------------------------------------------------------ overview
    @app.get("/api/overview")
    def overview(request: Request, days: int = 30):
        auth(request)
        return ledger.overview(store, days, ctx.today())

    @app.get("/api/forecast")
    def forecast(request: Request, months: int = 6):
        auth(request)
        return ledger.spending_forecast(store, ctx.today(), months)

    @app.get("/api/upcoming")
    def upcoming(request: Request, days: int = 30):
        auth(request)
        return ledger.upcoming(store, days, ctx.today())

    @app.get("/api/alerts")
    def alerts(request: Request):
        auth(request)
        return ledger.alerts(store, ctx.today())

    class AlertKey(BaseModel):
        key: str

    @app.post("/api/alerts/dismiss")
    def alert_dismiss(request: Request, b: AlertKey):
        """Mark an alert handled (for example a matured CD you have rolled over). It stays quiet; a new situation gets a new key."""
        auth(request)
        if not any(a["key"] == b.key for a in ledger.alerts(store, ctx.today())):
            bad("that alert is not active", 404)
        with store.tx() as c:
            c.execute("INSERT OR IGNORE INTO dismissed VALUES(?)", (f"alert:{b.key}",))
        return {"dismissed": b.key}

    @app.get("/api/projection")
    def projection(request: Request, days: int = 30, account_id: str | None = None, income: bool = True):
        auth(request)
        return ledger.project_balance(store, days, ctx.today(), [account_id] if account_id else None, income)

    @app.get("/api/history")
    def history(request: Request):
        """Daily net worth built from balance snapshots (last known balance carried forward)."""
        auth(request)
        kinds = {a["id"]: a["kind"] for a in store.all("SELECT id,kind FROM accounts")}
        shares = {a["id"]: (100.0 if a["my_share"] is None else a["my_share"]) / 100 for a in store.all("SELECT id,my_share FROM accounts")}
        sign = {"credit": -1, "loan": -1}
        last, by_day = {}, {}
        for r in store.all("SELECT * FROM snapshots ORDER BY ts"):
            last[r["account_id"]] = r["balance"] * shares.get(r["account_id"], 1.0) * sign.get(kinds.get(r["account_id"], "cash"), 1)
            by_day[r["ts"][:10]] = round(sum(last.values()), 2)
        return [{"date": d, "net_worth": v} for d, v in by_day.items()]

    @app.get("/api/events")
    def events(request: Request, limit: int = 50):
        auth(request)
        return store.all("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),))

    # ------------------------------------------------------------ accounts
    @app.get("/api/accounts")
    def accounts(request: Request):
        auth(request)
        return store.all("SELECT * FROM accounts ORDER BY kind, institution, name")

    @app.post("/api/accounts")
    def account_add(request: Request, a: AccountIn):
        """An account you track by hand (cash, a friend's card, anything Plaid cannot reach)."""
        auth(request)
        if not a.name:
            bad("name required")
        if (a.kind or "cash") not in KINDS:
            bad(f"kind must be one of {sorted(KINDS)}")
        aid = f"own-{uuid.uuid4().hex[:8]}"
        with store.tx() as c:
            c.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,min_balance,updated) VALUES(?,?,?,?,?,?,?,?)",
                      (aid, "manual", a.institution or "Manual", a.name, a.kind or "cash", a.balance, a.min_balance, sync.now()))
        sync.record_balance(store, aid, a.name, a.balance, a.kind or "cash")
        return store.one("SELECT * FROM accounts WHERE id=?", (aid,))

    @app.get("/api/accounts/{aid}")
    def account_detail(request: Request, aid: str):
        auth(request)
        a = store.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not a:
            bad("account not found", 404)
        today = ctx.today()
        recent = ledger.query_transactions(store, account_id=aid, start=(today - timedelta(days=30)).isoformat())
        return {"account": a, "last_30_days": {"in": recent["sum_in"], "out": recent["sum_out"], "count": recent["total"]},
                "subscriptions": [s for s in ledger.subscriptions(store, today) if s["account_id"] == aid],
                "savings": [ledger.savings_view(store, s, today) for s in store.all("SELECT * FROM savings WHERE account_id=?", (aid,))],
                "history": [{"ts": r["ts"], "balance": r["balance"]} for r in store.all("SELECT ts,balance FROM snapshots WHERE account_id=? ORDER BY ts", (aid,))],
                "alerts": [x for x in ledger.alerts(store, today) if x.get("account_id") == aid]}

    @app.put("/api/accounts/{aid}")
    def account_update(request: Request, aid: str, a: AccountIn):
        auth(request)
        row = store.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not row:
            bad("account not found", 404)
        manual = row["source"] == "manual"
        if not manual and (a.balance is not None or a.kind):
            bad("balance and type of a linked account come from the bank; only manual accounts can be edited", 409)
        if a.savings_mode not in (None, "auto", "yes", "no"):
            bad("savings_mode must be auto, yes or no")
        with store.tx() as c:
            if a.savings_mode:
                c.execute("UPDATE accounts SET savings_pool=? WHERE id=?", ({"auto": None, "yes": 1, "no": 0}[a.savings_mode], aid))
            if a.min_balance is not None:
                c.execute("UPDATE accounts SET min_balance=? WHERE id=?", (a.min_balance if a.min_balance >= 0 else None, aid))
            if a.nickname is not None:
                c.execute("UPDATE accounts SET nickname=? WHERE id=?", (a.nickname.strip()[:60] or None, aid))
            if a.joint_with is not None:
                c.execute("UPDATE accounts SET joint_with=? WHERE id=?", (a.joint_with.strip().lower()[:32] or None, aid))
            if a.my_share is not None:
                if a.my_share > 100:
                    bad("my_share is a percentage from 0 to 100")
                c.execute("UPDATE accounts SET my_share=? WHERE id=?", (a.my_share if a.my_share >= 0 else None, aid))
            if a.pinned is not None:
                c.execute("UPDATE accounts SET pinned=? WHERE id=?", (1 if a.pinned else 0, aid))
            if manual:
                if a.name:
                    c.execute("UPDATE accounts SET name=? WHERE id=?", (a.name, aid))
                if a.institution:
                    c.execute("UPDATE accounts SET institution=? WHERE id=?", (a.institution, aid))
                if a.kind:
                    if a.kind not in KINDS:
                        bad(f"kind must be one of {sorted(KINDS)}")
                    c.execute("UPDATE accounts SET kind=? WHERE id=?", (a.kind, aid))
                if a.balance is not None:
                    c.execute("UPDATE accounts SET balance=?, updated=? WHERE id=?", (a.balance, sync.now(), aid))
        if manual and a.balance is not None:
            sync.record_balance(store, aid, row["name"], a.balance, row["kind"])
        return store.one("SELECT * FROM accounts WHERE id=?", (aid,))

    @app.delete("/api/accounts/{aid}")
    def account_delete(request: Request, aid: str):
        auth(request)
        row = store.one("SELECT source FROM accounts WHERE id=?", (aid,))
        if not row:
            bad("account not found", 404)
        if row["source"] != "manual":
            bad("linked accounts are removed by unlinking the bank in Settings", 409)
        with store.tx() as c:
            c.execute("UPDATE subscriptions SET account_id=NULL WHERE account_id=?", (aid,))
            c.execute("UPDATE savings SET account_id=NULL WHERE account_id=?", (aid,))
            for t in ("transactions", "snapshots", "holdings"):
                c.execute(f"DELETE FROM {t} WHERE account_id=?", (aid,))
            c.execute("DELETE FROM accounts WHERE id=?", (aid,))
        return {"deleted": aid}

    # ------------------------------------------------------------ transactions + categories
    @app.get("/api/transactions")
    def transactions(request: Request, q: str | None = None, account_id: str | None = None, category: str | None = None,
                     min_amount: float | None = None, max_amount: float | None = None, start: str | None = None, end: str | None = None,
                     direction: str | None = None, limit: int = 100, offset: int = 0, sort: str = "date_desc"):
        auth(request)
        for v, f in ((start, "start"), (end, "end")):
            if v:
                date_ok(v, f)
        if direction not in (None, "in", "out"):
            bad("direction must be in or out")
        return ledger.query_transactions(store, q, account_id, category, min_amount, max_amount, start, end, direction, limit, offset, sort)

    @app.post("/api/transactions")
    def tx_add(request: Request, t: TxIn):
        auth(request)
        date_ok(t.date, "date")
        if not store.one("SELECT 1 FROM accounts WHERE id=?", (t.account_id,)):
            bad("account not found", 404)
        cat = t.category or ledger.categorize(store, t.name, t.amount)
        if not store.one("SELECT 1 FROM categories WHERE name=?", (cat,)):
            bad(f"unknown category {cat!r}")
        tid = f"manual:{uuid.uuid4().hex[:12]}"
        with store.tx() as c:
            c.execute("INSERT INTO transactions(id,account_id,date,name,amount,source,category,category_locked) VALUES(?,?,?,?,?,?,?,?)",
                      (tid, t.account_id, t.date, t.name, t.amount, "manual", cat, int(bool(t.category))))
            c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)", (f"tx:{tid}", f"{t.date}T00:00:00+00:00", "transaction",
                      f"{t.name} {sync.money(t.amount)}", t.amount, t.account_id, "manual entry"))
        return store.one("SELECT * FROM transactions WHERE id=?", (tid,))

    class CatSet(BaseModel):
        category: str
        make_rule: bool = False

    @app.put("/api/transactions/{tid}/category")
    def tx_category(request: Request, tid: str, body: CatSet):
        auth(request)
        try:
            return ledger.set_category(store, tid, body.category, body.make_rule)
        except KeyError:
            bad("transaction not found", 404)
        except ValueError as e:
            bad(str(e))

    @app.get("/api/spending")
    def spending(request: Request, start: str | None = None, end: str | None = None):
        auth(request)
        return ledger.spending_by_category(store, start, end)

    @app.get("/api/categories")
    def categories(request: Request):
        auth(request)
        return store.all("SELECT * FROM categories ORDER BY kind, name")

    class CatIn(BaseModel):
        name: str
        kind: str = "expense"

    @app.post("/api/categories")
    def category_add(request: Request, c: CatIn):
        auth(request)
        name = c.name.strip()
        if not name or len(name) > 30:
            bad("name must be 1-30 characters")
        if c.kind not in ("expense", "income", "transfer"):
            bad("kind must be expense, income or transfer")
        with store.tx() as cn:
            cn.execute("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,0)", (name, c.kind))
        return store.all("SELECT * FROM categories ORDER BY kind, name")

    class CatKind(BaseModel):
        kind: str

    @app.put("/api/categories/{name}")
    def category_kind(request: Request, name: str, b: CatKind):
        """Tag a category as spending, income, or 'not counted' (transfer). Works on built-in ones too."""
        auth(request)
        if b.kind not in ("expense", "income", "transfer"):
            bad("kind must be expense, income or transfer")
        with store.tx() as c:
            cur = c.execute("UPDATE categories SET kind=? WHERE name=?", (b.kind, name))
        if not cur.rowcount:
            bad("not found", 404)
        return store.all("SELECT * FROM categories ORDER BY kind, name")

    @app.delete("/api/categories/{name}")
    def category_delete(request: Request, name: str):
        auth(request)
        row = store.one("SELECT builtin FROM categories WHERE name=?", (name,))
        if not row:
            bad("not found", 404)
        if row["builtin"]:
            bad("built-in categories cannot be deleted", 409)
        with store.tx() as c:
            c.execute("UPDATE transactions SET category='Misc' WHERE category=?", (name,))
            c.execute("UPDATE subscriptions SET category='Subscriptions' WHERE category=?", (name,))
            c.execute("DELETE FROM category_rules WHERE category=?", (name,))
            c.execute("DELETE FROM budgets WHERE category=?", (name,))
            c.execute("DELETE FROM categories WHERE name=?", (name,))
        return {"deleted": name}

    @app.get("/api/category-rules")
    def rules(request: Request):
        auth(request)
        return store.all("SELECT * FROM category_rules ORDER BY category, pattern")

    @app.delete("/api/category-rules/{rid}")
    def rule_delete(request: Request, rid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM category_rules WHERE id=?", (rid,))
        return {"deleted": rid}

    @app.post("/api/categories/recategorize")
    def recategorize(request: Request):
        auth(request)
        return {"changed": ledger.recategorize(store)}

    # ------------------------------------------------------------ subscriptions
    def check_sub(s: Sub) -> tuple[int, str]:
        try:
            every, unit = recur.resolve(s.every, s.unit, s.cycle)
        except ValueError as e:
            bad(str(e))
        date_ok(s.next_date, "next_date")
        if s.end_date:
            date_ok(s.end_date, "end_date")
        if s.amount < 0:
            bad("amount must be >= 0")
        if not s.name.strip():
            bad("name required")
        if s.payer not in ("me", "other"):
            bad("payer must be me or other")
        if s.payer == "other" and not s.payer_name.strip():
            bad("payer_name is required when someone else pays")
        if s.account_id and not store.one("SELECT 1 FROM accounts WHERE id=?", (s.account_id,)):
            bad("account_id does not exist", 404)
        if not store.one("SELECT 1 FROM categories WHERE name=?", (s.category,)):
            bad(f"unknown category {s.category!r}")
        if not 0 <= s.notify_days <= 60:
            bad("notify_days must be 0-60")
        return every, unit

    def sub_row(s: Sub, every, unit):
        return (s.name.strip(), s.amount, s.currency, recur.label(every, unit), every, unit, s.next_date, s.category, s.url, s.notes, int(s.active),
                s.account_id or None, s.payment_method, s.payer, s.payer_name.strip() if s.payer == "other" else "", s.notify_days, s.end_date or None)

    COLS = "name,amount,currency,cycle,every,unit,next_date,category,url,notes,active,account_id,payment_method,payer,payer_name,notify_days,end_date"

    @app.get("/api/subscriptions")
    def subs_list(request: Request):
        auth(request)
        rows = ledger.subscriptions(store, ctx.today())
        mo = round(sum(r["monthly"] for r in rows), 2)
        return {"items": rows, "monthly_total": mo, "yearly_total": round(12 * mo, 2),
                "by_category": {c: round(sum(r["monthly"] for r in rows if r["category"] == c), 2) for c in sorted({r["category"] for r in rows if r["monthly"]})}}

    def create_sub(d: dict) -> dict:
        """One code path for the form and for the agent: same validation, same insert."""
        try:
            s = Sub(**d)
        except Exception as e:
            raise HTTPException(422, str(e)[:200])
        every, unit = check_sub(s)
        with store.tx() as c:
            cur = c.execute(f"INSERT INTO subscriptions({COLS},created) VALUES({','.join('?' * 17)},?)", sub_row(s, every, unit) + (sync.now(),))
        return ledger.subscription(store, cur.lastrowid, ctx.today())

    ctx.create_sub = create_sub

    @app.post("/api/subscriptions")
    def subs_add(request: Request, s: Sub):
        auth(request)
        return create_sub(s.model_dump())

    @app.put("/api/subscriptions/{sid}")
    def subs_update(request: Request, sid: int, s: Sub):
        auth(request)
        every, unit = check_sub(s)
        sets = ",".join(f"{c}=?" for c in COLS.split(","))
        with store.tx() as c:
            cur = c.execute(f"UPDATE subscriptions SET {sets} WHERE id=?", sub_row(s, every, unit) + (sid,))
        if not cur.rowcount:
            bad("not found", 404)
        return ledger.subscription(store, sid, ctx.today())

    @app.delete("/api/subscriptions/{sid}")
    def subs_delete(request: Request, sid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        return {"deleted": sid}

    @app.get("/api/suggestions")
    def suggestions(request: Request):
        auth(request)
        return ledger.suggestions(store, ctx.today())

    class Accept(BaseModel):
        key: str
        name: str
        amount: float | None = None
        every: int | None = None
        unit: str | None = None
        account_id: str | None = None
        payer: str = "me"
        payer_name: str = ""
        next_date: str | None = None
        category: str = "Subscriptions"

    @app.post("/api/suggestions/accept")
    def suggestion_accept(request: Request, a: Accept):
        auth(request)
        sug = next((x for x in ledger.suggestions(store, ctx.today()) if x["key"] == a.key), None)
        if not sug:
            bad("suggestion not found (already added or dismissed?)", 404)
        s = Sub(name=a.name, amount=a.amount if a.amount is not None else sug["amount"], next_date=a.next_date or sug["next_date"], every=a.every or sug["every"],
                unit=a.unit or sug["unit"], account_id=a.account_id or sug.get("account_id"), payer=a.payer, payer_name=a.payer_name, category=a.category)
        every, unit = check_sub(s)
        with store.tx() as c:
            cur = c.execute(f"INSERT INTO subscriptions({COLS},created) VALUES({','.join('?' * 17)},?)", sub_row(s, every, unit) + (sync.now(),))
            if sug.get("proposal_id"):
                c.execute("UPDATE proposals SET status='accepted' WHERE id=?", (sug["proposal_id"],))
            else:
                c.execute("UPDATE subscriptions SET match_key=? WHERE id=?", (sug["key"], cur.lastrowid))
        return ledger.subscription(store, cur.lastrowid, ctx.today())

    class Dismiss(BaseModel):
        key: str

    @app.post("/api/suggestions/dismiss")
    def suggestion_dismiss(request: Request, b: Dismiss):
        auth(request)
        with store.tx() as c:
            if b.key.startswith("proposal:"):
                c.execute("UPDATE proposals SET status='dismissed' WHERE id=?", (int(b.key.split(":")[1]),))
            else:
                c.execute("INSERT OR IGNORE INTO dismissed VALUES(?)", (b.key,))
        return {"dismissed": b.key}

    # ------------------------------------------------------------ savings
    def check_savings(s: SavingsIn):
        if not s.name.strip():
            bad("name required")
        if s.kind not in SAVINGS_KINDS:
            bad(f"kind must be one of {sorted(SAVINGS_KINDS)}")
        if s.balance < 0 or s.apy < 0 or s.apy > 100:
            bad("balance must be >= 0 and apy between 0 and 100")
        for v, f in ((s.opened, "opened"), (s.maturity, "maturity")):
            if v:
                date_ok(v, f)
        if s.opened and s.maturity and s.maturity < s.opened:
            bad("maturity is before opened")
        if s.account_id and not store.one("SELECT 1 FROM accounts WHERE id=?", (s.account_id,)):
            bad("account_id does not exist", 404)

    SC = "name,kind,institution,balance,apy,opened,maturity,term_months,account_id,goal,notes,active"

    def sv(s: SavingsIn):
        return (s.name.strip(), s.kind, s.institution, s.balance, s.apy, s.opened, s.maturity, s.term_months, s.account_id or None, s.goal, s.notes, int(s.active))

    @app.get("/api/savings")
    def savings(request: Request):
        auth(request)
        return ledger.savings_summary(store, ctx.today())

    @app.post("/api/savings")
    def savings_add(request: Request, s: SavingsIn):
        auth(request)
        check_savings(s)
        with store.tx() as c:
            cur = c.execute(f"INSERT INTO savings({SC}) VALUES({','.join('?' * 12)})", sv(s))
        return ledger.savings_view(store, store.one("SELECT * FROM savings WHERE id=?", (cur.lastrowid,)), ctx.today())

    @app.put("/api/savings/{sid}")
    def savings_update(request: Request, sid: int, s: SavingsIn):
        auth(request)
        check_savings(s)
        with store.tx() as c:
            cur = c.execute(f"UPDATE savings SET {','.join(f'{k}=?' for k in SC.split(','))} WHERE id=?", sv(s) + (sid,))
        if not cur.rowcount:
            bad("not found", 404)
        return ledger.savings_view(store, store.one("SELECT * FROM savings WHERE id=?", (sid,)), ctx.today())

    @app.delete("/api/savings/{sid}")
    def savings_delete(request: Request, sid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM savings WHERE id=?", (sid,))
        return {"deleted": sid}

    # ------------------------------------------------------------ planner: notes + budgets
    @app.get("/api/notes")
    def notes(request: Request):
        auth(request)
        return store.all("SELECT * FROM notes ORDER BY pinned DESC, updated DESC")

    @app.post("/api/notes")
    def note_add(request: Request, n: NoteIn):
        auth(request)
        if not n.title.strip():
            bad("title required")
        ts = sync.now()
        with store.tx() as c:
            cur = c.execute("INSERT INTO notes(title,body,author,pinned,created,updated) VALUES(?,?,?,?,?,?)", (n.title.strip(), n.body, "me", int(n.pinned), ts, ts))
        return store.one("SELECT * FROM notes WHERE id=?", (cur.lastrowid,))

    @app.put("/api/notes/{nid}")
    def note_update(request: Request, nid: int, n: NoteIn):
        auth(request)
        with store.tx() as c:
            cur = c.execute("UPDATE notes SET title=?, body=?, pinned=?, updated=? WHERE id=?", (n.title.strip(), n.body, int(n.pinned), sync.now(), nid))
        if not cur.rowcount:
            bad("not found", 404)
        return store.one("SELECT * FROM notes WHERE id=?", (nid,))

    @app.delete("/api/notes/{nid}")
    def note_delete(request: Request, nid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM notes WHERE id=?", (nid,))
        return {"deleted": nid}

    class BudgetIn(BaseModel):
        category: str
        monthly_amount: float

    @app.get("/api/budgets")
    def budgets(request: Request, month: str | None = None):
        auth(request)
        today = ctx.today()
        month = month or today.strftime("%Y-%m")
        try:
            date.fromisoformat(month + "-01")
        except ValueError:
            bad("month must be YYYY-MM")
        return {"month": month, "items": ledger.budget_status(store, month, today)}

    @app.put("/api/budgets")
    def budget_set(request: Request, b: BudgetIn):
        auth(request)
        if not store.one("SELECT 1 FROM categories WHERE name=?", (b.category,)):
            bad("unknown category")
        if b.monthly_amount < 0:
            bad("monthly_amount must be >= 0")
        with store.tx() as c:
            c.execute("INSERT OR REPLACE INTO budgets VALUES(?,?)", (b.category, b.monthly_amount))
        return {"category": b.category, "monthly_amount": b.monthly_amount}

    @app.delete("/api/budgets/{category}")
    def budget_delete(request: Request, category: str):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM budgets WHERE category=?", (category,))
        return {"deleted": category}
