"""The compose generator's secret handling: values with $ survive Docker Compose, no secret is written into the compose file,
workers never get login hashes, the data folder can live outside the checkout, and everything secret is owner-only."""
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from plaidproxy.security import verify_password

from test_passwords import PW, load_gen


@pytest.fixture
def site(tmp_path, monkeypatch):
    gen = load_gen()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(gen, "ROOT", repo)
    monkeypatch.setattr(gen, "SEC", repo / ".secrets")
    for k in ("PROXY_NEW_PASSWORD", "PLAID_PROXY_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    (repo / "people.yaml").write_text(yaml.safe_dump({"network": "testnet", "people": [{"name": "alice", "password": PW, "rh_agent": True, "rh_live": True}, {"name": "bob"}]}))
    return gen, repo


def compose_env(compose_file: Path) -> dict:
    """What Docker Compose itself would hand each container, as `docker compose config` resolves it."""
    r = subprocess.run(["docker", "compose", "-f", str(compose_file), "config", "--format", "json"], capture_output=True, text=True, cwd=compose_file.parent)
    assert r.returncode == 0, r.stderr
    assert "is not set" not in r.stderr, r.stderr
    import json
    # `config` prints a loadable compose file, so it writes each literal $ back as $$: undo that to get the value a container sees.
    return {name: {k: (v or "").replace("$$", "$") for k, v in (s.get("environment") or {}).items()} for name, s in json.loads(r.stdout)["services"].items()}


def mode(p: Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))


def test_secrets_with_dollar_signs_reach_the_containers_whole(site):
    gen, repo = site
    gen.main("people.yaml")
    kv = dict(l.split("=", 1) for l in (repo / ".secrets" / "alice.env").read_text().splitlines())
    assert "$" in kv["USER_PASSWORD_SCRYPT"] and verify_password(PW, kv["USER_PASSWORD_SCRYPT"])        # the generator's own record is untouched
    worker = (repo / ".secrets" / "alice.worker.env").read_text()
    assert "USER_PASSWORD_SCRYPT" not in worker and "USER_TOKEN_SHA256" not in worker                 # workers never get login hashes
    # Any value with $ in it is escaped for Compose: prove it with a real one through env_line.
    tricky = "scrypt$131072$8$1$KeiiBgcIqPbBt3wqB51Pmg==$shxgPrHY7vzibtTzx1RHICR3IhG6EPvy6yolUvNacHM="
    (repo / ".secrets" / "probe.env").write_text(gen.env_line("PROBE", tricky))
    (repo / "probe.yml").write_text(yaml.safe_dump({"services": {"p": {"image": "busybox", "env_file": ["./.secrets/probe.env"]}}}))
    if not shutil.which("docker") or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("docker compose is not installed here")
    assert compose_env(repo / "probe.yml")["p"]["PROBE"] == tricky
    (repo / ".secrets" / "probe.env").write_text(f"PROBE={tricky}\n")                                # the old, unescaped way really is cut short
    r = subprocess.run(["docker", "compose", "-f", "probe.yml", "config", "--format", "json"], capture_output=True, text=True, cwd=repo)
    assert "is not set" in r.stderr and tricky not in r.stdout.replace("$$", "$")
    env = compose_env(repo / "docker-compose.generated.yml")
    assert env["worker-alice"]["FIELD_KEY"] == kv["FIELD_KEY"] and env["worker-alice"]["CONNECTOR_SECRET"] == kv["CONNECTOR_SECRET"]
    assert env["worker-alice"]["INTERNAL_SECRET"] == kv["INTERNAL_SECRET"] and "USER_PASSWORD_SCRYPT" not in env["worker-alice"]
    assert env["alice-rh-agent"]["INTERNAL_SECRET"] == env["alice-rh-live"]["INTERNAL_SECRET"] == kv["CONNECTOR_SECRET"]


def test_no_secret_is_written_into_the_compose_file(site):
    gen, repo = site
    gen.main("people.yaml")
    compose = (repo / "docker-compose.generated.yml").read_text()
    for n in ("alice", "bob"):
        kv = dict(l.split("=", 1) for l in (repo / ".secrets" / f"{n}.env").read_text().splitlines())
        for v in kv.values():
            assert v not in compose
    c = yaml.safe_load(compose)
    assert c["name"] == "plaid-proxy" and set(c["services"]) == {"gateway", "worker-alice", "worker-bob", "alice-rh-agent", "alice-rh-live"}
    for s in c["services"].values():
        assert not {"INTERNAL_SECRET", "CONNECTOR_SECRET", "FIELD_KEY"} & set(s.get("environment") or {})


def test_files_and_folders_are_owner_only(site):
    gen, repo = site
    (repo / "people.yaml").write_text(yaml.safe_dump({**yaml.safe_load((repo / "people.yaml").read_text()), "orb_key": True}))
    gen.main("people.yaml")
    sec = repo / ".secrets"
    assert mode(sec) == "0o700"
    assert {p.name: mode(p) for p in sec.iterdir()} == {p.name: "0o600" for p in sec.iterdir()}
    assert mode(repo / "gateway.yaml") == "0o600" and mode(repo / "gateway-state") == "0o700"
    for n in ("alice", "bob"):
        assert mode(repo / "data" / n) == "0o700" and mode(repo / "data" / n / "db") == "0o700"


def test_the_data_folder_can_live_outside_the_checkout_and_regeneration_keeps_it(site, tmp_path, monkeypatch):
    gen, repo = site
    elsewhere = tmp_path / "appdata"
    spec = yaml.safe_load((repo / "people.yaml").read_text())
    (repo / "people.yaml").write_text(yaml.safe_dump({**spec, "data_dir": str(elsewhere)}))
    gen.main("people.yaml")
    first = (elsewhere / ".secrets" / "alice.env").read_text()
    gen.main("people.yaml")                                                                     # regenerating does not move or clobber anything
    assert (elsewhere / ".secrets" / "alice.env").read_text() == first
    assert not (repo / ".secrets").exists() and not (repo / "data").exists() and not (repo / "gateway.yaml").exists() and not (repo / "gateway-state").exists()
    c = yaml.safe_load((repo / "docker-compose.generated.yml").read_text())
    assert c["services"]["worker-alice"]["volumes"][0] == f"{elsewhere}/data/alice/db:/data"
    assert c["services"]["worker-alice"]["env_file"][0] == f"{elsewhere}/.secrets/alice.worker.env"
    assert c["services"]["gateway"]["volumes"] == [f"{elsewhere}/gateway.yaml:/config/gateway.yaml:ro", f"{elsewhere}/gateway-state:/state"]
    monkeypatch.setenv("PLAID_PROXY_DATA_DIR", str(tmp_path / "other"))                        # the environment wins over the file
    gen.main("people.yaml")
    assert (tmp_path / "other" / ".secrets" / "alice.env").exists()


def test_a_lone_dollar_in_a_hand_edited_file_is_warned_about(site, capsys):
    gen, repo = site
    gen.main("people.yaml")
    (repo / ".secrets" / "plaid.env").write_text("PLAID_CLIENT_ID=abc\nPLAID_SECRET=ab$cd\nPLAID_ENV=sandbox\n")
    (repo / ".secrets" / "bob.integrations.env").write_text("CALDAV_PASSWORD='ok$fine'\nCALDAV_USER=a$$b\n")
    capsys.readouterr()
    gen.main("people.yaml")
    out = capsys.readouterr().out
    assert "plaid.env: PLAID_SECRET contains a $" in out and "CALDAV" not in out and "ab$cd" not in out
