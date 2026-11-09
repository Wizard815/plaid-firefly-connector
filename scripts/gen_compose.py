#!/usr/bin/env python3
"""Generate secrets, gateway.yaml and docker-compose.generated.yml from goblins.yaml.

goblins.yaml:
  goblins:
    - name: alice
      share: [robinhood_agent]     # what the Orb/gateway may see
      rh_agent: true               # run the official-MCP connector
      rh_live: false               # run the unofficial live connector (off by default)
  orb_key: true                    # create a read-only key for Pondering Orb

User tokens / orb key are printed ONCE; only their sha256 is stored.
"""
import os
import secrets
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from goblinbank.security import sha256  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEC = ROOT / ".secrets"


def _stub(path, text):
    if not path.exists():
        path.write_text(text)
        path.chmod(0o600)
        print(f"created {path.name} (optional: everything in it can be entered in the app instead)")


def main(src="goblins.yaml", *flags):
    reset = None
    if src == "--reset-token":                      # python scripts/gen_compose.py --reset-token bob : new login token, nothing else changes
        reset, src = flags[0] if flags else sys.exit("usage: --reset-token <name>"), "goblins.yaml"
    spec = yaml.safe_load((ROOT / src).read_text())
    SEC.mkdir(exist_ok=True, mode=0o700)
    names = [g["name"] for g in spec["goblins"]]
    if len(set(names)) != len(names):
        sys.exit("duplicate goblin names")
    gw_goblins, services, networks = [], {}, {"edge": {}}
    for g in spec["goblins"]:
        n = g["name"]
        if not n.replace("-", "").isalnum() or not n.islower():
            sys.exit(f"bad goblin name {n!r} (lowercase alnum/hyphen)")
        f = SEC / f"{n}.env"
        kv = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l) if f.exists() else {}
        changed = False
        if reset == n and kv:
            token = secrets.token_urlsafe(32)
            kv["USER_TOKEN_SHA256"] = sha256(token)
            print(f"[{n}] NEW login token (shown once): {token}")
            changed = True
        if "INTERNAL_SECRET" not in kv:
            kv["INTERNAL_SECRET"], kv["CONNECTOR_SECRET"] = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            token = secrets.token_urlsafe(32)
            kv["USER_TOKEN_SHA256"] = sha256(token)
            print(f"[{n}] login token (shown once): {token}")
            changed = True
        if "FIELD_KEY" not in kv:          # encrypts Plaid tokens at rest; LOSE THIS AND LINKS MUST BE REDONE
            kv["FIELD_KEY"] = Fernet.generate_key().decode()
            changed = True
        if changed:
            f.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
            f.chmod(0o600)
        internal, connector, tok_hash = kv["INTERNAL_SECRET"], kv["CONNECTOR_SECRET"], kv["USER_TOKEN_SHA256"]
        _stub(SEC / f"{n}.integrations.env", "CALDAV_URL=\nCALDAV_USER=\nCALDAV_PASSWORD=\n")
        _stub(SEC / "plaid.env", "PLAID_CLIENT_ID=\nPLAID_SECRET=\nPLAID_ENV=sandbox\nPLAID_WEBHOOK_URL=\n")
        gw_goblins.append({"name": n, "url": f"http://goblin-{n}:8000", "internal_secret": internal, "user_token_sha256": tok_hash})
        for sub in ("db", "robinhood-csv", "rh-live-session", "imports"):
            d = ROOT / "data" / n / sub
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chown(d, 10001, 10001)    # the containers run as uid 10001
            except PermissionError:
                print(f"hint: sudo chown -R 10001:10001 data/{n}")
        net = f"net-{n}"
        networks["egress"] = {}
        networks[net] = {"internal": True}  # connectors sit on an internal-only network
        env = {"ROLE": "goblin", "GOBLIN_NAME": n, "SHARE": ",".join(g.get("share", [])),
               "GATEWAY_URL": "http://gateway:8000", "RH_CSV_URL": f"http://{n}-rh-csv:8000"}
        services[f"goblin-{n}"] = {"build": ".", "env_file": [f".secrets/{n}.env", f".secrets/{n}.integrations.env", ".secrets/plaid.env"], "environment": env,
                                   "volumes": [f"./data/{n}/db:/data", f"./data/{n}/robinhood-csv:/data/robinhood-csv", f"./data/{n}/imports:/data/imports"], "user": "10001", "extra_hosts": ["host.docker.internal:host-gateway"],
                                   "networks": ["edge", net, "egress"], "restart": "unless-stopped"}
        conn_env = {"INTERNAL_SECRET": connector}
        services[f"{n}-rh-csv"] = {"build": ".", "environment": {"ROLE": "rh-csv", "CSV_DIR": "/csv", **conn_env},
                                   "volumes": [f"./data/{n}/robinhood-csv:/csv:ro"], "networks": [net], "restart": "unless-stopped"}
        if g.get("rh_agent"):
            _stub(SEC / f"{n}.rh-agent.env", "# Optional. Leave empty and set these on the Settings page instead.\n")
            env["RH_AGENT_URL"] = f"http://{n}-rh-agent:8000"
            services[f"{n}-rh-agent"] = {"build": ".", "env_file": [f".secrets/{n}.rh-agent.env"],
                                         "environment": {"ROLE": "rh-agent", "CONFIG_URL": f"http://goblin-{n}:8000/internal/connector-config/rh-agent", **conn_env}, "networks": [net, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {}
        if g.get("rh_live"):
            _stub(SEC / f"{n}.rh-live.env", "RH_USERNAME=\nRH_PASSWORD=\n")
            env["RH_LIVE_URL"] = f"http://{n}-rh-live:8000"
            services[f"{n}-rh-live"] = {"build": {"context": ".", "args": {"WITH_LIVE": "1"}},
                                        "env_file": [f".secrets/{n}.rh-live.env"],
                                        "environment": {"ROLE": "rh-live", "RH_LIVE_ENABLED": "1", **conn_env},
                                        "volumes": [f"./data/{n}/rh-live-session:/session"], "networks": [net, "egress"], "restart": "unless-stopped"}
            networks["egress"] = {}
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
    if not af.exists():
        ak = secrets.token_urlsafe(32)
        print(f"[admin] admin token for /admin (shown once): {ak}")
        af.write_text(sha256(ak))
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
                           "ports": [f"127.0.0.1:{int(spec.get('port', 6767))}:8000"], "restart": "unless-stopped"}
    (ROOT / "docker-compose.generated.yml").write_text(yaml.safe_dump({"services": services, "networks": networks}, sort_keys=False))
    print(f"wrote gateway.yaml and docker-compose.generated.yml for {len(names)} goblin(s)")


if __name__ == "__main__":
    main(*sys.argv[1:])
