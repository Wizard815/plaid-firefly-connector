"""Routes for savings places, goals + allocations, income and the monthly plan. Logic lives in plan.py."""
from datetime import date

from fastapi import HTTPException, Request
from pydantic import BaseModel

from goblinbank import plan, recur, sync


class GoalIn(BaseModel):
    name: str
    target: float
    deadline: str | None = None
    priority: int = 2
    monthly: float = 0
    note: str = ""
    auto_allocate: bool = False         # tick to set aside whatever savings are free right now; otherwise the goal starts unfunded


class GoalPatch(BaseModel):
    name: str | None = None
    target: float | None = None
    deadline: str | None = None
    priority: int | None = None
    monthly: float | None = None
    note: str | None = None


class AllocIn(BaseModel):
    place: str = ""
    amount: float


class ReleaseIn(BaseModel):
    amount: float | None = None
    place: str | None = None


class CloseIn(BaseModel):
    status: str = "done"


class MoveIn(BaseModel):
    from_id: int
    to_id: int
    amount: float


class IncomeIn(BaseModel):
    name: str
    amount: float
    next_date: str
    cycle: str | None = None
    every: int | None = None
    unit: str | None = None
    account_id: str | None = None
    active: bool = True


class AcceptIncome(BaseModel):
    key: str
    name: str


def register(app, ctx):
    store, auth = ctx.store, ctx.auth

    def run(fn, *a, **k):
        try:
            return fn(*a, **k)
        except plan.PlanError as e:
            raise HTTPException(409 if any(w in str(e) for w in ("free", "earmark", "closed")) else 422, str(e))   # 409: valid request, but the books say no
        except KeyError:
            raise HTTPException(404, "not found")

    @app.get("/api/goals")
    def goals(request: Request):
        auth(request)
        return plan.goals_view(store, ctx.today())

    @app.post("/api/goals")
    def goal_add(request: Request, g: GoalIn):
        auth(request)
        return run(plan.add_goal, store, g.name, g.target, ctx.today(), g.deadline, g.priority, g.monthly, g.note, g.auto_allocate)

    @app.put("/api/goals/{gid}")
    def goal_update(request: Request, gid: int, g: GoalPatch):
        auth(request)
        return run(plan.update_goal, store, gid, ctx.today(), **g.model_dump())

    @app.delete("/api/goals/{gid}")
    def goal_delete(request: Request, gid: int):
        auth(request)
        plan.delete_goal(store, gid)
        return {"deleted": gid}

    @app.post("/api/goals/{gid}/allocate")
    def goal_allocate(request: Request, gid: int, a: AllocIn):
        auth(request)
        return run(plan.set_allocation, store, gid, a.place, a.amount, ctx.today())

    @app.post("/api/goals/{gid}/fund")
    def goal_fund(request: Request, gid: int, a: AllocIn):
        auth(request)
        return run(plan.fund, store, gid, a.amount, ctx.today(), a.place)

    @app.post("/api/goals/{gid}/release")
    def goal_release(request: Request, gid: int, r: ReleaseIn):
        auth(request)
        return run(plan.release, store, gid, ctx.today(), r.amount, r.place)

    @app.post("/api/goals/{gid}/close")
    def goal_close(request: Request, gid: int, c: CloseIn):
        auth(request)
        if c.status not in ("done", "dropped", "active"):
            raise HTTPException(422, "status must be done, dropped or active")
        return run(plan.close_goal, store, gid, ctx.today(), c.status)

    @app.post("/api/goals/move")
    def goal_move(request: Request, m: MoveIn):
        auth(request)
        return run(plan.move, store, m.from_id, m.to_id, m.amount, ctx.today())

    @app.post("/api/goals/autofill")
    def goal_autofill(request: Request):
        auth(request)
        return plan.autofill(store, ctx.today())

    @app.get("/api/maturities")
    def maturities(request: Request, days: int = 365):
        auth(request)
        return plan.maturity_outlook(store, ctx.today(), max(1, min(days, 3650)))

    @app.get("/api/afford")
    def afford(request: Request, amount: float):
        auth(request)
        if amount <= 0:
            raise HTTPException(422, "amount must be above 0")
        return plan.afford(store, amount, ctx.today())

    @app.get("/api/plan")
    def monthly(request: Request, month: str | None = None):
        auth(request)
        month = month or ctx.today().strftime("%Y-%m")
        try:
            date.fromisoformat(month + "-01")
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")
        return plan.monthly_plan(store, month, ctx.today())

    # ---- income
    def check_income(i: IncomeIn):
        try:
            every, unit = recur.resolve(i.every, i.unit, i.cycle)
            date.fromisoformat(i.next_date)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if i.amount <= 0 or not i.name.strip():
            raise HTTPException(422, "name and an amount above 0 are required")
        if i.account_id and not store.one("SELECT 1 FROM accounts WHERE id=?", (i.account_id,)):
            raise HTTPException(404, "account_id does not exist")
        return every, unit

    @app.get("/api/incomes")
    def incomes(request: Request):
        auth(request)
        return {"items": plan.incomes(store, ctx.today()), "suggestions": plan.income_suggestions(store, ctx.today())}

    @app.post("/api/incomes")
    def income_add(request: Request, i: IncomeIn):
        auth(request)
        every, unit = check_income(i)
        with store.tx() as c:
            cur = c.execute("INSERT INTO incomes(name,amount,every,unit,next_date,account_id,active) VALUES(?,?,?,?,?,?,?)", (i.name.strip(), i.amount, every, unit, i.next_date, i.account_id or None, int(i.active)))
        return plan.income_view(store.one("SELECT * FROM incomes WHERE id=?", (cur.lastrowid,)), ctx.today())

    @app.put("/api/incomes/{iid}")
    def income_update(request: Request, iid: int, i: IncomeIn):
        auth(request)
        every, unit = check_income(i)
        with store.tx() as c:
            cur = c.execute("UPDATE incomes SET name=?,amount=?,every=?,unit=?,next_date=?,account_id=?,active=? WHERE id=?", (i.name.strip(), i.amount, every, unit, i.next_date, i.account_id or None, int(i.active), iid))
        if not cur.rowcount:
            raise HTTPException(404, "not found")
        return plan.income_view(store.one("SELECT * FROM incomes WHERE id=?", (iid,)), ctx.today())

    @app.delete("/api/incomes/{iid}")
    def income_delete(request: Request, iid: int):
        auth(request)
        with store.tx() as c:
            c.execute("DELETE FROM incomes WHERE id=?", (iid,))
        return {"deleted": iid}

    @app.post("/api/incomes/accept")
    def income_accept(request: Request, a: AcceptIncome):
        auth(request)
        s = next((x for x in plan.income_suggestions(store, ctx.today()) if x["key"] == a.key), None)
        if not s:
            raise HTTPException(404, "suggestion not found")
        made = income_add(request, IncomeIn(name=a.name, amount=s["amount"], next_date=s["next_date"], every=s["every"], unit=s["unit"], account_id=s["account_id"]))
        with store.tx() as c:
            c.execute("UPDATE incomes SET match_key=? WHERE id=?", (s["key"], made["id"]))      # stays accepted even if you renamed it
        return made
