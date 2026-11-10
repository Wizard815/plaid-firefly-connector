#!/usr/bin/env python3
"""Generate secrets, gateway.yaml and docker-compose.generated.yml from goblins.yaml.

goblins.yaml:
  goblins:
    - name: alice
      share: [robinhood_agent]     # what the Orb/gateway may see
      rh_agent: true               # run the official-MCP connector
      rh_live: false               # run the unofficial live connector (off by default)
  orb_key: true                    # create a read-only key for Pondering Orb

Logins: a goblin entry may carry `password: <12+ characters>`; only a salted scrypt hash of it is stored. Without one, a random login token is made, printed ONCE,
and only its sha256 is stored. The admin password is a random hex string saved in .secrets/admin.password and printed every time this runs; the gateway keeps
only its hash. The orb key is printed once.

    python3 scripts/gen_compose.py                          # (re)generate everything
    python3 scripts/gen_compose.py --reset-token NAME       # a new random login token for NAME
    GOBLIN_NEW_PASSWORD=... python3 scripts/gen_compose.py --set-password NAME     # set or change NAME's password (never read from a file)
"""
import os
import secrets
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from goblinbank.security import check_password_strength, hash_password, sha256, verify_password  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEC = ROOT / ".secrets"


def _stub(path, text):
    if not path.exists():
        path.write_text(text)
        path.chmod(0o600)
        print(f"created {path.name} (optional: everything in it can be entered in the app instead)")


def _password_for(g, n, setpw):
    """The password a person chose, from the install prompt / --set-password (environment) or the optional `password:` in goblins.yaml. Checked for length here."""
    pw = os.environ.get("GOBLIN_NEW_PASSWORD") if setpw == n else os.environ.get("GOBLIN_PASSWORD_" + n.upper().replace("-", "_")) or g.get("password")
    if pw is None or pw == "":
        return None
    try:
        check_password_strength(str(pw), n)
    except ValueError as e:
        sys.exit(f"bad password for {n!r}: {e}")
    return str(pw)


def main(src="goblins.yaml", *flags):
    reset = setpw = None
    if src == "--reset-token":                      # python scripts/gen_compose.py --reset-token bob : new login token, nothing else changes
        reset, src = flags[0] if flags else sys.exit("usage: --reset-token <name>"), "goblins.yaml"
    elif src == "--set-password":                   # GOBLIN_NEW_PASSWORD=... python scripts/gen_compose.py --set-password bob
        setpw, src = flags[0] if flags else sys.exit("usage: --set-password <name>"), "goblins.yaml"
    spec = yaml.safe_load((ROOT / src).read_text())
    SEC.mkdir(exist_ok=True, mode=0o700)
    network = spec.get("network")
    if not network:
        sys.exit("set `network: <docker network name>` in goblins.yaml — an existing external network "
                  "(docker network create <name>) that goblin and connector containers join")
    names = [g["name"] for g in spec["goblins"]]
    if len(set(names)) != len(names):
        sys.exit("duplicate goblin names")
    gw_goblins, services, networks = [], {}, {"edge": {}, network: {"external": True}}
    for g in spec["goblins"]:
        n = g["name"]
        if not n.replace("-", "").isalnum() or not n.islower():
            sys.exit(f"bad goblin name {n!r} (lowercase alnum/hyphen)")
        f = SEC / f"{n}.env"
        kv = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l) if f.exists() else {}
        changed = False
        pw = _password_for(g, n, setpw)
        if setpw == n and not pw:
            sys.exit("set GOBLIN_NEW_PASSWORD to the new password (it is never read from a file)")
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
            f.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
            f.chmod(0o600)
        internal, connector = kv["INTERNAL_SECRET"], kv["CONNECTOR_SECRET"]
        _stub(SEC / f"{n}.integrations.env", "CALDAV_URL=\nCALDAV_USER=\nCALDAV_PASSWORD=\n")
        _stub(SEC / "plaid.env", "PLAID_CLIENT_ID=\nPLAID_SECRET=\nPLAID_ENV=sandbox\nPLAID_WEBHOOK_URL=\n")
        gw_goblins.append({"name": n, "url": f"http://goblin-{n}:8000", "internal_secret": internal,
                           **({"user_password_scrypt": kv["USER_PASSWORD_SCRYPT"]} if kv.get("USER_PASSWORD_SCRYPT") else {"user_token_sha256": kv["USER_TOKEN_SHA256"]})})
        for sub in ("db", "robinhood-csv", "rh-live-session", "imports"):
            d = ROOT / "data" / n / sub
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chown(d, 10001, 10001)    # the containers run as uid 10001
            except PermissionError:
                print(f"hint: sudo chown -R 10001:10001 data/{n}")
        networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
        env = {"ROLE": "goblin", "GOBLIN_NAME": n, "SHARE": ",".join(g.get("share", [])),
               "GATEWAY_URL": "http://gateway:8000"}
        services[f"goblin-{n}"] = {"build": ".", "env_file": [f".secrets/{n}.env", f".secrets/{n}.integrations.env", ".secrets/plaid.env"], "environment": env,
                                   "volumes": [f"./data/{n}/db:/data", f"./data/{n}/robinhood-csv:/data/robinhood-csv", f"./data/{n}/imports:/data/imports"], "user": "10001", "extra_hosts": ["host.docker.internal:host-gateway"],
                                   "networks": ["edge", network, "egress"], "restart": "unless-stopped"}
        conn_env = {"INTERNAL_SECRET": connector}
        if g.get("rh_agent"):
            _stub(SEC / f"{n}.rh-agent.env", "# Optional. Leave empty and set these on the Settings page instead.\n")
            env["RH_AGENT_URL"] = f"http://{n}-rh-agent:8000"
            services[f"{n}-rh-agent"] = {"build": ".", "env_file": [f".secrets/{n}.rh-agent.env"],
                                         "environment": {"ROLE": "rh-agent", "CONFIG_URL": f"http://goblin-{n}:8000/internal/connector-config/rh-agent", **conn_env}, "networks": [network, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
        if g.get("rh_live"):
            _stub(SEC / f"{n}.rh-live.env", "RH_USERNAME=\nRH_PASSWORD=\n")
            env["RH_LIVE_URL"] = f"http://{n}-rh-live:8000"
            services[f"{n}-rh-live"] = {"build": {"context": ".", "args": {"WITH_LIVE": "1"}},
                                        "env_file": [f".secrets/{n}.rh-live.env"],
                                        "environment": {"ROLE": "rh-live", "RH_LIVE_ENABLED": "1", **conn_env},
                                        "volumes": [f"./data/{n}/rh-live-session:/session"], "networks": [network, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {"ipam": {"config": [{"subnet": "10.89.1.0/24"}]}}
        env["CONNECTOR_SECRET"] = connector
    if reset and reset not in names:
        sys.exit(f"{reset!r} is not in goblins.yaml (people added on the admin page: remove and re-add them there)")
    orb = []
    if spec.get("orb_key"):
        of = SEC / "orb.key.sha256"
        if not of.exists():
            k = secrets.token_urlsafe(32)
            print(f"[orb] API key for Pondering Orb (shown once): {k}")
            of.write_text(sha256(k))
        orb = [{"sha256": of.read_text().strip(), "scopes": ["summary:read"]}]
    af = SEC / "admin.key.sha256"          # for the /admin page: general health and counts only
    pf = SEC / "admin.password"            # the admin password: random hex, kept here and shown on every run (the gateway only ever gets its hash)
    if not pf.exists() and not af.exists():
        pf.write_text(secrets.token_hex(16) + "\n")
        pf.chmod(0o600)
    if pf.exists():
        ap = pf.read_text().strip()
        if not af.exists() or af.read_text().strip() != sha256(ap):
            af.write_text(sha256(ap))
        print(f"[admin] admin password: {ap}   (saved in .secrets/admin.password)")
    else:
        print("[admin] an admin token from an earlier install is in use (only its hash is stored). Delete .secrets/admin.key.sha256 and run this again to get an admin password.")
    st = ROOT / "gateway-state"                                         # people added from the admin page live here
    st.mkdir(exist_ok=True)
    try:
        os.chown(st, 10001, 10001)
    except PermissionError:
        print("hint: sudo chown -R 10001:10001 gateway-state")
    (ROOT / "gateway.yaml").write_text(yaml.safe_dump({"goblins": gw_goblins, "orb_keys": orb, "admin_token_sha256": af.read_text().strip()}))
    (ROOT / "gateway.yaml").chmod(0o600)
    try:
        os.chown(ROOT / "gateway.yaml", 10001, 10001)      # the gateway container runs as uid 10001 and must be able to read it
    except PermissionError:
        print("hint: sudo chown 10001:10001 gateway.yaml   (the gateway container cannot read it otherwise)")
    services["gateway"] = {"build": ".", "environment": {"ROLE": "gateway", "GATEWAY_CONFIG": "/config/gateway.yaml", "UI_DIR": "/app/ui", "GATEWAY_STATE": "/state/people.json"}, "user": "10001",
                           "volumes": ["./gateway.yaml:/config/gateway.yaml:ro", "./gateway-state:/state"], "networks": ["edge"],
                           "ports": [f"127.0.0.1:{int(spec.get('port', 6868))}:8000"], "restart": "unless-stopped"}
    (ROOT / "docker-compose.generated.yml").write_text(yaml.safe_dump({"services": services, "networks": networks}, sort_keys=False))
    print(f"wrote gateway.yaml and docker-compose.generated.yml for {len(names)} goblin(s)")


if __name__ == "__main__":
    main(*sys.argv[1:])
