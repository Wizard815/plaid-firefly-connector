"""Mattermost notifications and the 'matured but still sitting there' alert."""
import json
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import goblin, notify
from goblinbank.db import DB

TODAY = date(2026, 11, 7)
H = {"X-Internal-Secret": "isec"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def rig(status=200, **kw):
    sent = []

    def h(req: httpx.Request):
        sent.append({"url": str(req.url), "body": json.loads(req.content)})
        return httpx.Response(status, text="ok")
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, notify_transport=httpx.MockTransport(h), **kw))
    return c, db, sent


def add_cd(db, name="12-mo CD", balance=10000, maturity="2026-10-20", apy=5.0):
    cur = db.conn.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES(?,?,?,?,?)", (name, "cd", balance, apy, maturity)); db.conn.commit()
    return cur.lastrowid


def overdrawn(db, i=1, bal=-20):
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES(?,?,?,?,?)", (f"neg{i}", "manual", f"Checking {i}", "cash", bal)); db.conn.commit()


HOOK = "https://mm.example/hooks/abc123"


# ---------------------------------------------------------------- matured but idle
def test_matured_idle_alert_says_where_the_money_could_go():
    c, db, _ = rig()
    cd = add_cd(db, maturity="2026-10-20")                                              # matured 18 days ago
    car = c.post("/api/goals", headers=H, json={"name": "Car", "target": 3000, "priority": 1}).json()
    house = c.post("/api/goals", headers=H, json={"name": "House", "target": 9000, "priority": 2}).json()
    c.post(f"/api/goals/{house['id']}/allocate", headers=H, json={"place": f"s:{cd}", "amount": 7000})
    a = next(x for x in c.get("/api/alerts", headers=H).json() if x["kind"] == "matured_idle")
    assert a["severity"] == "warning" and a["date"] == "2026-10-20" and "matured 18 days ago and is still sitting there" in a["title"]
    assert "Already pinned: House 7,000.00" in a["detail"] and "Car 3,000.00" in a["detail"] and "mark it handled" in a["detail"]
    assert a["key"] == f"matured_idle:s:{cd}:2026-10-20"


def test_matured_idle_window_and_edge_cases():
    c, db, _ = rig()
    kinds = lambda: [x["kind"] for x in c.get("/api/alerts", headers=H).json()]
    add_cd(db, "future", maturity="2026-12-01"); assert "matured_idle" not in kinds()                         # not matured yet
    add_cd(db, "today", maturity="2026-11-07"); assert kinds().count("matured_idle") == 1                      # matures today counts as matured
    add_cd(db, "ancient", maturity="2026-08-01"); assert kinds().count("matured_idle") == 1                    # 98 days ago: long past, stop nagging
    add_cd(db, "empty", balance=0, maturity="2026-10-25"); assert kinds().count("matured_idle") == 1           # nothing left on it
    db.conn.execute("UPDATE savings SET active=0 WHERE name='today'"); db.conn.commit()
    assert "matured_idle" not in kinds()                                                                       # removed from the list = handled


def test_handled_alerts_stay_quiet_but_a_new_situation_is_new():
    c, db, _ = rig()
    cd = add_cd(db, maturity="2026-10-20")
    a = next(x for x in c.get("/api/alerts", headers=H).json() if x["kind"] == "matured_idle")
    assert c.post("/api/alerts/dismiss", headers=H, json={"key": a["key"]}).status_code == 200
    assert "matured_idle" not in [x["kind"] for x in c.get("/api/alerts", headers=H).json()]
    assert c.post("/api/alerts/dismiss", headers=H, json={"key": "nope"}).status_code == 404
    db.conn.execute("UPDATE savings SET maturity='2026-11-01' WHERE id=?", (cd,)); db.conn.commit()           # rolled over into a new term that has also ended
    assert "matured_idle" in [x["kind"] for x in c.get("/api/alerts", headers=H).json()]                      # different maturity date, different key


def test_idle_maturity_reaches_the_orb_with_its_date():
    c, db, _ = rig()
    add_cd(db, maturity="2026-10-20")
    c.put("/api/settings", headers=H, json={"share": ["alert_list"]})
    m = next(a for a in c.get("/summary", headers=H).json()["alert_list"] if a["kind"] == "matured_idle")
    assert m["date"] == "2026-10-20" and m["severity"] == "warning" and set(m) == {"severity", "kind", "title", "detail", "date"}


# ---------------------------------------------------------------- notifications
def test_nothing_is_sent_without_a_webhook_and_the_secret_is_protected():
    c, db, sent = rig()
    overdrawn(db)
    assert c.post("/api/notify/run", headers=H).json() == {"configured": False, "sent": 0} and sent == []
    r = c.put("/api/settings", headers=H, json={"notify_url": HOOK}).json()
    assert r["notify_url"] == {"set": True} and HOOK not in json.dumps(r) and HOOK not in db.one("SELECT value FROM settings WHERE key='notify_url'")["value"]
    assert c.put("/api/settings", headers=H, json={"notify_url": "ftp://x"}).status_code == 422
    assert c.put("/api/settings", headers=H, json={"notify_min_severity": "loud"}).status_code == 422


def test_new_alerts_are_sent_once_in_mattermost_format():
    c, db, sent = rig()
    c.put("/api/settings", headers=H, json={"notify_url": HOOK})
    overdrawn(db)
    r = c.post("/api/notify/run", headers=H).json()
    assert r["sent"] == 1 and r["titles"] == ["Checking 1 is overdrawn"] and len(sent) == 1 and sent[0]["url"] == HOOK
    b = sent[0]["body"]
    assert b["username"] == "Goblin Bank (alice)" and b["icon_emoji"] == ":coin:" and ":rotating_light: **Checking 1 is overdrawn**" in b["text"] and "Balance -20.00" in b["text"]
    assert c.post("/api/notify/run", headers=H).json()["sent"] == 0 and len(sent) == 1                       # the same alert is not repeated
    overdrawn(db, 2, -5); overdrawn(db, 3, -9)
    r = c.post("/api/notify/run", headers=H).json()
    assert r["sent"] == 2 and len(sent) == 2 and "2 things need a look" in sent[1]["body"]["text"]            # new ones are batched into one message
    st = c.get("/api/notify/status", headers=H).json()
    assert st["configured"] and st["tracked"] == 3 and st["min_severity"] == "warning" and st["last_sent"]


def test_severity_floor_decides_what_reaches_your_phone():
    c, db, sent = rig()
    c.put("/api/settings", headers=H, json={"notify_url": HOOK})
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance,min_balance) VALUES('low','manual','Low','cash',80,200)")
    db.conn.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,created) VALUES('Soon',9,'monthly',1,'month',?,'x')", ((TODAY + timedelta(days=1)).isoformat(),)); db.conn.commit()
    r = c.post("/api/notify/run", headers=H).json()                                                           # default floor: warning
    assert r["titles"] == ["Low is below your minimum"]                                                       # the info-level "bill due soon" stays in the app
    c.put("/api/settings", headers=H, json={"notify_min_severity": "info"})
    assert "Soon due" in c.post("/api/notify/run", headers=H).json()["titles"][0]
    c.put("/api/settings", headers=H, json={"notify_min_severity": "critical"})
    overdrawn(db)
    assert c.post("/api/notify/run", headers=H).json()["titles"] == ["Checking 1 is overdrawn"]
    assert [a["severity"] for a in c.get("/api/alerts", headers=H).json()][0] == "critical"


def test_critical_repeats_after_a_day_warnings_do_not_and_returning_alerts_are_announced_again():
    c, db, sent = rig()
    c.put("/api/settings", headers=H, json={"notify_url": HOOK})
    overdrawn(db)
    db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance,min_balance) VALUES('low','manual','Low','cash',80,200)"); db.conn.commit()
    t0 = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
    assert notify.run(db, TODAY, "alice", httpx.MockTransport(lambda r: sent.append(1) or httpx.Response(200)), now=t0)["sent"] == 2
    ok = httpx.MockTransport(lambda r: sent.append(json.loads(r.content)) or httpx.Response(200))
    assert notify.run(db, TODAY, "alice", ok, now=t0 + timedelta(hours=23))["sent"] == 0
    r = notify.run(db, TODAY, "alice", ok, now=t0 + timedelta(hours=25))
    assert r["sent"] == 1 and r["titles"] == ["Checking 1 is overdrawn"]                                      # only the critical one repeats
    db.conn.execute("UPDATE accounts SET balance=500 WHERE id='neg1'"); db.conn.commit()                       # the overdraft clears...
    assert notify.run(db, TODAY, "alice", ok, now=t0 + timedelta(hours=26))["sent"] == 0
    assert db.one("SELECT COUNT(*) n FROM notified WHERE key LIKE 'negative_balance%'")["n"] == 0
    db.conn.execute("UPDATE accounts SET balance=-3 WHERE id='neg1'"); db.conn.commit()                         # ...and comes back
    assert notify.run(db, TODAY, "alice", ok, now=t0 + timedelta(hours=27))["sent"] == 1


def test_a_failed_send_is_retried_not_lost():
    bad, db, sent = rig(status=500)
    bad.put("/api/settings", headers=H, json={"notify_url": HOOK})
    overdrawn(db)
    r = bad.post("/api/notify/run", headers=H).json()
    assert r["sent"] == 0 and "500" in r["error"] and db.one("SELECT COUNT(*) n FROM notified")["n"] == 0        # not marked as sent
    ok = httpx.MockTransport(lambda req: httpx.Response(200))
    assert notify.run(db, TODAY, "alice", ok)["sent"] == 1                                                      # the next check delivers it
    def down(req): raise httpx.ConnectError("no route")
    db.conn.execute("DELETE FROM notified"); db.conn.commit()
    assert "could not reach" in notify.run(db, TODAY, "alice", httpx.MockTransport(down))["error"]


def test_test_button_and_dismissed_alerts_are_not_sent():
    c, db, sent = rig()
    assert c.post("/api/notify/test", headers=H).status_code == 503                                             # no address yet
    c.put("/api/settings", headers=H, json={"notify_url": HOOK})
    assert c.post("/api/notify/test", headers=H).json() == {"ok": True} and "notifications are working" in sent[0]["body"]["text"]
    assert db.one("SELECT COUNT(*) n FROM notified")["n"] == 0                                                   # a test message does not use up a real alert
    cd = add_cd(db, maturity="2026-10-20")
    key = next(a["key"] for a in c.get("/api/alerts", headers=H).json() if a["kind"] == "matured_idle")
    c.post("/api/alerts/dismiss", headers=H, json={"key": key})
    n = len(sent)
    assert c.post("/api/notify/run", headers=H).json()["sent"] == 0 and len(sent) == n                           # handled means silent
    bad, _, _ = rig(status=404)
    bad.put("/api/settings", headers=H, json={"notify_url": HOOK})
    assert "is the address right" in bad.post("/api/notify/test", headers=H).json()["detail"]


def test_message_is_capped_and_never_contains_secrets():
    db = DB(":memory:")
    for i in range(20):
        db.conn.execute("INSERT INTO accounts(id,source,name,kind,balance) VALUES(?,?,?,?,?)", (f"n{i}", "manual", f"Acct {i}", "cash", -1.0))
    db.conn.commit()
    from goblinbank import ledger
    body = notify.payload(ledger.alerts(db, TODAY), "alice")
    assert body["text"].count("**") // 2 == notify.MAX_PER_MESSAGE and "and 5 more in the app" in body["text"] and "20 things" in body["text"]


def test_background_loop_notifies_on_its_own(monkeypatch):
    sent = []
    t = httpx.MockTransport(lambda r: sent.append(1) or httpx.Response(200))
    db = DB(":memory:")
    c = TestClient(goblin.create_app(db=db, today=lambda: TODAY, notify_transport=t, autosync=True, autosync_tick=0.05))
    c.put("/api/settings", headers=H, json={"notify_url": HOOK, "sync_minutes": "0"})                           # data sync off, alerts still watched
    overdrawn(db)
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        time.sleep(0.05)
    assert len(sent) == 1
    time.sleep(0.3)
    assert len(sent) == 1                                                                                       # and it does not repeat itself
