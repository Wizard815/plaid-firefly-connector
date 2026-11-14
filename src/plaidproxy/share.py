"""A joint bank account for two people. One person links it with Plaid and asks to share it with the other; the other accepts (or declines);
then the other person's Firefly III gets it too. Either can stop. The admin can also set a share directly.

Asking sends only the account's name, bank and last digits. Nothing else moves until the other person accepts. Then the owner's container sends the
account and its transactions (only what changed since last time) through the gateway, which relays them and keeps nothing. The receiving container
stores them as an account of its own, "shared by <owner>", and sends it to its Firefly III the way the receiver chose when accepting (a new account,
or one they already have). A share the admin set directly waits for the admin's approval there instead, like a bank the receiver linked themselves."""
import hashlib
import json

PREFIX = "shared:"


def _sig(t: dict) -> str:
    return hashlib.sha256(json.dumps([t["date"], round(t["amount"], 2), t["name"], t["category"], t["pending"]]).encode()).hexdigest()[:16]


def set_share(db, account_id: str, peer: str | None, state: str = "active") -> None:
    """Share one of this person's bank accounts with one other person (or stop with peer=None). The admin sets it active at once;
    the person themselves asks (state requested) and it becomes active when the other accepts."""
    a = db.one("SELECT id, source FROM accounts WHERE id=?", (account_id,))
    if not a:
        raise ValueError("no such account")
    if a["id"].startswith(PREFIX):
        raise ValueError("an account that was shared with you cannot be shared on")
    with db.tx() as c:
        old = c.execute("SELECT peer FROM shares WHERE account_id=?", (account_id,)).fetchone()
        c.execute("DELETE FROM shares WHERE account_id=?", (account_id,))
        if peer:
            c.execute("INSERT INTO shares(account_id, peer, state, asked) VALUES(?,?,?,0)", (account_id, peer, state))
        if old and old["peer"] != peer:
            c.execute("INSERT OR REPLACE INTO share_revoke(account_id, peer) VALUES(?,?)", (account_id, old["peer"]))
            c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (account_id, old["peer"]))
        if peer:
            c.execute("DELETE FROM share_revoke WHERE account_id=? AND peer=?", (account_id, peer))


def shared_with(db) -> dict[str, str]:
    return {r["account_id"]: r["peer"] for r in db.all("SELECT account_id, peer FROM shares WHERE coalesce(state,'active')='active'")}


def request_share(db, account_id: str, peer: str) -> None:
    """This person asks to share one of their own bank accounts with another person. Nothing but its name goes until they accept."""
    a = db.one("SELECT id FROM accounts WHERE id=?", (account_id,))
    if not a:
        raise ValueError("no such account")
    cur = db.one("SELECT peer, state FROM shares WHERE account_id=?", (account_id,))
    if cur and cur["peer"] == peer and cur["state"] in ("requested", "active"):
        raise ValueError("already " + ("asked" if cur["state"] == "requested" else "shared with them"))
    set_share(db, account_id, peer, state="requested")


def overview(db) -> dict:
    mine = [{"account_id": r["account_id"], "peer": r["peer"], "state": r["state"] or "active"} for r in db.all("SELECT * FROM shares ORDER BY account_id")]
    incoming = [dict(r) for r in db.all("SELECT * FROM share_in ORDER BY received DESC")]
    return {"mine": mine, "incoming": incoming}


def answer(db, sender: str, account_id: str, kind: str, mode: str | None = None, ff_id: int | None = None) -> None:
    """The person a share was offered to: accept (with how it goes into their Firefly III), decline, or leave one they accepted."""
    r = db.one("SELECT * FROM share_in WHERE sender=? AND account_id=?", (sender, account_id))
    if not r:
        raise KeyError(account_id)
    aid = f"{PREFIX}{sender}:{account_id}"
    if kind == "accept":
        if r["state"] != "asked":
            raise ValueError("already answered")
        if mode not in ("create", "link"):
            raise ValueError("choose create (a new account in your Firefly III) or link (one you have already)")
        if mode == "link" and not ff_id:
            raise ValueError("choose the Firefly III account to link it to")
        with db.tx() as c:
            c.execute("UPDATE share_in SET state='accepted', mode=?, ff_id=? WHERE sender=? AND account_id=?", (mode, ff_id, sender, account_id))
    elif kind in ("decline", "leave"):
        with db.tx() as c:
            c.execute("DELETE FROM share_in WHERE sender=? AND account_id=?", (sender, account_id))
            if db.one("SELECT 1 FROM accounts WHERE id=?", (aid,)):
                c.execute("INSERT INTO ff_accounts(account_id, mode) VALUES(?, 'skip') ON CONFLICT(account_id) DO UPDATE SET mode='skip'", (aid,))
    else:
        raise ValueError("unknown answer")
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO share_out(peer, kind, account_id) VALUES(?,?,?)", (sender, "share-" + kind, account_id))


def run(db, send) -> dict:
    """Send what changed. `send(peer, kind, payload) -> bool` hands one message to the gateway. Anything not delivered is simply tried again next time."""
    out = {"sent": 0, "failed": 0}
    for r in db.all("SELECT * FROM share_out"):                     # my answers to shares offered to me
        try:
            ok = send(r["peer"], r["kind"], {"account_id": r["account_id"]})
        except Exception:
            ok = False
        if ok:
            with db.tx() as c:
                c.execute("DELETE FROM share_out WHERE peer=? AND kind=? AND account_id=?", (r["peer"], r["kind"], r["account_id"]))
        else:
            out["failed"] += 1
    for s in db.all("SELECT account_id, peer FROM shares WHERE state='requested' AND coalesce(asked,0)=0"):      # my questions: the name only
        a = db.one("SELECT id, institution, name, mask, kind FROM accounts WHERE id=?", (s["account_id"],))
        if not a:
            continue
        try:
            ok = send(s["peer"], "share-request", {"account_id": a["id"], "name": a["name"], "institution": a["institution"], "mask": a["mask"], "kind": a["kind"]})
        except Exception:
            ok = False
        if ok:
            with db.tx() as c:
                c.execute("UPDATE shares SET asked=1 WHERE account_id=?", (s["account_id"],))
        else:
            out["failed"] += 1
    for r in db.all("SELECT account_id, peer FROM share_revoke"):
        try:
            ok = send(r["peer"], "share-revoke", {"account_id": r["account_id"]})
        except Exception:
            ok = False
        if ok:
            with db.tx() as c:
                c.execute("DELETE FROM share_revoke WHERE account_id=? AND peer=?", (r["account_id"], r["peer"]))
        else:
            out["failed"] += 1
    for s in db.all("SELECT account_id, peer FROM shares WHERE coalesce(state,'active')='active'"):
        a = db.one("SELECT id, institution, name, mask, kind, balance, currency FROM accounts WHERE id=?", (s["account_id"],))
        if not a:
            continue
        sent = {r["tx_id"]: r["sig"] for r in db.all("SELECT tx_id, sig FROM share_sent WHERE account_id=? AND peer=?", (a["id"], s["peer"]))}
        rows = db.all("SELECT id, date, name, amount, currency, category, pending FROM transactions WHERE account_id=?", (a["id"],))
        now = {t["id"]: _sig(t) for t in rows}
        changed = [t for t in rows if sent.get(t["id"]) != now[t["id"]]]
        gone = [i for i in sent if i not in now]
        payload = {"account": dict(a), "transactions": changed[:500], "removed": gone[:500]}
        try:
            ok = send(s["peer"], "share-data", payload)
        except Exception:
            ok = False
        if not ok:
            out["failed"] += 1
            continue
        with db.tx() as c:
            for t in payload["transactions"]:
                c.execute("INSERT INTO share_sent(account_id, peer, tx_id, sig) VALUES(?,?,?,?) ON CONFLICT(account_id, peer, tx_id) DO UPDATE SET sig=excluded.sig", (a["id"], s["peer"], t["id"], now[t["id"]]))
            for i in payload["removed"]:
                c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=? AND tx_id=?", (a["id"], s["peer"], i))
        out["sent"] += 1
    return out


def receive(db, sender: str, kind: str, payload: dict) -> dict:
    """Store what a person shared with us as an account of our own. It starts as 'needs approval' for this person's Firefly III."""
    if not sender:
        return {"ok": False}
    aid_in = str(payload.get("account", {}).get("id") if kind == "share-data" else payload.get("account_id", ""))[:80]
    if not aid_in:
        return {"ok": False}
    aid = f"{PREFIX}{sender}:{aid_in}"
    if kind == "share-request":                                     # someone asks to share one of their accounts with me
        if db.one("SELECT COUNT(*) n FROM share_in WHERE state='asked'")["n"] >= 20:
            return {"ok": False}
        with db.tx() as c:
            c.execute("""INSERT INTO share_in(sender, account_id, name, institution, mask, kind, state, received) VALUES(?,?,?,?,?,?,'asked',datetime('now'))
                         ON CONFLICT(sender, account_id) DO UPDATE SET name=excluded.name, institution=excluded.institution, mask=excluded.mask, kind=excluded.kind,
                         state=CASE WHEN share_in.state='accepted' THEN 'accepted' ELSE 'asked' END""",
                      (sender, aid_in, str(payload.get("name") or "")[:120], str(payload.get("institution") or "")[:80], str(payload.get("mask") or "")[:8], str(payload.get("kind") or "cash")[:12]))
        return {"ok": True}
    if kind in ("share-accept", "share-decline", "share-leave"):   # the answer to a share I asked for
        s = db.one("SELECT * FROM shares WHERE account_id=? AND peer=?", (aid_in, sender))
        if not s:
            return {"ok": True}
        with db.tx() as c:
            if kind == "share-accept" and s["state"] == "requested":
                c.execute("UPDATE shares SET state='active' WHERE account_id=?", (aid_in,))
                c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (aid_in, sender))
            elif kind != "share-accept":
                c.execute("DELETE FROM shares WHERE account_id=? AND peer=?", (aid_in, sender))
                c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (aid_in, sender))
        return {"ok": True}
    if kind == "share-revoke":
        with db.tx() as c:
            c.execute("DELETE FROM share_in WHERE sender=? AND account_id=?", (sender, aid_in))
            c.execute("INSERT INTO ff_accounts(account_id, mode) VALUES(?, 'skip') ON CONFLICT(account_id) DO UPDATE SET mode='skip', ff_id=NULL", (aid,))
        return {"ok": True}
    if kind != "share-data":
        return {"ok": False}
    a = payload["account"]
    with db.tx() as c:
        chosen = c.execute("SELECT mode, ff_id FROM share_in WHERE sender=? AND account_id=? AND state='accepted'", (sender, aid_in)).fetchone()
        if chosen and not c.execute("SELECT 1 FROM ff_accounts WHERE account_id=? AND mode != 'pending'", (aid,)).fetchone():
            # accepted by this person: it goes into their Firefly III the way they chose, no further approval needed
            c.execute("INSERT INTO ff_accounts(account_id, mode, ff_id) VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET mode=excluded.mode, ff_id=excluded.ff_id",
                      (aid, chosen["mode"], chosen["ff_id"] if chosen["mode"] == "link" else None))
        c.execute("""INSERT INTO accounts(id,item_id,source,institution,name,kind,balance,currency,updated,mask) VALUES(?,?,?,?,?,?,?,?,datetime('now'),?)
                     ON CONFLICT(id) DO UPDATE SET balance=excluded.balance, name=excluded.name, updated=excluded.updated, mask=excluded.mask""",
                  (aid, None, "shared", f"Shared by {sender}" + (f" ({a.get('institution')})" if a.get("institution") else ""), str(a.get("name") or "Joint account")[:120],
                   a.get("kind") if a.get("kind") in ("cash", "credit", "investment", "loan") else "cash", float(a.get("balance") or 0), str(a.get("currency") or "USD")[:3], str(a.get("mask") or "")[:8]))
        for t in payload.get("transactions", [])[:500]:
            c.execute("""INSERT INTO transactions(id,account_id,date,name,amount,currency,category,pending,source) VALUES(?,?,?,?,?,?,?,?,'shared')
                         ON CONFLICT(id) DO UPDATE SET date=excluded.date,name=excluded.name,amount=excluded.amount,category=excluded.category,pending=excluded.pending""",
                      (f"{PREFIX}{sender}:{t['id']}", aid, str(t["date"])[:10], str(t.get("name") or "")[:255], float(t["amount"]), str(t.get("currency") or "USD")[:3], t.get("category"), int(bool(t.get("pending")))))
        for i in payload.get("removed", [])[:500]:
            tid = f"{PREFIX}{sender}:{i}"
            c.execute("UPDATE ff_tx SET state='deleted' WHERE tx_id=? AND ff_id IS NOT NULL", (tid,))            # Firefly III's copy goes at the next push
            c.execute("DELETE FROM ff_tx WHERE tx_id=? AND ff_id IS NULL", (tid,))
            c.execute("DELETE FROM transactions WHERE id=?", (tid,))
    return {"ok": True}
