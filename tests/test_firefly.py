"""Plaid Firefly Connector as a Plaid router for Firefly III: accounts and transactions go across once, correctly, and stay in step."""
from datetime import date, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from plaidproxy import firefly, worker
from plaidproxy.db import DB
from tests.fake_firefly import FakeFirefly

H = {"X-Internal-Secret": "isec"}
TODAY = date.today()
d = lambda n: (TODAY - timedelta(days=n)).isoformat()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PERSON_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def rig(token="good-token"):
    ff = FakeFirefly()
    db = DB(":memory:")
    c = TestClient(worker.create_app(db=db, firefly_transport=ff.transport()))
    c.put("/api/settings", headers=H, json={"firefly_url": "https://firefly.example.com", "firefly_token": token})
    return c, db, ff


def approve(c, modes=None):
    """What the admin does: decide for each bank account. Default: create the everyday and savings accounts and cards, skip the IRA (as the old default did)."""
    modes = modes or {"plaid-chk": "create", "plaid-crd": "create", "plaid-sav": "create", "plaid-ira": "skip"}
    for aid, m in modes.items():
        assert c.put(f"/api/firefly/accounts/{aid}", headers=H, json={"mode": m}).status_code == 200


def bank(db):
    for a in (("chk", "Huntington", "Everyday Checking", "cash", "checking", 1000.00, "4821"), ("crd", "Capital One", "Quicksilver", "credit", "credit card", 250.00, "0182"), ("sav", "Axos", "High Yield", "cash", "savings", 5000.00, "0099"), ("ira", "Fidelity", "IRA", "investment", "ira", 9000.0, "7777")):
        db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,subtype,balance,mask,currency) VALUES(?,?,?,?,?,?,?,?,?)", ("plaid-" + a[0], "plaid") + a[1:] + ("USD",))
    tx = [("t1", "plaid-chk", d(10), "ACME PAYROLL", 2000.00, "Income", 0), ("t2", "plaid-chk", d(8), "KROGER #66", -150.00, "Groceries", 0), ("t3", "plaid-chk", d(2), "PENDING COFFEE", -4.5, "Food", 1),
          ("t4", "plaid-crd", d(6), "AMAZON", -250.00, "Shopping", 0)]
    for t in tx:
        db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES(?,?,?,?,?,?,?,?)", t + ("plaid",))
    db.conn.commit()


def test_connection_test_and_a_clear_message_when_the_token_is_refused():
    c, db, ff = rig()
    r = c.post("/api/firefly/test", headers=H)
    assert r.status_code == 200 and r.json()["version"] == "6.7.9"
    assert "token_set" in c.get("/api/firefly/status", headers=H).json() and "good-token" not in c.get("/api/settings", headers=H).text      # the token is never shown
    c.put("/api/settings", headers=H, json={"firefly_token": "expired-token"})
    r = c.post("/api/firefly/test", headers=H)
    assert r.status_code == 502 and "new Personal Access Token" in r.json()["detail"]
    c2 = TestClient(worker.create_app(db=DB(":memory:")))
    assert c2.post("/api/firefly/test", headers=H).status_code == 409


def test_accounts_are_created_with_the_right_roles_and_balances_and_transactions_arrive_once():
    c, db, ff = rig()
    bank(db)
    maps = {a["id"]: a["mode"] for a in c.get("/api/firefly/accounts", headers=H).json()["accounts"]}
    assert set(maps.values()) == {"pending"}                                                                                       # nothing is sent until the admin approves each account
    assert c.post("/api/firefly/sync", headers=H).json()["created"] == 0 and ff.groups == {} and ff.accounts == {}
    assert c.get("/api/firefly/status", headers=H).json()["awaiting_approval"] == 4
    approve(c)
    assert c.get("/api/firefly/status", headers=H).json()["awaiting_approval"] == 0
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["accounts_created"] == 3 and r["created"] == 3 and r["errors"] == 0                                                 # the pending coffee waits for the bank to post it
    names = {a["name"]: (aid, a) for aid, a in ff.accounts.items()}
    assert set(names) == {"Huntington - Everyday Checking ••4821", "Capital One - Quicksilver ••0182", "Axos - High Yield ••0099"}
    assert names["Capital One - Quicksilver ••0182"][1]["account_role"] == "ccAsset" and names["Axos - High Yield ••0099"][1]["account_role"] == "savingAsset"
    assert names["Huntington - Everyday Checking ••4821"][1]["account_role"] == "defaultAsset"
    # Firefly III's balances equal the bank's: a card's owed amount is a negative balance there
    assert ff.balance(names["Huntington - Everyday Checking ••4821"][0]) == 1000.00 and ff.balance(names["Capital One - Quicksilver ••0182"][0]) == -250.00 and ff.balance(names["Axos - High Yield ••0099"][0]) == 5000.00
    ts = {t["external_id"]: t for g in ff.groups.values() for t in g["transactions"]}
    assert set(ts) == {"t1", "t2", "t4"} and ts["t1"]["type"] == "deposit" and ts["t2"]["type"] == "withdrawal" and ts["t2"]["amount"] == "150.00"
    assert ts["t2"]["destination_name"] == "KROGER #66" and ts["t1"]["source_name"] == "ACME PAYROLL" and "category_name" not in ts["t2"] and ts["t2"]["tags"] == ["goblin-bank"]
    before = len(ff.calls)
    again = c.post("/api/firefly/sync", headers=H).json()
    assert again["created"] == 0 and again["updated"] == 0 and again["accounts_created"] == 0                                    # running it again changes nothing
    assert len([x for x in ff.calls[before:] if x[0] in ("POST", "PUT", "DELETE")]) == 0
    st = c.get("/api/firefly/status", headers=H).json()
    assert st["sent"] == 3 and st["failed"] == 0 and st["waiting"] == 0 and st["last_ok"] and st["host"] == "firefly.example.com"


def test_changes_at_the_bank_update_and_removals_delete_and_a_posted_pending_one_arrives():
    c, db, ff = rig()
    bank(db)
    approve(c)
    c.post("/api/firefly/sync", headers=H)
    db.conn.execute("UPDATE transactions SET amount=-155.25, name='KROGER FUEL' WHERE id='t2'")        # Plaid corrected it
    db.conn.execute("UPDATE transactions SET pending=0 WHERE id='t3'")                                    # the coffee posted
    db.conn.commit()
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["updated"] == 1 and r["created"] == 1
    got = {t["external_id"]: t for g in ff.groups.values() for t in g["transactions"]}
    assert got["t2"]["amount"] == "155.25" and got["t2"]["description"] == "KROGER FUEL" and "t3" in got
    # the bank removes one (as sync.py does: mark Firefly's copy for deletion, then drop the row)
    from plaidproxy import sync
    with db.tx() as cur:
        cur.execute("UPDATE ff_tx SET state='deleted' WHERE tx_id='t4' AND ff_id IS NOT NULL")
        cur.execute("DELETE FROM transactions WHERE id='t4'")
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["deleted"] == 1 and "t4" not in ff.external_ids() and db.one("SELECT 1 FROM ff_tx WHERE tx_id='t4'") is None


def test_link_to_an_existing_firefly_account_skip_and_never_duplicating_after_lost_state():
    c, db, ff = rig()
    bank(db)
    approve(c)
    mine = ff.add_account("My real checking", opening="500.00")
    assert c.put("/api/firefly/accounts/plaid-chk", headers=H, json={"mode": "link"}).status_code == 422               # which one?
    assert c.put("/api/firefly/accounts/plaid-chk", headers=H, json={"mode": "link", "ff_id": mine}).status_code == 200
    c.put("/api/firefly/accounts/plaid-sav", headers=H, json={"mode": "skip"})
    assert c.put("/api/firefly/accounts/nope", headers=H, json={"mode": "skip"}).status_code == 404
    lst = c.get("/api/firefly/accounts", headers=H).json()
    assert [x["name"] for x in lst["firefly_accounts"]] == ["My real checking"]
    c.post("/api/firefly/sync", headers=H)
    assert {a["name"] for a in ff.accounts.values()} == {"My real checking", "Capital One - Quicksilver ••0182"}         # nothing created for the linked or the skipped one
    assert {t["external_id"] for g in ff.groups.values() for t in g["transactions"] if t.get("source_id") == mine or t.get("destination_id") == mine} == {"t1", "t2"}
    # the state is lost (restored from an old backup): the same transactions are found by their id and adopted, not duplicated
    n = len(ff.groups)
    with db.tx() as cur:
        cur.execute("DELETE FROM ff_tx")
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["errors"] == 0 and len(ff.groups) == n and db.one("SELECT COUNT(*) n FROM ff_tx")["n"] == n


def test_transfers_between_your_own_accounts_become_one_transfer_and_options_work():
    c, db, ff = rig()
    bank(db)
    approve(c)
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES('x1','plaid-chk',?,?,-300,'Transfer',0,'plaid')", (d(3), "TRANSFER TO SAVINGS"))
    db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES('x2','plaid-sav',?,?,300,'Transfer',0,'plaid')", (d(2), "TRANSFER FROM CHECKING"))
    db.conn.commit()
    c.put("/api/settings", headers=H, json={"firefly_send_categories": "on", "firefly_include_pending": "on", "firefly_apply_rules": "off"})
    r = c.post("/api/firefly/sync", headers=H).json()
    assert "error" not in r and r["errors"] == 0
    tr = [t for g in ff.groups.values() for t in g["transactions"] if t["type"] == "transfer"]
    assert len(tr) == 1 and tr[0]["amount"] == "300.00" and tr[0]["source_id"] != tr[0]["destination_id"]
    assert {"x1", "x2"} <= {r["tx_id"] for r in db.all("SELECT tx_id FROM ff_tx")} and sum(1 for e in ff.external_ids() if e in ("x1", "x2")) == 1
    assert all(g["apply_rules"] is False for g in ff.groups.values())
    cats = {t["external_id"]: t.get("category_name") for g in ff.groups.values() for t in g["transactions"]}
    assert cats["t2"] == "Groceries" and "t3" in cats                                                                        # categories sent, pending included, as asked
    assert ff.balance(next(i for i, a in ff.accounts.items() if "Axos" in a["name"])) == 5000.00


def test_problems_are_recorded_not_raised_and_a_rejected_token_stops_the_run():
    c, db, ff = rig()
    bank(db)
    approve(c)
    c.put("/api/settings", headers=H, json={"firefly_token": "revoked"})
    r = c.post("/api/firefly/sync", headers=H).json()
    assert "error" in r and "new Personal Access Token" in r["error"]
    st = c.get("/api/firefly/status", headers=H).json()
    assert st["last_error"] and st["last_ok"] is None
    # an ordinary bank sync still works when Firefly III is down
    out = c.post("/sync", headers=H).json()
    assert "plaid" in out and out["firefly"].get("error")
    # one bad transaction does not stop the rest, and retry gives it another go
    c.put("/api/settings", headers=H, json={"firefly_token": "good-token"})
    db.conn.execute("UPDATE transactions SET name='' WHERE id='t2'"); db.conn.commit()
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["created"] >= 2 and r["errors"] == 0                                    # an empty name still becomes "(no description)"
    assert c.post("/api/firefly/retry", headers=H).status_code == 200


def test_existing_transactions_are_adopted_not_duplicated_even_after_they_were_edited_in_firefly_and_balances_can_be_matched():
    c, db, ff = rig()
    bank(db)
    approve(c)
    c.post("/api/firefly/sync", headers=H)
    n = len(ff.groups)
    gid = next(g for g, v in ff.groups.items() if v["transactions"][0]["external_id"] == "t2")
    ff.groups[gid]["transactions"][0].update({"amount": "155.25", "description": "KROGER FUEL (edited in Firefly)"})          # the hash Firefly kept no longer matches
    with db.tx() as cur:
        cur.execute("DELETE FROM ff_tx"); cur.execute("UPDATE ff_accounts SET adopted=0")                                     # state lost
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["created"] == 0 and r["errors"] == 0 and len(ff.groups) == n
    assert db.one("SELECT ff_id FROM ff_tx WHERE tx_id='t2'")["ff_id"] == gid
    # drift: the bank says 1000.00 but Firefly III now disagrees; one click moves the opening balance, no fake transaction
    aid = next(i for i, a in ff.accounts.items() if "Huntington" in a["name"])
    assert ff.balance(aid) != 1000.00
    r = c.post("/api/firefly/accounts/plaid-chk/rebalance", headers=H).json()
    assert r["changed"] is True and ff.balance(aid) == 1000.00 and len(ff.groups) == n
    assert c.post("/api/firefly/accounts/plaid-chk/rebalance", headers=H).json() == {"changed": False, "difference": 0.0}
    assert c.post("/api/firefly/accounts/plaid-ira/rebalance", headers=H).status_code == 404                                    # never created: nothing to match


def test_linking_to_an_account_that_already_has_history_adopts_what_matches_and_leaves_the_rest():
    c, db, ff = rig()
    bank(db)
    approve(c)
    mine = ff.add_account("My real checking", opening="0")
    ff.groups[900] = {"transactions": [{"type": "withdrawal", "date": d(8), "amount": "150.00", "description": "KROGER #66", "source_id": mine, "destination_name": "KROGER", "external_id": "t2"}]}      # already imported by something else
    ff.groups[901] = {"transactions": [{"type": "withdrawal", "date": d(30), "amount": "9.00", "description": "my own entry", "source_id": mine, "destination_name": "x", "external_id": "manual-1"}]}
    c.put("/api/firefly/accounts/plaid-chk", headers=H, json={"mode": "link", "ff_id": mine})
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["errors"] == 0 and "t2" in {x["tx_id"] for x in db.all("SELECT tx_id FROM ff_tx")}
    ids = ff.external_ids()
    assert ids.count("t2") == 1 and "manual-1" in ids and "t1" in ids                                                           # t2 not doubled, your own entry untouched, the new one added


def test_the_gateway_admin_sets_each_persons_firefly_connection_and_the_token_never_comes_back(tmp_path, monkeypatch):
    import httpx
    from plaidproxy import gateway
    from plaidproxy.security import sha256
    monkeypatch.setenv("GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("GATEWAY_STATE", str(tmp_path / "s.json"))
    holder, ff = {}, FakeFirefly()

    def to_gateway(req: httpx.Request):
        r = holder["gw"].request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    db = DB(":memory:")
    g = TestClient(worker.create_app(db=db, transport=httpx.MockTransport(to_gateway), firefly_transport=ff.transport()))

    def to_worker(req: httpx.Request):
        r = g.request(req.method, req.url.raw_path.decode(), content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers["content-type"]})
    cfg = {"people": [{"name": "alice", "url": "http://alice", "internal_secret": "isec", "user_token_sha256": sha256("t")}], "orb_keys": [], "admin_token_sha256": sha256("adm")}
    gw = TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(to_worker)))
    holder["gw"] = gw
    A = {"Authorization": "Bearer adm"}
    assert gw.put("/admin/policy", headers=A, json={"default": {"firefly_token": "x" * 30}}).status_code == 422          # a token is one person's
    assert gw.put("/admin/policy", headers=A, json={"people": {"alice": {"firefly_url": "ftp://nope"}}}).status_code == 422
    r = gw.put("/admin/policy", headers=A, json={"people": {"alice": {"firefly_url": "https://firefly.example.com", "firefly_token": "good-token" + "x" * 12}}})
    assert r.status_code == 200 and "good-tokenxxxx" not in r.text and r.json()["firefly_token_set"] == {"alice": True}
    assert "good-tokenxxxx" not in gw.get("/admin/policy", headers=A).text
    gw.put("/admin/policy", headers=A, json={"default": {"plaid_budget_month": 3}})                                          # other changes keep the token
    assert gw.get("/admin/policy", headers=A).json()["firefly_token_set"] == {"alice": True}
    s = g.get("/api/settings", headers=H).json()
    assert s["firefly_url"] == "https://firefly.example.com" and s["firefly_token"] == {"set": True} and {"firefly_url", "firefly_token"} <= set(s["managed"])
    assert g.put("/api/settings", headers=H, json={"firefly_url": "https://elsewhere.example.com"}).status_code == 409        # not changeable by the person


def test_a_long_history_goes_across_in_one_run_in_batches(monkeypatch):
    monkeypatch.setattr(firefly, "RUN_LIMIT", 2)
    c, db, ff = rig()
    bank(db)
    approve(c)
    for i in range(7):
        db.conn.execute("INSERT INTO transactions(id,account_id,date,name,amount,category,pending,source) VALUES(?,?,?,?,?,?,?,?)", (f"h{i}", "plaid-chk", d(20 + i), f"OLD {i}", -1.0 * (i + 1), "Misc", 0, "plaid"))
    db.conn.commit()
    r = c.post("/api/firefly/sync", headers=H).json()
    assert r["remaining"] == 0 and r["created"] == 10 and r["errors"] == 0 and len(ff.groups) == 10
    low = c.post("/api/firefly/sync", headers=H).json()
    assert low["created"] == 0
