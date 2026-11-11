"""Passwords people choose: scrypt, a minimum length, throttled guessing, an admin password kept in a file, and no plaintext anywhere it should not be."""
import importlib.util
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from plaidproxy import gateway, security
from plaidproxy.security import Throttle, check_password_strength, hash_password, sha256, verify_password

PW = "correct horse battery"
ROOT = Path(__file__).resolve().parents[1]


def load_gen():
    spec = importlib.util.spec_from_file_location("gen_compose", ROOT / "scripts" / "gen_compose.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def site(tmp_path, monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen, "SEC", tmp_path / ".secrets")
    monkeypatch.delenv("PROXY_NEW_PASSWORD", raising=False)
    (tmp_path / "people.yaml").write_text(yaml.safe_dump({"network": "testnet", "people": [{"name": "alice", "password": PW}, {"name": "bob"}]}))
    return gen, tmp_path


def everything(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file() and p.name != "people.yaml")


# ---------------------------------------------------------------- hashing
def test_scrypt_hash_is_salted_uses_owasp_parameters_and_verifies():
    a, b = hash_password(PW), hash_password(PW)
    assert a != b and a.startswith("scrypt$131072$8$1$")                    # N=2^17, r=8, p=1, a fresh salt each time
    assert verify_password(PW, a) and verify_password(PW, b)
    assert not verify_password("correct horse batterz", a) and not verify_password("", a) and not verify_password(PW, "garbage") and not verify_password(PW, None)
    assert PW not in a and sha256(PW) not in a


def test_minimum_length_and_name_rule():
    for bad in ("short", "elevenchars", ""):
        with pytest.raises(ValueError):
            check_password_strength(bad)
    check_password_strength("twelve chars")
    with pytest.raises(ValueError):
        check_password_strength("alicealicealice", "AliceAliceAlice")


def test_throttle_backs_off_doubles_and_clears(monkeypatch):
    now = [100.0]
    t = Throttle(clock=lambda: now[0])
    for _ in range(5):
        t.fail("u:a"); t.check("u:a")                                       # five free misses
    t.fail("u:a")
    with pytest.raises(Exception) as e:
        t.check("u:a")
    assert getattr(e.value, "status_code", None) == 429
    now[0] += 2.5
    t.check("u:a")                                                           # waited it out
    t.fail("u:a")
    assert 3.5 < t.wait_for("u:a") <= 4.0                                    # then it doubled
    for _ in range(30):
        t.fail("u:a")
    assert t.wait_for("u:a") <= 900                                          # capped at 15 minutes
    t.ok("u:a"); t.check("u:a")                                              # a right answer clears it
    t.check("u:other")                                                       # and it is per name


# ---------------------------------------------------------------- gen_compose
def test_password_is_hashed_never_written_and_the_admin_password_is_a_random_hex_file(site, capsys):
    gen, root = site
    gen.main("people.yaml")
    out = capsys.readouterr().out
    alice = (root / ".secrets" / "alice.env").read_text()
    assert "USER_PASSWORD_SCRYPT=scrypt$131072$8$1$" in alice and "USER_TOKEN_SHA256" not in alice
    assert "USER_TOKEN_SHA256=" in (root / ".secrets" / "bob.env").read_text()                       # no password given: the random-token fallback as before
    assert "[bob] login token (shown once)" in out and "[alice] password set" in out and PW not in out
    assert PW not in everything(root) and sha256(PW) not in everything(root)                         # not in .secrets/*.env, gateway.yaml, the compose file, anywhere
    gw = yaml.safe_load((root / "gateway.yaml").read_text())
    entries = {g["name"]: g for g in gw["people"]}
    assert entries["alice"]["user_password_scrypt"].startswith("scrypt$") and "user_token_sha256" not in entries["alice"] and "user_token_sha256" in entries["bob"]
    # the admin password: random hex in a 0600 file, printed, only its hash goes to the gateway
    ap = (root / ".secrets" / "admin.password").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{32}", ap) and oct((root / ".secrets" / "admin.password").stat().st_mode)[-3:] == "600"
    assert f"[admin] admin password: {ap}" in out and gw["admin_token_sha256"] == sha256(ap) and ap not in (root / "gateway.yaml").read_text()
    before = alice
    gen.main("people.yaml")                                                                           # running it again keeps everything and prints the admin password again
    assert (root / ".secrets" / "alice.env").read_text() == before and (root / ".secrets" / "admin.password").read_text().strip() == ap
    assert f"[admin] admin password: {ap}" in capsys.readouterr().out


def test_too_short_password_fails_with_a_clear_error(site):
    gen, root = site
    (root / "people.yaml").write_text(yaml.safe_dump({"network": "n", "people": [{"name": "alice", "password": "tooshort"}]}))
    with pytest.raises(SystemExit) as e:
        gen.main("people.yaml")
    assert "bad password for 'alice'" in str(e.value) and "at least 12" in str(e.value)
    assert not (root / ".secrets" / "alice.env").exists()


def test_set_password_takes_it_from_the_environment_only(site, monkeypatch, capsys):
    gen, root = site
    gen.main("people.yaml"); capsys.readouterr()
    with pytest.raises(SystemExit):
        gen.main("--set-password", "bob")                                                              # no PROXY_NEW_PASSWORD: refuses
    monkeypatch.setenv("PROXY_NEW_PASSWORD", "a brand new phrase here")
    gen.main("--set-password", "bob")
    bob = (root / ".secrets" / "bob.env").read_text()
    assert "USER_PASSWORD_SCRYPT=" in bob and "USER_TOKEN_SHA256" not in bob and "a brand new phrase here" not in everything(root)
    monkeypatch.setenv("PROXY_NEW_PASSWORD", "short")
    with pytest.raises(SystemExit):
        gen.main("--set-password", "bob")


# ---------------------------------------------------------------- the gateway
def gateway_from(root: Path, monkeypatch):
    monkeypatch.setenv("GATEWAY_STATE", str(root / "state.json"))
    cfg = yaml.safe_load((root / "gateway.yaml").read_text())
    seen = []
    return TestClient(gateway.create_app(cfg, transport=httpx.MockTransport(lambda r: (seen.append(r.url.path), httpx.Response(200, json={"ok": True}))[1]))), cfg


def test_password_login_session_throttle_and_change(site, monkeypatch, capsys):
    gen, root = site
    gen.main("people.yaml"); capsys.readouterr()
    gw, _ = gateway_from(root, monkeypatch)
    ok = gw.post("/login", json={"name": "alice", "password": PW})
    assert ok.status_code == 200
    tok = ok.json()["token"]
    assert gw.get("/g/alice/api/overview", headers={"Authorization": "Bearer " + tok}).status_code == 200      # the session token opens her worker
    assert gw.get("/g/bob/api/overview", headers={"Authorization": "Bearer " + tok}).status_code == 401          # and only hers
    assert gw.get("/g/alice/api/overview", headers={"Authorization": "Bearer " + PW}).status_code == 401         # the password itself is not a bearer token
    # wrong passwords: same answer for a real and an unknown name, then slowed down
    assert gw.post("/login", json={"name": "alice", "password": "nope nope nope"}).status_code == 401
    assert gw.post("/login", json={"name": "ghost", "password": "nope nope nope"}).status_code == 401
    codes = [gw.post("/login", json={"name": "alice", "password": f"wrong guess {i}"}).status_code for i in range(8)]
    assert 429 in codes
    assert gw.post("/login", json={"name": "alice", "password": PW}).status_code == 429                          # even the right one waits
    assert gw.post("/login", json={"name": "bob", "password": "x" * 14}).status_code == 401                       # someone else is unaffected
    # the random-token user still works exactly as before
    bob_tok = None
    # change a password: needs the current one, enforces the length, signs the old session out, persists across a restart
    gw2, _ = gateway_from(root, monkeypatch)
    assert gw2.post("/account/password", json={"name": "alice", "current": "wrong", "new": "another long phrase"}).status_code == 401
    assert gw2.post("/account/password", json={"name": "alice", "current": PW, "new": "short"}).status_code == 422
    t0 = gw2.post("/login", json={"name": "alice", "password": PW}).json()["token"]
    r = gw2.post("/account/password", json={"name": "alice", "current": PW, "new": "another long phrase"})
    assert r.status_code == 200
    assert gw2.get("/g/alice/api/overview", headers={"Authorization": "Bearer " + t0}).status_code == 401            # old session gone
    assert gw2.get("/g/alice/api/overview", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200
    gw3, _ = gateway_from(root, monkeypatch)                                                                       # restart: the new password is remembered, the old one is not
    assert gw3.post("/login", json={"name": "alice", "password": "another long phrase"}).status_code == 200
    assert gw3.post("/login", json={"name": "alice", "password": PW}).status_code == 401
    assert "another long phrase" not in (root / "state.json").read_text() and "another long phrase" not in everything(root)


def test_random_token_users_and_the_admin_password_still_work_and_are_throttled(site, monkeypatch, capsys):
    gen, root = site
    gen.main("people.yaml")
    out = capsys.readouterr().out
    bob_tok = re.search(r"\[bob\] login token \(shown once\): (\S+)", out).group(1)
    ap = (root / ".secrets" / "admin.password").read_text().strip()
    gw, _ = gateway_from(root, monkeypatch)
    assert gw.get("/g/bob/api/overview", headers={"Authorization": "Bearer " + bob_tok}).status_code == 200             # direct, as before
    assert gw.post("/login", json={"name": "bob", "password": bob_tok}).status_code == 200                              # and through the login box
    assert gw.get("/admin/status", headers={"Authorization": "Bearer " + ap}).status_code == 200
    codes = [gw.get("/admin/status", headers={"Authorization": f"Bearer guess{i}"}).status_code for i in range(8)]
    assert 429 in codes
    assert gw.get("/admin/status", headers={"Authorization": "Bearer " + ap}).status_code == 429


def test_admin_can_add_a_person_with_a_password(site, monkeypatch, capsys):
    gen, root = site
    gen.main("people.yaml")
    ap = (root / ".secrets" / "admin.password").read_text().strip()
    gw, _ = gateway_from(root, monkeypatch)
    A = {"Authorization": "Bearer " + ap}
    assert gw.post("/admin/people", headers=A, json={"name": "cara", "password": "short"}).status_code == 422
    r = gw.post("/admin/people", headers=A, json={"name": "cara", "password": "cara has a long phrase"}).json()
    assert r["password_set"] is True and r["login_token"] == ""
    assert gw.post("/login", json={"name": "cara", "password": "cara has a long phrase"}).status_code == 200
    assert "cara has a long phrase" not in (root / "state.json").read_text()
