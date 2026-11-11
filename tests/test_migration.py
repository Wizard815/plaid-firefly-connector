"""Moving an old Goblin Bank folder into a plaid-proxy folder: every login, key, link and row arrives, and the old folder is never changed."""
import hashlib
import importlib.util
import json
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from plaidproxy.security import hash_password, sha256, verify_password

from test_passwords import load_gen

ROOT = Path(__file__).resolve().parents[1]
WIZ_OLD_PW, WIZ_NEW_PW, BOB_PW = "the wizard's first password", "changed in the app later", "bob's own long password"


def load_mig():
    spec = importlib.util.spec_from_file_location("migrate_from_goblin_bank", ROOT / "scripts" / "migrate_from_goblin_bank.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_db(path: Path, key: str, wal_rows: int = 2):
    """An old worker database in WAL mode, with the newest rows still only in the -wal file (as after a hard stop)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE plaid_items(item_id TEXT PRIMARY KEY, institution TEXT, token_enc TEXT NOT NULL, cursor TEXT)")
    c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, secret INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE api_tokens(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, created TEXT)")
    c.commit()
    f = Fernet(key.encode())
    for i in range(wal_rows):
        c.execute("INSERT INTO plaid_items VALUES(?,?,?,NULL)", (f"item-{i}", "Bank", f.encrypt(b"access-secret").decode()))
    c.execute("INSERT INTO settings VALUES('plaid_secret',?,1)", (f.encrypt(b"plaid-secret").decode(),))
    c.execute("INSERT INTO api_tokens(name,token_hash,scopes,created) VALUES('home assistant','h','summary','2026-01-01')")
    c.commit()
    return c          # kept open so the -wal file is not folded back in


@pytest.fixture
def old_install(tmp_path):
    old, elsewhere = tmp_path / "goblin-bank", tmp_path / "elsewhere"
    sec = old / ".secrets"
    sec.mkdir(parents=True)
    keys = {n: Fernet.generate_key().decode() for n in ("alice", "wizard", "bob")}
    wiz_hash = hash_password(WIZ_OLD_PW)
    (old / "goblins.yaml").write_text("# my people\ngoblins:\n  - name: alice\n    share: [net_worth]\n  - name: wizard\n    rh_agent: true\nnetwork: ai_net   # shared with Firefly III\norb_key: true\n")
    kv = {"alice": {"INTERNAL_SECRET": "alice-int", "CONNECTOR_SECRET": "alice-conn", "USER_TOKEN_SHA256": sha256("alice-login-token"), "FIELD_KEY": keys["alice"]},
          "wizard": {"INTERNAL_SECRET": "wiz-int", "CONNECTOR_SECRET": "wiz-conn", "USER_PASSWORD_SCRYPT": wiz_hash, "FIELD_KEY": keys["wizard"]}}
    for n, d in kv.items():
        (sec / f"{n}.env").write_text("".join(f"{k}={v}\n" for k, v in d.items()))
        (sec / f"{n}.integrations.env").write_text("CALDAV_URL=\n")
    (sec / "plaid.env").write_text("PLAID_CLIENT_ID=abc\nPLAID_SECRET=real-plaid-secret\n")
    (sec / "wizard.rh-agent.env").write_text("# empty\n")
    (sec / "admin.password").write_text("adminpw\n")
    (sec / "admin.key.sha256").write_text(sha256("adminpw"))
    (sec / "orb.key.sha256").write_text(sha256("orb-key"))
    (old / "gateway.yaml").write_text(yaml.safe_dump({"goblins": [
        {"name": "alice", "url": "http://goblin-alice:8000", "internal_secret": "alice-int", "user_token_sha256": sha256("alice-login-token")},
        {"name": "wizard", "url": "http://goblin-wizard:8000", "internal_secret": "wiz-int", "user_password_scrypt": wiz_hash}],
        "orb_keys": [{"sha256": sha256("orb-key"), "scopes": ["summary:read"]}], "admin_token_sha256": sha256("adminpw")}))
    bob_hash, wiz_new = hash_password(BOB_PW), hash_password(WIZ_NEW_PW)
    (old / "gateway-state").mkdir()
    (old / "gateway-state" / "people.json").write_text(json.dumps({
        "people": [{"name": "bob", "url": "http://goblin-bob:8000", "internal_secret": "bob-int", "user_password_scrypt": bob_hash}],
        "policy": {"default": {"budget": 5}, "people": {"wizard": {"firefly_url": "http://ff", "firefly_token": "ff-pat"}}},
        "orb": [{"id": "x", "name": "orb2", "sha256": sha256("orb-key-2")}], "creds": {"wizard": wiz_new}}))
    gen = {"services": {
        "goblin-alice": {"env_file": [".secrets/alice.env", ".secrets/alice.integrations.env", ".secrets/plaid.env"], "environment": {"ROLE": "goblin", "GOBLIN_NAME": "alice", "CONNECTOR_SECRET": "alice-conn"},
                         "volumes": ["./data/alice/db:/data", "./data/alice/robinhood-csv:/data/robinhood-csv", "./data/alice/imports:/data/imports"]},
        "goblin-wizard": {"env_file": [".secrets/wizard.env", ".secrets/plaid.env"], "environment": {"ROLE": "goblin", "GOBLIN_NAME": "wizard", "CONNECTOR_SECRET": "wiz-conn", "TZ": "America/Chicago"},
                          "volumes": [f"{elsewhere}/wizard/db:/data", f"{elsewhere}/wizard/robinhood-csv:/data/robinhood-csv", f"{elsewhere}/wizard/imports:/data/imports"]},   # moved by hand
        "wizard-rh-agent": {"env_file": [".secrets/wizard.rh-agent.env"], "environment": {"ROLE": "rh-agent", "INTERNAL_SECRET": "wiz-conn"}},
        "gateway": {"environment": {"ROLE": "gateway"}, "volumes": ["./gateway.yaml:/config/gateway.yaml:ro", "./gateway-state:/state"], "ports": ["192.168.1.5:6500:8000"]}}}
    (old / "docker-compose.generated.yml").write_text(yaml.safe_dump(gen))
    (old / "docker-compose.bob.yml").write_text(yaml.safe_dump({"services": {"goblin-bob": {"environment": {"ROLE": "goblin", "GOBLIN_NAME": "bob", "INTERNAL_SECRET": "bob-int", "FIELD_KEY": keys["bob"]},
                                                                                          "env_file": [".secrets/plaid.env"], "volumes": ["./data/bob/db:/data"]}}}))
    conns = [make_db(old / "data" / "alice" / "db" / "goblin.db", keys["alice"]), make_db(elsewhere / "wizard" / "db" / "goblin.db", keys["wizard"], 3),
             make_db(old / "data" / "bob" / "db" / "goblin.db", keys["bob"], 1)]
    (old / "data" / "bob" / "db" / "robinhood-csv").mkdir()
    (old / "data" / "bob" / "db" / "robinhood-csv" / "export.csv").write_text("Activity Date\n")      # bob's compose mounted only /data
    (elsewhere / "wizard" / "imports").mkdir(parents=True)
    (elsewhere / "wizard" / "imports" / "bank.csv").write_text("date,amount\n")
    # the connections stay open while migrating: the newest rows are only in each -wal file, as after a hard stop
    yield old, elsewhere, keys, {"bob": bob_hash, "wiz_new": wiz_new}
    for c in conns:
        c.close()


def tree_digest(*folders: Path) -> str:
    h = hashlib.sha256()
    for folder in folders:
        for p in sorted(folder.rglob("*")):
            h.update(str(p).encode())
            if p.is_file():
                h.update(p.read_bytes())
    return h.hexdigest()


def migrate(tmp_path, monkeypatch, old, data_dir=None):
    mig, gen = load_mig(), load_gen()
    new = tmp_path / "plaid-proxy"
    new.mkdir(exist_ok=True)
    for m in (mig, gen):
        monkeypatch.setattr(m, "ROOT", new)
    monkeypatch.setattr(gen, "SEC", new / ".secrets")
    assert mig.main(["check", str(old)]) == 0
    assert mig.main(["copy", str(old), *(["--data-dir", str(data_dir)] if data_dir else [])]) == 0
    gen.main("people.yaml")
    assert mig.main(["verify", str(old)]) == 0
    return new


def test_everything_arrives_and_the_old_folder_is_untouched(tmp_path, monkeypatch, old_install):
    old, elsewhere, keys, hashes = old_install
    before = tree_digest(old, elsewhere)
    new = migrate(tmp_path, monkeypatch, old)
    assert tree_digest(old, elsewhere) == before
    spec = yaml.safe_load((new / "people.yaml").read_text())
    assert sorted(g["name"] for g in spec["people"]) == ["alice", "bob", "wizard"] and spec["network"] == "ai_net" and spec["orb_key"] is True
    assert spec["listen"] == "192.168.1.5" and spec["port"] == 6500                                  # reachable exactly where the old one was
    assert "# my people" in (new / "people.yaml").read_text()                                         # their own comments kept
    gw = {g["name"]: g for g in yaml.safe_load((new / "gateway.yaml").read_text())["people"]}
    assert gw["alice"]["user_token_sha256"] == sha256("alice-login-token") and gw["alice"]["url"] == "http://worker-alice:8000"
    assert verify_password(BOB_PW, gw["bob"]["user_password_scrypt"]) and gw["bob"]["internal_secret"] == "bob-int"
    state = json.loads((new / "gateway-state" / "people.json").read_text())
    assert state["creds"]["wizard"] == hashes["wiz_new"] and state["policy"]["people"]["wizard"]["firefly_token"] == "ff-pat"
    assert state["people"] == [] and state["orb"][0]["sha256"] == sha256("orb-key-2")                # bob is in people.yaml now
    for n, rows in (("alice", 2), ("wizard", 3), ("bob", 1)):
        db = new / "data" / n / "db" / "proxy.db"
        c = sqlite3.connect(db)
        assert c.execute("SELECT count(*) FROM plaid_items").fetchone()[0] == rows                    # including what was only in the -wal file
        token = c.execute("SELECT token_enc FROM plaid_items").fetchone()[0]
        assert Fernet(keys[n].encode()).decrypt(token.encode()) == b"access-secret"
        assert c.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 1
        assert not (new / "data" / n / "db" / "goblin.db").exists()
        worker = (new / ".secrets" / f"{n}.worker.env").read_text()
        assert f"FIELD_KEY={keys[n]}" in worker and "USER_" not in worker
    assert (new / "data" / "bob" / "robinhood-csv" / "export.csv").exists() and (new / "data" / "wizard" / "imports" / "bank.csv").exists()
    assert (new / ".secrets" / "plaid.env").read_text() == (old / ".secrets" / "plaid.env").read_text()
    assert (new / ".secrets" / "admin.password").read_text().strip() == "adminpw"
    compose = yaml.safe_load((new / "docker-compose.generated.yml").read_text())
    assert set(compose["services"]) == {"gateway", "worker-alice", "worker-wizard", "worker-bob", "wizard-rh-agent"}
    assert "wiz-conn" in (new / ".secrets" / "wizard.connector.env").read_text() and "wiz-conn" not in (new / "docker-compose.generated.yml").read_text()
    for p in (new / ".secrets").iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    assert stat.S_IMODE((new / "data" / "wizard" / "db" / "proxy.db").stat().st_mode) == 0o600
    assert (new / ".migrated-from").read_text().strip() == str(old)


def test_data_can_go_to_a_separate_folder(tmp_path, monkeypatch, old_install):
    old, *_ = old_install
    outside = tmp_path / "appdata"
    new = migrate(tmp_path, monkeypatch, old, data_dir=outside)
    assert (outside / ".secrets" / "wizard.env").exists() and (outside / "data" / "wizard" / "db" / "proxy.db").exists() and (outside / "gateway.yaml").exists()
    assert not (new / ".secrets").exists() and yaml.safe_load((new / "people.yaml").read_text())["data_dir"] == str(outside)


def test_it_refuses_when_a_key_would_not_open_the_bank_links(tmp_path, monkeypatch, old_install, capsys):
    old, *_ = old_install
    (old / ".secrets" / "alice.env").write_text((old / ".secrets" / "alice.env").read_text().replace("FIELD_KEY=", "FIELD_KEY=x"))
    mig = load_mig()
    monkeypatch.setattr(mig, "ROOT", tmp_path / "new")
    assert mig.main(["check", str(old)]) == 1 and "alice: FIELD_KEY is not a valid key" in capsys.readouterr().err
    other = Fernet.generate_key().decode()
    lines = [l for l in (old / ".secrets" / "alice.env").read_text().splitlines() if not l.startswith("FIELD_KEY")]
    (old / ".secrets" / "alice.env").write_text("\n".join(lines + [f"FIELD_KEY={other}"]) + "\n")
    assert mig.main(["check", str(old)]) == 1 and "do not open" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


def test_it_never_overwrites_an_existing_setup(tmp_path, monkeypatch, old_install, capsys):
    old, *_ = old_install
    mig = load_mig()
    new = tmp_path / "new"
    (new / ".secrets").mkdir(parents=True)
    (new / ".secrets" / "alice.env").write_text("precious")
    monkeypatch.setattr(mig, "ROOT", new)
    assert mig.main(["copy", str(old)]) == 1 and "never overwrites" in capsys.readouterr().err
    assert (new / ".secrets" / "alice.env").read_text() == "precious"


def test_a_wrong_login_is_caught_by_verify(tmp_path, monkeypatch, old_install, capsys):
    old, *_ = old_install
    new = migrate(tmp_path, monkeypatch, old)
    g = yaml.safe_load((new / "gateway.yaml").read_text())
    g["people"][0]["user_token_sha256"] = sha256("someone else")
    (new / "gateway.yaml").write_text(yaml.safe_dump(g))
    mig = load_mig()
    monkeypatch.setattr(mig, "ROOT", new)
    assert mig.main(["verify", str(old)]) == 1 and "login changed" in capsys.readouterr().err
