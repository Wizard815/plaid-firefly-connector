"""Links between goblins: two people agree to share a goal (and its notes), nothing else.

What crosses a link, and only for a goal both sides have agreed to share: its name, cost, deadline, priority, status, the notes thread, and one number per
person: how much that person has set aside for it. Never an account, a balance, a transaction or a private note. The gateway only relays the messages;
it keeps none of them. If the other side is offline the message waits in an outbox and goes out on the next try.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_PENDING = 10
MAX_NOTES = 200


class LinkError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status(db, peer: str) -> str | None:
    r = db.one("SELECT status FROM links WHERE peer=?", (peer,))
    return r["status"] if r else None


def active_peers(db) -> list[str]:
    return [r["peer"] for r in db.all("SELECT peer FROM links WHERE status='active' ORDER BY peer")]


def queue(db, peer: str, kind: str, payload: dict, ref: str = "") -> None:
    """Latest message per (peer, kind, thing) wins: a newer edit replaces one still waiting."""
    with db.tx() as c:
        c.execute("INSERT INTO outbox(peer,kind,ref,payload) VALUES(?,?,?,?) ON CONFLICT(peer,kind,ref) DO UPDATE SET payload=excluded.payload", (peer, kind, ref, json.dumps(payload)))


def flush(db, send) -> int:
    """Try everything waiting. `send(peer, kind, payload)` returns True when the gateway handed it over. Returns how many are still waiting."""
    for r in db.all("SELECT * FROM outbox"):
        try:
            ok = send(r["peer"], r["kind"], json.loads(r["payload"]))
        except Exception:
            ok = False
        if ok:
            with db.tx() as c:
                c.execute("DELETE FROM outbox WHERE peer=? AND kind=? AND ref=? AND payload=?", (r["peer"], r["kind"], r["ref"], r["payload"]))
    return db.one("SELECT COUNT(*) n FROM outbox")["n"]


# ------------------------------------------------------------------ pairing
def invite(db, me: str, peer: str) -> dict:
    peer = (peer or "").strip().lower()
    if not NAME.match(peer) or peer == me:
        raise LinkError("that is not another goblin's name")
    cur = status(db, peer)
    if cur == "active":
        raise LinkError("already linked")
    if cur == "pending":                                  # they already asked me: inviting back means yes
        return accept(db, peer)
    with db.tx() as c:
        c.execute("INSERT INTO links(peer,status,created) VALUES(?,?,?) ON CONFLICT(peer) DO UPDATE SET status='invited'", (peer, "invited", now()))
    queue(db, peer, "invite", {})
    return {"peer": peer, "status": "invited"}


def accept(db, peer: str) -> dict:
    if status(db, peer) != "pending":
        raise LinkError("no request from them")
    with db.tx() as c:
        c.execute("UPDATE links SET status='active' WHERE peer=?", (peer,))
    queue(db, peer, "accept", {})
    return {"peer": peer, "status": "active"}


def unlink(db, peer: str, notify: bool = True) -> None:
    """Drop the link. Goals shared with them stay here as ordinary private goals; their number and their notes' link to them go away."""
    if status(db, peer) is None:
        raise KeyError(peer)
    with db.tx() as c:
        c.execute("DELETE FROM links WHERE peer=?", (peer,))
        c.execute("DELETE FROM outbox WHERE peer=?", (peer,))
        for g in c.execute("SELECT id, members FROM goals WHERE shared_id IS NOT NULL").fetchall():
            members = [m for m in json.loads(g["members"] or "[]") if m != peer]
            c.execute("DELETE FROM goal_partners WHERE goal_id=? AND peer=?", (g["id"], peer))
            if members:
                c.execute("UPDATE goals SET members=? WHERE id=?", (json.dumps(members), g["id"]))
            else:
                _make_private(c, g["id"])
    if notify:
        queue(db, peer, "unlink", {})


def _make_private(c, gid: int) -> None:
    c.execute("UPDATE goals SET shared_id=NULL, members=NULL, shared_state=NULL, sent_sig=NULL WHERE id=?", (gid,))
    c.execute("DELETE FROM goal_partners WHERE goal_id=?", (gid,))
    c.execute("DELETE FROM goal_notes WHERE goal_id=?", (gid,))


# ------------------------------------------------------------------ shared goals
def _goal(db, gid: int) -> dict:
    g = db.one("SELECT * FROM goals WHERE id=?", (gid,))
    if not g:
        raise KeyError(gid)
    return g


def my_contrib(db, gid: int) -> float:
    return round(db.one("SELECT COALESCE(SUM(amount),0) n FROM allocations WHERE goal_id=?", (gid,))["n"], 2)


def share_goal(db, me: str, gid: int, peers: list[str]) -> dict:
    g = _goal(db, gid)
    peers = sorted({p.strip().lower() for p in peers})
    if not peers:
        raise LinkError("choose who to share it with")
    for p in peers:
        if status(db, p) != "active":
            raise LinkError(f"you are not linked with {p} yet")
    members = sorted(set(json.loads(g["members"] or "[]")) | set(peers))
    with db.tx() as c:
        c.execute("UPDATE goals SET shared_id=COALESCE(shared_id,?), members=?, shared_state='active', rev=rev+1, edited_by=?, sent_sig=NULL WHERE id=?", (uuid.uuid4().hex, json.dumps(members), me, gid))
    push(db, me, gid)
    return {"id": gid, "members": members}


def stop_sharing(db, me: str, gid: int) -> None:
    """Leave a shared goal. It stays here as a private goal; the others keep theirs (without my number)."""
    g = _goal(db, gid)
    if not g["shared_id"]:
        return
    for p in json.loads(g["members"] or "[]"):
        if status(db, p) == "active":
            queue(db, p, "leave", {"shared_id": g["shared_id"]}, g["shared_id"])
    with db.tx() as c:
        _make_private(c, gid)


def _notes(db, gid: int) -> list[dict]:
    return [{"id": n["id"], "author": n["author"], "ts": n["ts"], "text": "" if n["deleted"] else n["text"], "deleted": bool(n["deleted"])}
            for n in db.all("SELECT * FROM goal_notes WHERE goal_id=? ORDER BY ts, id", (gid,))]


def add_note(db, me: str, gid: int, text: str) -> dict:
    g = _goal(db, gid)
    text = (text or "").strip()
    if not g["shared_id"] or g["shared_state"] != "active":
        raise LinkError("notes are for shared goals; a private goal has its own note field")
    if not text or len(text) > 2000:
        raise LinkError("a note is 1 to 2000 characters")
    if db.one("SELECT COUNT(*) n FROM goal_notes WHERE goal_id=?", (gid,))["n"] >= MAX_NOTES:
        raise LinkError("this goal has as many notes as it can hold")
    nid = uuid.uuid4().hex
    with db.tx() as c:
        c.execute("INSERT INTO goal_notes(id,goal_id,author,ts,text) VALUES(?,?,?,?,?)", (nid, gid, me, now(), text))
    push(db, me, gid)
    return {"id": nid}


def delete_note(db, me: str, gid: int, nid: str) -> None:
    n = db.one("SELECT * FROM goal_notes WHERE id=? AND goal_id=?", (nid, gid))
    if not n:
        raise KeyError(nid)
    if n["author"] != me:
        raise LinkError("you can only remove your own notes")
    with db.tx() as c:
        c.execute("UPDATE goal_notes SET deleted=1, text='' WHERE id=?", (nid,))
    push(db, me, gid)


def _message(db, me: str, g: dict) -> dict:
    return {"shared_id": g["shared_id"], "rev": g["rev"], "edited_by": g["edited_by"] or me, "members": sorted(set(json.loads(g["members"] or "[]")) | {me}),
            "goal": {"name": g["name"], "target": g["target"], "deadline": g["deadline"], "priority": g["priority"], "status": g["status"], "closed": g["closed"]},
            "contrib": my_contrib(db, g["id"]), "notes": _notes(db, g["id"])}


def _sig(msg: dict) -> str:
    return hashlib.sha256(json.dumps({k: msg[k] for k in ("rev", "goal", "contrib", "notes", "members")}, sort_keys=True).encode()).hexdigest()


def push(db, me: str, gid: int | None = None) -> int:
    """Queue an update for every shared goal that changed since it was last sent (or just one). Returns how many were queued."""
    n = 0
    q = "SELECT * FROM goals WHERE shared_id IS NOT NULL AND shared_state='active'" + (" AND id=?" if gid else "")
    for g in db.all(q, (gid,) if gid else ()):
        msg = _message(db, me, g)
        sig = _sig(msg)
        if sig == g["sent_sig"]:
            continue
        for p in json.loads(g["members"] or "[]"):
            if status(db, p) == "active":
                queue(db, p, "goal", msg, g["shared_id"])
        with db.tx() as c:
            c.execute("UPDATE goals SET sent_sig=? WHERE id=?", (sig, g["id"]))
        n += 1
    return n


def bump(db, me: str, gid: int) -> None:
    """I edited a shared goal's shared fields: newer revision, then send."""
    g = db.one("SELECT shared_id FROM goals WHERE id=?", (gid,))
    if g and g["shared_id"]:
        with db.tx() as c:
            c.execute("UPDATE goals SET rev=rev+1, edited_by=? WHERE id=?", (me, gid))
        push(db, me, gid)


def accept_goal(db, me: str, gid: int) -> None:
    g = _goal(db, gid)
    if g["shared_state"] != "offered":
        raise LinkError("nothing to accept")
    with db.tx() as c:
        c.execute("UPDATE goals SET shared_state='active', sent_sig=NULL WHERE id=?", (gid,))
    push(db, me, gid)


def decline_goal(db, me: str, gid: int) -> None:
    g = _goal(db, gid)
    if g["shared_state"] != "offered":
        raise LinkError("nothing to decline")
    for p in json.loads(g["members"] or "[]"):
        if status(db, p) == "active":
            queue(db, p, "leave", {"shared_id": g["shared_id"]}, g["shared_id"])
    with db.tx() as c:
        c.execute("DELETE FROM allocations WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goal_partners WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goal_notes WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goals WHERE id=?", (gid,))


# ------------------------------------------------------------------ inbound
def receive(db, me: str, sender: str, kind: str, p: dict) -> dict:
    """One message from the gateway. `sender` is set by the gateway, never by the payload."""
    st = status(db, sender)
    if kind == "invite":
        if st is None:
            if db.one("SELECT COUNT(*) n FROM links WHERE status='pending'")["n"] >= MAX_PENDING:
                return {"ignored": "too many requests"}
            with db.tx() as c:
                c.execute("INSERT INTO links(peer,status,created) VALUES(?,?,?)", (sender, "pending", now()))
        elif st == "invited":                             # we invited each other: that is a yes
            with db.tx() as c:
                c.execute("UPDATE links SET status='active' WHERE peer=?", (sender,))
            queue(db, sender, "accept", {})
        return {"ok": True}
    if kind == "accept":
        if st == "invited":
            with db.tx() as c:
                c.execute("UPDATE links SET status='active' WHERE peer=?", (sender,))
            push(db, me)
        return {"ok": True}
    if kind == "unlink":
        if st is not None:
            unlink(db, sender, notify=False)
        return {"ok": True}
    if st != "active":
        return {"ignored": "not linked"}
    if kind == "leave":
        g = db.one("SELECT * FROM goals WHERE shared_id=?", (p.get("shared_id"),))
        if g:
            members = [m for m in json.loads(g["members"] or "[]") if m != sender]
            with db.tx() as c:
                c.execute("DELETE FROM goal_partners WHERE goal_id=? AND peer=?", (g["id"], sender))
                if members:
                    c.execute("UPDATE goals SET members=? WHERE id=?", (json.dumps(members), g["id"]))
                else:
                    _make_private(c, g["id"])
        return {"ok": True}
    if kind == "goal":
        return _receive_goal(db, me, sender, p)
    return {"ignored": "unknown message"}


def _receive_goal(db, me: str, sender: str, p: dict) -> dict:
    sid, members, goal = p.get("shared_id"), p.get("members"), p.get("goal")
    if not (isinstance(sid, str) and 8 <= len(sid) <= 64 and isinstance(members, list) and isinstance(goal, dict) and me in members and sender in members):
        return {"ignored": "malformed"}
    try:
        rev, contrib = int(p.get("rev", 0)), round(float(p.get("contrib", 0)), 2)
        name, target = str(goal["name"])[:120].strip(), round(float(goal["target"]), 2)
        prio = goal.get("priority") if goal.get("priority") in (1, 2, 3) else 2
        status_ = goal.get("status") if goal.get("status") in ("active", "done", "dropped") else "active"
        deadline = goal.get("deadline") if isinstance(goal.get("deadline"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", goal["deadline"]) else None
        closed = goal.get("closed") if isinstance(goal.get("closed"), str) else None
    except (KeyError, TypeError, ValueError):
        return {"ignored": "malformed"}
    if not name or not target > 0 or contrib < 0:
        return {"ignored": "malformed"}
    others = sorted(m for m in members if m != me)
    g = db.one("SELECT * FROM goals WHERE shared_id=?", (sid,))
    with db.tx() as c:
        if not g:
            cur = c.execute("INSERT INTO goals(name,target,deadline,priority,monthly,note,status,created,closed,shared_id,members,shared_state,rev,edited_by) VALUES(?,?,?,?,0,'',?,?,?,?,?,'offered',?,?)",
                            (name, target, deadline, prio, status_, now()[:10], closed, sid, json.dumps(others), rev, p.get("edited_by") or sender))
            gid = cur.lastrowid
        else:
            gid = g["id"]
            newer = rev > (g["rev"] or 0) or (rev == (g["rev"] or 0) and (p.get("edited_by") or sender) > (g["edited_by"] or ""))
            if newer:
                c.execute("UPDATE goals SET name=?,target=?,deadline=?,priority=?,status=?,closed=?,members=?,rev=?,edited_by=? WHERE id=?",
                          (name, target, deadline, prio, status_, closed, json.dumps(others), rev, p.get("edited_by") or sender, gid))
        c.execute("INSERT INTO goal_partners(goal_id,peer,amount) VALUES(?,?,?) ON CONFLICT(goal_id,peer) DO UPDATE SET amount=excluded.amount", (gid, sender, contrib))
        for n in (p.get("notes") or [])[:MAX_NOTES]:
            if not (isinstance(n, dict) and isinstance(n.get("id"), str) and len(n["id"]) <= 64 and n.get("author") in members and isinstance(n.get("ts"), str)):
                continue
            text = "" if n.get("deleted") else str(n.get("text", ""))[:2000]
            c.execute("INSERT INTO goal_notes(id,goal_id,author,ts,text,deleted) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text, deleted=excluded.deleted WHERE excluded.deleted=1 AND goal_notes.author=excluded.author",
                      (n["id"], gid, n["author"], n["ts"][:40], text, 1 if n.get("deleted") else 0))
    return {"ok": True, "goal_id": gid}


def decorate(db, items: list[dict]) -> None:
    """Add the shared side to goals_view items in place: who is in, what each has set aside, the notes, and the team's progress."""
    for v in items:
        if not v.get("shared_id"):
            continue
        partners = [{"peer": r["peer"], "amount": r["amount"]} for r in db.all("SELECT peer, amount FROM goal_partners WHERE goal_id=? ORDER BY peer", (v["id"],))]
        v["members"] = json.loads(v.get("members") or "[]")
        v["partners"] = partners
        v["notes"] = [n for n in _notes(db, v["id"]) if not n["deleted"]]
