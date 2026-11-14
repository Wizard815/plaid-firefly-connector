"""A joint bank account for two people. One person links it with Plaid and asks to share it with the other; the other accepts (or declines);
then the other person's Firefly III gets it too. Either can stop. The admin can also set a share directly.

Asking sends only the account's name, bank and last digits. Nothing else moves until the other person accepts. Then the owner's container sends the
account and its transactions (only what changed since last time) through the gateway, which relays them and keeps nothing. The receiving container
stores them as an account of its own, "shared by <owner>", and sends it to its Firefly III the way the receiver chose when accepting (a new account,
or one they already have). A share the admin set directly waits for the admin's approval there instead, like a bank the receiver linked themselves."""
import hashlib
import json
import threading

PREFIX = "shared:"


def _sig(t: dict) -> str:
    return hashlib.sha256(json.dumps([t["date"], round(t["amount"], 2), t["name"], t["category"], t["pending"]]).encode()).hexdigest()[:16]


MAX_ASKS_PER_PERSON = 20
_locks: dict[int, threading.Lock] = {}


def _lock(db) -> threading.Lock:
    return _locks.setdefault(id(db), threading.Lock())


def set_share(db, account_id: str, peer: str | None, state: str = "active") -> None:
    """Share one of this person's bank accounts with one other person (or stop with peer=None). The admin sets it active at once;
    the person themselves asks (state requested) and it becomes active when the other accepts. Whoever had it before is always told it stopped,
    even when a new question follows straight away (the stop goes out first)."""
    a = db.one("SELECT id, source FROM accounts WHERE id=?", (account_id,))
    if not a and peer:
        raise ValueError("no such account")
    if account_id.startswith(PREFIX):
        raise ValueError("an account that was shared with you cannot be shared on")
    with db.tx() as c:
        old = c.execute("SELECT peer, state FROM shares WHERE account_id=?", (account_id,)).fetchone()
        c.execute("DELETE FROM shares WHERE account_id=?", (account_id,))
        if peer:
            c.execute("INSERT INTO shares(account_id, peer, state, asked) VALUES(?,?,?,0)", (account_id, peer, state))
        if old and (old["peer"] != peer or old["state"] != state or not peer):
            c.execute("INSERT OR REPLACE INTO share_revoke(account_id, peer) VALUES(?,?)", (account_id, old["peer"]))
            c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (account_id, old["peer"]))


def shared_with(db) -> dict[str, str]:
    return {r["account_id"]: r["peer"] for r in db.all("SELECT account_id, peer FROM shares WHERE coalesce(state,'active')='active'")}


def request_share(db, account_id: str, peer: str) -> None:
    """This person asks to share one of their own bank accounts with another person. Nothing but its name goes until they accept."""
    if not db.one("SELECT id FROM accounts WHERE id=?", (account_id,)):
        raise ValueError("no such account")
    cur = db.one("SELECT peer, state FROM shares WHERE account_id=?", (account_id,))
    if cur and cur["peer"] == peer and cur["state"] in ("requested", "active"):
        raise ValueError("already " + ("asked" if cur["state"] == "requested" else "shared with them"))
    set_share(db, account_id, peer, state="requested")


def overview(db) -> dict:
    mine = [{"account_id": r["account_id"], "peer": r["peer"], "state": r["state"] or "active"} for r in db.all("SELECT * FROM shares ORDER BY account_id")]
    incoming = [{k: r[k] for k in ("sender", "account_id", "name", "institution", "mask", "kind", "state", "mode", "received")} for r in db.all("SELECT * FROM share_in ORDER BY received DESC")]
    admin_set = [{"sender": r["id"].split(":")[1], "account_id": r["id"].split(":", 2)[2], "name": r["name"], "institution": r["institution"], "state": "set by the admin"}
                 for r in db.all("SELECT * FROM accounts WHERE source='shared'") if not any(i["sender"] == r["id"].split(":")[1] and i["account_id"] == r["id"].split(":", 2)[2] for i in incoming)
                 and (db.one("SELECT mode FROM ff_accounts WHERE account_id=?", (r["id"],)) or {}).get("mode") != "skip"]
    return {"mine": mine, "incoming": incoming + admin_set}


def ff_in_use(db, ff_id: int, except_account: str) -> str | None:
    """The bank account that already goes into this Firefly III account, if any (one Firefly III account takes one bank account)."""
    r = db.one("SELECT a.institution, a.name FROM ff_accounts m JOIN accounts a ON a.id=m.account_id WHERE m.ff_id=? AND m.account_id!=? AND m.mode IN ('create','link')", (int(ff_id), except_account))
    return f"{r['institution']} - {r['name']}" if r else None


def _skip(c, aid: str) -> None:
    if c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():          # only an account that is really here (no orphan rows)
        c.execute("INSERT INTO ff_accounts(account_id, mode) VALUES(?, 'skip') ON CONFLICT(account_id) DO UPDATE SET mode='skip'", (aid,))


def _apply_choice(c, aid: str, mode: str, ff_id: int | None) -> None:
    c.execute("INSERT INTO ff_accounts(account_id, mode, ff_id) VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET mode=excluded.mode, ff_id=excluded.ff_id, created=0, adopted=0",
              (aid, mode, ff_id if mode == "link" else None))


def answer(db, sender: str, account_id: str, kind: str, mode: str | None = None, ff_id: int | None = None) -> None:
    """The person a share was offered to: accept (with how it goes into their Firefly III), decline, or leave one they accepted (or one the admin set)."""
    r = db.one("SELECT * FROM share_in WHERE sender=? AND account_id=?", (sender, account_id))
    aid = f"{PREFIX}{sender}:{account_id}"
    here = db.one("SELECT 1 FROM accounts WHERE id=?", (aid,))
    if not r and not (kind == "leave" and here):
        raise KeyError(account_id)
    if kind == "accept":
        if r["state"] != "asked":
            raise ValueError("already answered")
        if mode not in ("create", "link"):
            raise ValueError("choose create (a new account in your Firefly III) or link (one you have already)")
        if mode == "link":
            if not ff_id:
                raise ValueError("choose the Firefly III account to link it to")
            used = ff_in_use(db, ff_id, aid)
            if used:
                raise ValueError(f"that Firefly III account already receives {used}; one Firefly III account takes one bank account")
        with db.tx() as c:
            c.execute("UPDATE share_in SET state='accepted', mode=?, ff_id=?, applied=0 WHERE sender=? AND account_id=?", (mode, ff_id if mode == "link" else None, sender, account_id))
            if here:                                  # accepted again after leaving: its copy is already here, so the choice applies now
                _apply_choice(c, aid, mode, ff_id)
                c.execute("UPDATE share_in SET applied=1 WHERE sender=? AND account_id=?", (sender, account_id))
    elif kind in ("decline", "leave"):
        with db.tx() as c:
            c.execute("DELETE FROM share_in WHERE sender=? AND account_id=?", (sender, account_id))
            _skip(c, aid)
    else:
        raise ValueError("unknown answer")
    with db.tx() as c:                                # the latest answer for this account is the one that goes
        c.execute("INSERT INTO share_answer(peer, account_id, kind) VALUES(?,?,?) ON CONFLICT(peer, account_id) DO UPDATE SET kind=excluded.kind", (sender, account_id, "share-" + kind))


def _forget_peer(db, peer: str) -> None:
    """The other person is no longer on this server: drop everything shared with them or by them."""
    with db.tx() as c:
        for r in c.execute("SELECT account_id FROM share_in WHERE sender=?", (peer,)).fetchall():
            _skip(c, f"{PREFIX}{peer}:{r[0]}")
        for tbl, col in (("shares", "peer"), ("share_sent", "peer"), ("share_revoke", "peer"), ("share_answer", "peer"), ("share_in", "sender")):
            c.execute(f"DELETE FROM {tbl} WHERE {col}=?", (peer,))


def run(db, send) -> dict:
    """Send what changed, in order: my answers, stops, questions, then data for active shares. `send(peer, kind, payload)` returns True when the
    gateway delivered it, "gone" when that person is no longer on the server, else False (tried again next time). One run at a time."""
    out = {"sent": 0, "failed": 0}
    with _lock(db):
        def deliver(peer, kind, payload) -> bool:
            try:
                ok = send(peer, kind, payload)
            except Exception:
                ok = False
            if ok == "gone":
                _forget_peer(db, peer)
                return False
            if not ok:
                out["failed"] += 1
            return bool(ok)
        for r in db.all("SELECT * FROM share_answer"):                    # my answers to shares offered to me
            if deliver(r["peer"], r["kind"], {"account_id": r["account_id"]}):
                with db.tx() as c:
                    c.execute("DELETE FROM share_answer WHERE peer=? AND account_id=? AND kind=?", (r["peer"], r["account_id"], r["kind"]))
        for r in db.all("SELECT account_id, peer FROM share_revoke"):     # stops go before any new question
            if deliver(r["peer"], "share-revoke", {"account_id": r["account_id"]}):
                with db.tx() as c:
                    c.execute("DELETE FROM share_revoke WHERE account_id=? AND peer=?", (r["account_id"], r["peer"]))
        for s in db.all("SELECT account_id, peer FROM shares"):          # an account that is gone (bank unlinked, deleted) stops being shared
            if not db.one("SELECT 1 FROM accounts WHERE id=?", (s["account_id"],)):
                set_share(db, s["account_id"], None)
                if deliver(s["peer"], "share-revoke", {"account_id": s["account_id"]}):
                    with db.tx() as c:
                        c.execute("DELETE FROM share_revoke WHERE account_id=? AND peer=?", (s["account_id"], s["peer"]))
        for s in db.all("SELECT account_id, peer FROM shares WHERE state='requested' AND coalesce(asked,0)=0"):      # my questions: the name only
            a = db.one("SELECT id, institution, name, mask, kind FROM accounts WHERE id=?", (s["account_id"],))
            if a and deliver(s["peer"], "share-request", {"account_id": a["id"], "name": a["name"], "institution": a["institution"], "mask": a["mask"], "kind": a["kind"]}):
                with db.tx() as c:
                    c.execute("UPDATE shares SET asked=1 WHERE account_id=? AND peer=?", (s["account_id"], s["peer"]))
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
            if not db.one("SELECT 1 FROM shares WHERE account_id=? AND peer=? AND coalesce(state,'active')='active'", (a["id"], s["peer"])):
                continue                                                    # stopped while this run was reading
            if not deliver(s["peer"], "share-data", payload):
                continue
            with db.tx() as c:
                if not c.execute("SELECT 1 FROM shares WHERE account_id=? AND peer=? AND coalesce(state,'active')='active'", (a["id"], s["peer"])).fetchone():
                    continue                                                # stopped meanwhile: do not record it as sent
                for t in payload["transactions"]:
                    c.execute("INSERT INTO share_sent(account_id, peer, tx_id, sig) VALUES(?,?,?,?) ON CONFLICT(account_id, peer, tx_id) DO UPDATE SET sig=excluded.sig", (a["id"], s["peer"], t["id"], now[t["id"]]))
                for i in payload["removed"]:
                    c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=? AND tx_id=?", (a["id"], s["peer"], i))
            out["sent"] += 1
    return out


def receive(db, sender: str, kind: str, payload: dict) -> dict:
    """A message from another person's container (the gateway checked who sent it)."""
    if not sender:
        return {"ok": False}
    aid_in = str(payload.get("account", {}).get("id") if kind == "share-data" else payload.get("account_id", ""))[:80]
    if not aid_in:
        return {"ok": False}
    aid = f"{PREFIX}{sender}:{aid_in}"
    if kind == "share-request":                                     # someone asks to share one of their accounts with me: a fresh question every time
        if db.one("SELECT COUNT(*) n FROM share_in WHERE sender=? AND state='asked' AND account_id!=?", (sender, aid_in))["n"] >= MAX_ASKS_PER_PERSON:
            return {"ok": False}
        with db.tx() as c:
            c.execute("""INSERT INTO share_in(sender, account_id, name, institution, mask, kind, state, mode, ff_id, applied, received) VALUES(?,?,?,?,?,?,'asked',NULL,NULL,0,datetime('now'))
                         ON CONFLICT(sender, account_id) DO UPDATE SET name=excluded.name, institution=excluded.institution, mask=excluded.mask, kind=excluded.kind,
                         state='asked', mode=NULL, ff_id=NULL, applied=0, received=excluded.received""",
                      (sender, aid_in, str(payload.get("name") or "")[:120], str(payload.get("institution") or "")[:80], str(payload.get("mask") or "")[:8], str(payload.get("kind") or "cash")[:12]))
        return {"ok": True}
    if kind in ("share-accept", "share-decline", "share-leave"):   # the answer to a share I asked for
        s = db.one("SELECT * FROM shares WHERE account_id=? AND peer=?", (aid_in, sender))
        if not s:
            return {"ok": True}
        with db.tx() as c:
            if kind == "share-accept" and s["state"] == "requested":
                c.execute("UPDATE shares SET state='active' WHERE account_id=? AND peer=?", (aid_in, sender))
                c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (aid_in, sender))
            elif kind != "share-accept":
                c.execute("DELETE FROM shares WHERE account_id=? AND peer=?", (aid_in, sender))
                c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (aid_in, sender))
        return {"ok": True}
    if kind == "share-revoke":
        with db.tx() as c:
            c.execute("DELETE FROM share_in WHERE sender=? AND account_id=?", (sender, aid_in))
            _skip(c, aid)
        return {"ok": True}
    if kind != "share-data":
        return {"ok": False}
    a = payload["account"]
    with db.tx() as c:
        c.execute("""INSERT INTO accounts(id,item_id,source,institution,name,kind,balance,currency,updated,mask) VALUES(?,?,?,?,?,?,?,?,datetime('now'),?)
                     ON CONFLICT(id) DO UPDATE SET balance=excluded.balance, name=excluded.name, updated=excluded.updated, mask=excluded.mask""",
                  (aid, None, "shared", f"Shared by {sender}" + (f" ({a.get('institution')})" if a.get("institution") else ""), str(a.get("name") or "Joint account")[:120],
                   a.get("kind") if a.get("kind") in ("cash", "credit", "investment", "loan") else "cash", float(a.get("balance") or 0), str(a.get("currency") or "USD")[:3], str(a.get("mask") or "")[:8]))
        chosen = c.execute("SELECT mode, ff_id FROM share_in WHERE sender=? AND account_id=? AND state='accepted' AND coalesce(applied,0)=0", (sender, aid_in)).fetchone()
        if chosen:                                # accepted by this person: it goes into their Firefly III the way they chose (once; the admin can change it later)
            _apply_choice(c, aid, chosen["mode"], chosen["ff_id"])
            c.execute("UPDATE share_in SET applied=1 WHERE sender=? AND account_id=?", (sender, aid_in))
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
