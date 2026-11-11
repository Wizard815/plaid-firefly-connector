#!/usr/bin/env python3
"""Generate secrets, gateway.yaml and docker-compose.generated.yml from people.yaml.

people.yaml:
  people:
    - name: alice
      share: [robinhood_agent]     # what the Orb/gateway may see
      rh_agent: true               # run the official-MCP connector
      rh_live: false               # run the unofficial live connector (off by default)
  orb_key: true                    # create a read-only key for Pondering Orb
  data_dir: /mnt/user/appdata/plaid-proxy-data   # optional: keep .secrets/, data/, gateway-state/ and gateway.yaml outside this folder
                                                 # (PLAID_PROXY_DATA_DIR in the environment wins). Default: this folder, as before.

Logins: a worker entry may carry `password: <12+ characters>`; only a salted scrypt hash of it is stored. Without one, a random login token is made, printed ONCE,
and only its sha256 is stored. The admin password is a random hex string saved in .secrets/admin.password and printed every time this runs; the gateway keeps
only its hash. The orb key is printed once.

    python3 scripts/gen_compose.py                          # (re)generate everything
    python3 scripts/gen_compose.py --reset-token NAME       # a new random login token for NAME
    PROXY_NEW_PASSWORD=... python3 scripts/gen_compose.py --set-password NAME     # set or change NAME's password (never read from a file)
"""
import ipaddress
import os
import re
import secrets
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plaidproxy.security import check_password_strength, hash_password, sha256, verify_password  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEC = ROOT / ".secrets"
WORKER_KEYS = ("INTERNAL_SECRET", "CONNECTOR_SECRET", "FIELD_KEY")      # all a worker container needs from <name>.env; login hashes stay with the gateway


def env_line(k, v) -> str:
    """One line of a file Docker Compose loads with env_file. Compose expands $NAME in those files, so a literal $ is written as $$
    (an unescaped scrypt hash, scrypt$N$r$p$salt$hash, was cut to a few bytes with only a quiet 'variable is not set' warning)."""
    v = str(v)
    if "\n" in v or "\r" in v:
        raise ValueError(f"{k} has a line break in it")
    return f"{k}={v.replace('$', '$$')}\n"


def write_private(path: Path, text: str):
    """Owner-only from the first byte (no moment where it is readable by others), replaced atomically."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def data_dir(spec) -> Path:
    """Where live data goes: PLAID_PROXY_DATA_DIR, else `data_dir:` in people.yaml, else this folder. A relative path is taken from this folder."""
    d = os.environ.get("PLAID_PROXY_DATA_DIR") or spec.get("data_dir") or ""
    if not d:
        return ROOT
    d = Path(str(d)).expanduser()
    return d if d.is_absolute() else ROOT / d


def _mount(p: Path) -> str:
    """A host path for the compose file: ./relative when it is inside this folder (as before), absolute otherwise."""
    try:
        return "./" + p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def warn_dollars(path: Path):
    """Files people fill in by hand (Plaid keys, CalDAV and Robinhood passwords) are loaded by Compose too: a lone $ in a value is expanded and the value cut short."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        k, _, v = line.partition("=")
        if v and not line.lstrip().startswith("#") and re.search(r"(?<!\$)\$(?!\$)", v.replace("$$", "")) and not (v.startswith("'") and v.endswith("'")):
            print(f"WARNING: {path.name}: {k.strip()} contains a $. Docker Compose would cut it short. Write each $ as $$, or put the value in single quotes.")


def _stub(path, text):
    if not path.exists():
        path.write_text(text)
        path.chmod(0o600)
        print(f"created {path.name} (optional: everything in it can be entered in the app instead)")
    path.chmod(0o600)


def _password_for(g, n, setpw):
    """The password a person chose, from the install prompt / --set-password (environment) or the optional `password:` in people.yaml. Checked for length here."""
    pw = os.environ.get("PROXY_NEW_PASSWORD") if setpw == n else os.environ.get("PROXY_PASSWORD_" + n.upper().replace("-", "_")) or g.get("password")
    if pw is None or pw == "":
        return None
    try:
        check_password_strength(str(pw), n)
    except ValueError as e:
        sys.exit(f"bad password for {n!r}: {e}")
    return str(pw)


def main(src="people.yaml", *flags):
    reset = setpw = None
    if src == "--reset-token":                      # python scripts/gen_compose.py --reset-token bob : new login token, nothing else changes
        reset, src = flags[0] if flags else sys.exit("usage: --reset-token <name>"), "people.yaml"
    elif src == "--set-password":                   # PROXY_NEW_PASSWORD=... python scripts/gen_compose.py --set-password bob
        setpw, src = flags[0] if flags else sys.exit("usage: --set-password <name>"), "people.yaml"
    spec = yaml.safe_load((ROOT / src).read_text())
    data = data_dir(spec)
    sec = SEC if data == ROOT else data / ".secrets"
    data.mkdir(parents=True, exist_ok=True)
    sec.mkdir(exist_ok=True, mode=0o700)
    sec.chmod(0o700)
    listen = str(spec.get("listen", "127.0.0.1"))                  # who can reach the app: 127.0.0.1 = this machine only; a LAN address or 0.0.0.0 = other devices too
    try:
        ipaddress.ip_address(listen)
    except ValueError:
        sys.exit(f"bad `listen:` {listen!r}: use an IP address such as 127.0.0.1, 192.168.1.5 or 0.0.0.0")
    network = spec.get("network")
    if not network:
        sys.exit("set `network: <docker network name>` in people.yaml — an existing external network "
                  "(docker network create <name>) that worker and connector containers join")
    names = [g["name"] for g in spec["people"]]
    if len(set(names)) != len(names):
        sys.exit("duplicate worker names")
    gw_workers, services, networks = [], {}, {"edge": {}, network: {"external": True}}
    for g in spec["people"]:
        n = g["name"]
        if not n.replace("-", "").isalnum() or not n.islower():
            sys.exit(f"bad worker name {n!r} (lowercase alnum/hyphen)")
        f = sec / f"{n}.env"
        kv = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l) if f.exists() else {}
        changed = False
        pw = _password_for(g, n, setpw)
        if setpw == n and not pw:
            sys.exit("set PROXY_NEW_PASSWORD to the new password (it is never read from a file)")
        if reset == n and kv:
            token = secrets.token_urlsafe(32)
            kv["USER_TOKEN_SHA256"] = sha256(token)
            kv.pop("USER_PASSWORD_SCRYPT", None)
            print(f"[{n}] NEW login token (shown once): {token}")
            changed = True
        if "INTERNAL_SECRET" not in kv:
            kv["INTERNAL_SECRET"], kv["CONNECTOR_SECRET"] = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if not pw:
                token = secrets.token_urlsafe(32)
                kv["USER_TOKEN_SHA256"] = sha256(token)
                print(f"[{n}] login token (shown once): {token}")
            changed = True
        if pw and not verify_password(pw, kv.get("USER_PASSWORD_SCRYPT")):      # a chosen password: salted scrypt, never plain sha256, and the plaintext is not written anywhere
            kv["USER_PASSWORD_SCRYPT"] = hash_password(pw)
            kv.pop("USER_TOKEN_SHA256", None)
            print(f"[{n}] password set (only a salted scrypt hash of it is stored)")
            changed = True
        if "FIELD_KEY" not in kv:          # encrypts Plaid tokens at rest; LOSE THIS AND LINKS MUST BE REDONE
            kv["FIELD_KEY"] = Fernet.generate_key().decode()
            changed = True
        if changed:
            write_private(f, "".join(f"{k}={v}\n" for k, v in kv.items()))       # the generator's own record (read only by this script, never by Compose)
        f.chmod(0o600)
        internal, connector = kv["INTERNAL_SECRET"], kv["CONNECTOR_SECRET"]
        write_private(sec / f"{n}.worker.env", "".join(env_line(k, kv[k]) for k in WORKER_KEYS))      # what the worker container gets: no login hashes
        write_private(sec / f"{n}.connector.env", env_line("INTERNAL_SECRET", connector))            # the Robinhood connectors' secret, kept out of the compose file
        for hand in (f"{n}.integrations.env", "plaid.env", f"{n}.rh-agent.env", f"{n}.rh-live.env"):
            warn_dollars(sec / hand)
        _stub(sec / f"{n}.integrations.env", "CALDAV_URL=\nCALDAV_USER=\nCALDAV_PASSWORD=\n")
        _stub(sec / "plaid.env", "PLAID_CLIENT_ID=\nPLAID_SECRET=\nPLAID_ENV=sandbox\nPLAID_WEBHOOK_URL=\n")
        gw_workers.append({"name": n, "url": f"http://worker-{n}:8000", "internal_secret": internal,
                           **({"user_password_scrypt": kv["USER_PASSWORD_SCRYPT"]} if kv.get("USER_PASSWORD_SCRYPT") else {"user_token_sha256": kv["USER_TOKEN_SHA256"]})})
        for sub in ("db", "robinhood-csv", "rh-live-session", "imports"):
            d = data / "data" / n / sub
            d.mkdir(parents=True, exist_ok=True)
            for owner_only in (d, d.parent, d.parent.parent):
                owner_only.chmod(0o700)
            try:
                os.chown(d, 10001, 10001)    # the containers run as uid 10001
                os.chown(d.parent, 10001, 10001)
            except PermissionError:
                print(f"hint: sudo chown -R 10001:10001 data/{n}")
        networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
        env = {"ROLE": "worker", "PERSON_NAME": n, "SHARE": ",".join(g.get("share", [])),
               "GATEWAY_URL": "http://gateway:8000"}
        S, D = _mount(sec), _mount(data / "data" / n)
        services[f"worker-{n}"] = {"build": ".", "env_file": [f"{S}/{n}.worker.env", f"{S}/{n}.integrations.env", f"{S}/plaid.env"], "environment": env,
                                   "volumes": [f"{D}/db:/data", f"{D}/robinhood-csv:/data/robinhood-csv", f"{D}/imports:/data/imports"], "user": "10001", "extra_hosts": ["host.docker.internal:host-gateway"],
                                   "networks": ["edge", network, "egress"], "restart": "unless-stopped"}
        if g.get("rh_agent"):
            _stub(sec / f"{n}.rh-agent.env", "# Optional. Leave empty and set these on the Settings page instead.\n")
            env["RH_AGENT_URL"] = f"http://{n}-rh-agent:8000"
            services[f"{n}-rh-agent"] = {"build": ".", "env_file": [f"{S}/{n}.rh-agent.env", f"{S}/{n}.connector.env"],
                                         "environment": {"ROLE": "rh-agent", "CONFIG_URL": f"http://worker-{n}:8000/internal/connector-config/rh-agent"}, "networks": [network, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
        if g.get("rh_live"):
            _stub(sec / f"{n}.rh-live.env", "RH_USERNAME=\nRH_PASSWORD=\n")
            env["RH_LIVE_URL"] = f"http://{n}-rh-live:8000"
            services[f"{n}-rh-live"] = {"build": {"context": ".", "args": {"WITH_LIVE": "1"}},
                                        "env_file": [f"{S}/{n}.rh-live.env", f"{S}/{n}.connector.env"],
                                        "environment": {"ROLE": "rh-live", "RH_LIVE_ENABLED": "1"},
                                        "volumes": [f"{D}/rh-live-session:/session"], "networks": [network, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
    if reset and reset not in names:
        sys.exit(f"{reset!r} is not in people.yaml (people added on the admin page: remove and re-add them there)")
    orb = []
    if spec.get("orb_key"):
        of = sec / "orb.key.sha256"
        if not of.exists():
            k = secrets.token_urlsafe(32)
            print(f"[orb] API key for Pondering Orb (shown once): {k}")
            write_private(of, sha256(k))
        of.chmod(0o600)
        orb = [{"sha256": of.read_text().strip(), "scopes": ["summary:read"]}]
    af = sec / "admin.key.sha256"          # for the /admin page: general health and counts only
    pf = sec / "admin.password"            # the admin password: random hex, kept here and shown on every run (the gateway only ever gets its hash)
    if not pf.exists() and not af.exists():
        write_private(pf, secrets.token_hex(16) + "\n")
    if pf.exists():
        ap = pf.read_text().strip()
        if not af.exists() or af.read_text().strip() != sha256(ap):
            write_private(af, sha256(ap))
        pf.chmod(0o600)
        print(f"[admin] admin password: {ap}   (saved in {pf})")
    else:
        print("[admin] an admin token from an earlier install is in use (only its hash is stored). Delete .secrets/admin.key.sha256 and run this again to get an admin password.")
    if af.exists():
        af.chmod(0o600)
    st = data / "gateway-state"                                         # people added from the admin page live here
    st.mkdir(exist_ok=True)
    st.chmod(0o700)
    try:
        os.chown(st, 10001, 10001)
    except PermissionError:
        print("hint: sudo chown -R 10001:10001 gateway-state")
    gwf = data / "gateway.yaml"
    write_private(gwf, yaml.safe_dump({"people": gw_workers, "orb_keys": orb, "admin_token_sha256": af.read_text().strip()}))
    try:
        os.chown(gwf, 10001, 10001)      # the gateway container runs as uid 10001 and must be able to read it
    except PermissionError:
        print("hint: sudo chown 10001:10001 gateway.yaml   (the gateway container cannot read it otherwise)")
    services["gateway"] = {"build": ".", "environment": {"ROLE": "gateway", "GATEWAY_CONFIG": "/config/gateway.yaml", "UI_DIR": "/app/ui", "GATEWAY_STATE": "/state/people.json"}, "user": "10001",
                           "volumes": [f"{_mount(gwf)}:/config/gateway.yaml:ro", f"{_mount(st)}:/state"], "networks": ["edge"],
                           "ports": [f"{listen}:{int(spec.get('port', 6421))}:8000"], "restart": "unless-stopped"}
    # A fixed project name: the stack is "plaid-proxy" whatever this folder is called. Holds no secrets (they are all in env files).
    (ROOT / "docker-compose.generated.yml").write_text(yaml.safe_dump({"name": "plaid-proxy", "services": services, "networks": networks}, sort_keys=False))
    print(f"wrote gateway.yaml and docker-compose.generated.yml for {len(names)} worker(s)")


if __name__ == "__main__":
    main(*sys.argv[1:])
