"""Routes for linking goblins and sharing goals. Logic lives in links.py."""
from fastapi import HTTPException, Request
from pydantic import BaseModel

from goblinbank import links, plan


class InviteIn(BaseModel):
    peer: str


class ShareIn(BaseModel):
    peers: list[str]


class NoteIn(BaseModel):
    text: str


class InboxIn(BaseModel):
    type: str
    payload: dict = {}
    sender: str = ""


def register(app, ctx):
    store, auth, name = ctx.store, ctx.auth, ctx.name

    def run(fn, *a):
        try:
            return fn(*a)
        except links.LinkError as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "not found")

    def done(res=None):
        ctx.shared_changed()
        return res if res is not None else {"ok": True}

    @app.get("/api/links")
    def list_links(request: Request):
        auth(request)
        waiting = store.one("SELECT COUNT(*) n FROM outbox")["n"]
        return {"me": name, "links": store.all("SELECT peer, status, created FROM links ORDER BY peer"), "waiting": waiting}

    @app.post("/api/links")
    def link_invite(request: Request, b: InviteIn):
        auth(request)
        r = run(links.invite, store, name, b.peer)
        ctx.link_flush()
        return r

    @app.post("/api/links/{peer}/accept")
    def link_accept(request: Request, peer: str):
        auth(request)
        r = run(links.accept, store, peer)
        ctx.link_flush()
        ctx.shared_changed()
        return r

    @app.delete("/api/links/{peer}")
    def link_remove(request: Request, peer: str):
        auth(request)
        run(links.unlink, store, peer)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/links/flush")
    def link_flush(request: Request):
        auth(request)
        ctx.shared_changed()
        return {"waiting": store.one("SELECT COUNT(*) n FROM outbox")["n"]}

    @app.post("/internal/link/inbox")
    def inbox(request: Request, m: InboxIn):
        """From the gateway only (it sets `sender` after checking who really sent it)."""
        auth(request)
        r = links.receive(store, name, m.sender, m.type, m.payload)
        if r.get("ok"):
            ctx.link_flush()
        return r

    @app.post("/api/goals/{gid}/share")
    def goal_share(request: Request, gid: int, b: ShareIn):
        auth(request)
        r = run(links.share_goal, store, name, gid, b.peers)
        ctx.link_flush()
        return r

    @app.post("/api/goals/{gid}/unshare")
    def goal_unshare(request: Request, gid: int):
        auth(request)
        run(links.stop_sharing, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/accept")
    def goal_accept(request: Request, gid: int):
        auth(request)
        run(links.accept_goal, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/decline")
    def goal_decline(request: Request, gid: int):
        auth(request)
        run(links.decline_goal, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/notes")
    def note_add(request: Request, gid: int, b: NoteIn):
        auth(request)
        r = run(links.add_note, store, name, gid, b.text)
        ctx.link_flush()
        return r

    @app.delete("/api/goals/{gid}/notes/{nid}")
    def note_delete(request: Request, gid: int, nid: str):
        auth(request)
        run(links.delete_note, store, name, gid, nid)
        ctx.link_flush()
        return {"ok": True}
