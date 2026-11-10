"""A joint bank account for two people. One person links it with Plaid; the admin shares it with the other person; the other person's Firefly III gets it too.

The owner's container sends the account and its transactions (only what changed since last time) through the gateway, which relays them and keeps nothing. The
receiving container stores them as an account of its own, "shared by <owner>". It is NOT sent to the receiver's Firefly III until the admin approves it there
(create / link / skip), exactly like a bank the receiver linked themselves."""
import hashlib
import json

PREFIX = "shared:"


def _sig(t: dict) -> str:
    return hashlib.sha256(json.dumps([t["date"], round(t["amount"], 2), t["name"], t["category"], t["pending"]]).encode()).hexdigest()[:16]


def set_share(db, account_id: str, peer: str | None) -> None:
    """The admin shares one of this person's bank accounts with one other person (or stops with peer=None)."""
    a = db.one("SELECT id, source FROM accounts WHERE id=?", (account_id,))
    if not a:
        raise ValueError("no such account")
    if a["id"].startswith(PREFIX):
        raise ValueError("an account that was shared with you cannot be shared on")
    with db.tx() as c:
        old = c.execute("SELECT peer FROM shares WHERE account_id=?", (account_id,)).fetchone()
        c.execute("DELETE FROM shares WHERE account_id=?", (account_id,))
        if peer:
            c.execute("INSERT INTO shares(account_id, peer) VALUES(?,?)", (account_id, peer))
        if old and old["peer"] != peer:
            c.execute("INSERT OR REPLACE INTO share_revoke(account_id, peer) VALUES(?,?)", (account_id, old["peer"]))
            c.execute("DELETE FROM share_sent WHERE account_id=? AND peer=?", (account_id, old["peer"]))
        if peer:
            c.execute("DELETE FROM share_revoke WHERE account_id=? AND peer=?", (account_id, peer))


def shared_with(db) -> dict[str, str]:
    return {r["account_id"]: r["peer"] for r in db.all("SELECT account_id, peer FROM shares")}


def run(db, send) -> dict:
    """Send what changed. `send(peer, kind, payload) -> bool` hands one message to the gateway. Anything not delivered is simply tried again next time."""
    out = {"sent": 0, "failed": 0}
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
    for s in db.all("SELECT account_id, peer FROM shares"):
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
    aid_in = str(payload.get("account", {}).get("id") if kind == "share-data" else payload.get("account_id", ""))
    if not aid_in:
        return {"ok": False}
    aid = f"{PREFIX}{sender}:{aid_in}"
    if kind == "share-revoke":
        with db.tx() as c:
            c.execute("INSERT INTO ff_accounts(account_id, mode) VALUES(?, 'skip') ON CONFLICT(account_id) DO UPDATE SET mode='skip', ff_id=NULL", (aid,))
        return {"ok": True}
    if kind != "share-data":
        return {"ok": False}
    a = payload["account"]
    with db.tx() as c:
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
