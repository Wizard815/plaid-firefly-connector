#!/usr/bin/env python3
"""Move an old Goblin Bank install into this plaid-proxy folder: people, logins, Plaid links, data, keys. Nothing is re-linked or re-entered.

The old folder is only ever READ (scripts/migrate_from_goblin_bank.sh mounts it read-only), so until you delete it you can always go back.
What it reads is what the old containers really ran with: their compose files (generated and any docker-compose.<name>.yml added from the
admin page), the env files those load, gateway.yaml and the gateway's saved state. Hand-moved data folders are found the same way.

    python3 scripts/migrate_from_goblin_bank.py check  OLD_DIR              # what would be moved, and any problem; changes nothing
    python3 scripts/migrate_from_goblin_bank.py copy   OLD_DIR [--data-dir D]
    python3 scripts/migrate_from_goblin_bank.py verify OLD_DIR              # after gen_compose.py: every key and login is the same as before

--host-root /host: the host's / is mounted there (how the .sh wrapper runs it inside Docker); host paths are looked up under it.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
UID = 10001
OLD_DB_NAMES = ("goblin.db",)
NEW_DB = "proxy.db"
WORKER_MOUNTS = {"/data": "db", "/data/robinhood-csv": "robinhood-csv", "/data/imports": "imports"}


class Stop(Exception):
    pass


class Old:
    """The old install, as its containers saw it."""

    def __init__(self, folder: str, host_root: str = ""):
        self.dir = Path(folder).resolve() if not host_root else Path(folder)
        self.host_root = host_root.rstrip("/")
        if not self.p(self.dir / "goblins.yaml").exists():
            raise Stop(f"{folder} has no goblins.yaml: that is not an old Goblin Bank folder")
        self.yaml_text = self.p(self.dir / "goblins.yaml").read_text()
        self.spec = yaml.safe_load(self.yaml_text) or {}
        self.yaml_people = [g["name"] for g in self.spec.get("goblins") or []]
        self.services: dict[str, dict] = {}
        self.compose_files = sorted(f for f in self.p(self.dir).glob("docker-compose*.yml") if f.is_file())
        if not any(f.name == "docker-compose.generated.yml" for f in self.compose_files):
            raise Stop("no docker-compose.generated.yml in the old folder, so I cannot tell how it was run")
        for f in [f for f in self.compose_files if f.name == "docker-compose.generated.yml"] + [f for f in self.compose_files if f.name != "docker-compose.generated.yml"]:
            for name, s in ((yaml.safe_load(f.read_text()) or {}).get("services") or {}).items():
                merged = self.services.setdefault(name, {"environment": {}, "env_file": [], "volumes": [], "ports": []})
                merged["ports"] += [str(x) for x in s.get("ports") or [] if isinstance(x, (str, int))]
                env = s.get("environment") or {}
                if isinstance(env, list):
                    env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
                merged["environment"].update({k: "" if v is None else str(v) for k, v in env.items()})
                ef = s.get("env_file") or []
                merged["env_file"] += [ef] if isinstance(ef, str) else [e if isinstance(e, str) else e.get("path") for e in ef]
                merged["volumes"] += [v for v in s.get("volumes") or [] if isinstance(v, str)]
        self.gateway_yaml = self._mounted("gateway", "/config/gateway.yaml") or self.dir / "gateway.yaml"
        self.gw = yaml.safe_load(self.p(self.gateway_yaml).read_text()) if self.p(self.gateway_yaml).exists() else {}
        self.state_dir = self._mounted("gateway", "/state") or self.dir / "gateway-state"
        sf = self.p(self.state_dir) / "people.json"
        self.state = json.loads(sf.read_text()) if sf.exists() else {}
        workers = [n[len("goblin-"):] for n in self.services if n.startswith("goblin-")]
        self.added_people = [n for n in workers if n not in self.yaml_people]          # people added on the admin page, run from their own compose file
        self.people = self.yaml_people + self.added_people

    def p(self, host_path) -> Path:
        """A host path, as this process can open it."""
        return Path(self.host_root + str(host_path)) if self.host_root else Path(host_path)

    def host(self, path: str) -> Path:
        """A path from a compose file (relative to the old folder) as a host path."""
        path = os.path.expanduser(path)
        return Path(path) if path.startswith("/") else Path(os.path.normpath(self.dir / path))

    def _mounted(self, service: str, target: str):
        for v in (self.services.get(service) or {}).get("volumes", []):
            parts = v.split(":")
            if len(parts) >= 2 and parts[1].rstrip("/") == target:
                return self.host(parts[0])
        return None

    def effective_env(self, service: str) -> dict:
        """What the container really got: its env files in order, then `environment:` on top (as Compose does)."""
        s = self.services.get(service) or {}
        env = {}
        for ef in s.get("env_file", []):
            f = self.p(self.host(ef))
            if f.exists():
                env.update(read_env(f.read_text()))
        env.update(s.get("environment", {}))
        return env

    def published(self):
        """(address, port) the old gateway really listened on, from its compose file (it may have been edited by hand)."""
        for spec in (self.services.get("gateway") or {}).get("ports", []):
            parts = spec.rsplit(":", 2)
            if len(parts) == 3 and parts[2] == "8000":
                return parts[0].strip("[]"), parts[1]
            if len(parts) == 2 and parts[1] == "8000":
                return "0.0.0.0", parts[0]
        return None

    def secrets_dir(self) -> Path:
        for ef in (self.services.get(f"goblin-{self.yaml_people[0]}") or {}).get("env_file", []) if self.yaml_people else []:
            if Path(ef).name == f"{self.yaml_people[0]}.env":
                return self.host(ef).parent
        return self.dir / ".secrets"

    def mounts(self, person: str) -> dict[str, Path]:
        out = {}
        for svc, wanted in ((f"goblin-{person}", WORKER_MOUNTS), (f"{person}-rh-live", {"/session": "rh-live-session"})):
            for target, sub in wanted.items():
                h = self._mounted(svc, target)
                if h:
                    out[sub] = h
        if "db" not in out:
            out["db"] = self.dir / "data" / person / "db"
        for sub in ("robinhood-csv", "imports", "rh-live-session"):
            out.setdefault(sub, self.dir / "data" / person / sub)
        return out

    def login(self, person: str) -> dict:
        """The login the gateway checked: a password changed in the app (state creds) wins over gateway.yaml, as in the gateway itself."""
        entry = next((g for g in self.gw.get("goblins") or [] if g.get("name") == person), None) \
            or next((g for g in self.state.get("people") or [] if g.get("name") == person), None) or {}
        out = {k: entry[k] for k in ("user_password_scrypt", "user_token_sha256") if entry.get(k)}
        if (self.state.get("creds") or {}).get(person):
            out = {"user_password_scrypt": self.state["creds"][person]}
        return out

    def internal_secret(self, person: str) -> str:
        entry = next((g for g in (self.gw.get("goblins") or []) + (self.state.get("people") or []) if g.get("name") == person), {})
        return entry.get("internal_secret", "")


def read_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def plan(old: Old) -> dict:
    """Every person's keys and folders, checked. Raises Stop on anything that would lose a login or a bank link."""
    problems, people = [], {}
    for n in old.people:
        env = old.effective_env(f"goblin-{n}")
        kv = {}
        f = old.p(old.secrets_dir() / f"{n}.env")
        if f.exists():
            kv.update(read_env(f.read_text()))
        for k in ("INTERNAL_SECRET", "CONNECTOR_SECRET", "FIELD_KEY"):          # what the running container had wins
            if env.get(k):
                kv[k] = env[k]
        if not kv.get("INTERNAL_SECRET"):
            kv["INTERNAL_SECRET"] = old.internal_secret(n)
        for live in (f"{n}-rh-agent", f"{n}-rh-live"):
            c = old.effective_env(live).get("INTERNAL_SECRET")
            if c and not kv.get("CONNECTOR_SECRET"):
                kv["CONNECTOR_SECRET"] = c
        made_connector = not kv.get("CONNECTOR_SECRET")
        if made_connector:
            kv["CONNECTOR_SECRET"] = secrets.token_urlsafe(32)             # people added on the admin page had none; only their own connectors would use it
        login = old.login(n)
        kv.pop("USER_PASSWORD_SCRYPT", None), kv.pop("USER_TOKEN_SHA256", None)
        if login.get("user_password_scrypt"):
            kv["USER_PASSWORD_SCRYPT"] = login["user_password_scrypt"]
        elif login.get("user_token_sha256"):
            kv["USER_TOKEN_SHA256"] = login["user_token_sha256"]
        else:
            problems.append(f"{n}: no login found (neither a password hash nor a token hash)")
        if not kv.get("INTERNAL_SECRET"):
            problems.append(f"{n}: no INTERNAL_SECRET found")
        gw_secret = old.internal_secret(n)
        if gw_secret and kv.get("INTERNAL_SECRET") and gw_secret != kv["INTERNAL_SECRET"]:
            problems.append(f"{n}: the gateway and the container disagree on INTERNAL_SECRET (the old install could not have worked for this person)")
        if not kv.get("FIELD_KEY"):
            problems.append(f"{n}: no FIELD_KEY found. It decrypts the Plaid links; without it every bank must be linked again")
        else:
            try:
                Fernet(kv["FIELD_KEY"].encode())
            except ValueError:
                problems.append(f"{n}: FIELD_KEY is not a valid key")
        for k, v in kv.items():
            if "$" in v and k != "USER_PASSWORD_SCRYPT":
                problems.append(f"{n}: {k} contains a $ (it would have been cut short by Docker Compose in the old install)")
        mounts = old.mounts(n)
        db_dir = old.p(mounts["db"])
        names = [Path(env["GOBLIN_DB"]).name] if env.get("GOBLIN_DB") else list(OLD_DB_NAMES)
        db = next((db_dir / d for d in names if (db_dir / d).exists()), None)
        if not db or not db.exists():
            problems.append(f"{n}: no database found in {mounts['db']}")
        people[n] = {"kv": kv, "mounts": mounts, "db": db, "added_here": n in old.added_people, "made_connector": made_connector}
    if problems:
        raise Stop("NOT SAFE TO MOVE YET:\n  " + "\n  ".join(problems))
    return people


def tokens_open(db: Path, key: str) -> tuple[int, int]:
    """How many encrypted Plaid links and secret settings there are, and how many this key opens. Reads a private copy:
    even a read-only SQLite open can write the -shm file next to a database, and the old folder must not change at all."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            if Path(str(db) + suffix).exists():
                shutil.copy2(str(db) + suffix, Path(tmp) / ("x.db" + suffix))
        return _tokens_open(Path(tmp) / "x.db", key)


def _tokens_open(db: Path, key: str) -> tuple[int, int]:
    con = sqlite3.connect(db)
    f = Fernet(key.encode())
    rows = []
    try:
        rows += [r[0] for r in con.execute("SELECT token_enc FROM plaid_items")]
    except sqlite3.Error:
        pass
    try:
        rows += [r[0] for r in con.execute("SELECT value FROM settings WHERE secret=1 AND value != ''")]
    except sqlite3.Error:
        pass
    con.close()
    ok = 0
    for r in rows:
        try:
            f.decrypt(r.encode())
            ok += 1
        except (InvalidToken, AttributeError):
            pass
    return len(rows), ok


def copy_tree(src: Path, dst: Path, skip: set[str] = frozenset()):
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir() if src.exists() else []:
        if item.name in skip:
            continue
        target = dst / item.name
        if item.is_dir() and not item.is_symlink():
            copy_tree(item, target)
        elif item.is_file():
            shutil.copy2(item, target)


def own(path: Path, uid=UID):
    for p in [path, *path.rglob("*")]:
        try:
            os.chown(p, uid, uid)
        except PermissionError:
            pass
        p.chmod(0o700 if p.is_dir() else 0o600)


def private(path: Path, text: str):
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def new_paths(data_dir: str | None):
    data = Path(data_dir) if data_dir else ROOT
    return data, (data / ".secrets"), data / "data", data / "gateway-state"


def copy(old: Old, data_dir: str | None):
    people = plan(old)
    data, sec, data_root, state = new_paths(data_dir)
    already = [p for p in (sec, data_root, state, ROOT / "people.yaml") if p.exists() and (p.is_file() or any(p.iterdir()))]
    if already:
        raise Stop("this folder already has " + ", ".join(str(p) for p in already) + ". Move them away first: the migration never overwrites anything")
    # people.yaml first, checked before anything is written: the same file with `goblins:` renamed, people added on the admin page now
    # listed in it, and the data folder if one was asked for
    text = re.sub(r"(?m)^goblins:", "people:", old.yaml_text)
    extra = "".join(f"  - name: {n}\n    share: []\n" for n in old.added_people)
    if extra:
        text = re.sub(r"(?m)^people:[^\n]*\n", lambda m: m.group(0) + extra, text, count=1)
    pub = old.published()
    if pub:                                                     # keep it reachable exactly where it was, even if the compose file was edited by hand
        for key, val in (("listen", pub[0]), ("port", pub[1])):
            if re.search(rf"(?m)^{key}:", text):
                text = re.sub(rf"(?m)^{key}:.*$", f"{key}: {val}", text)
            else:
                text = text.rstrip("\n") + f"\n{key}: {val}\n"
    if data_dir:
        text = text.rstrip("\n") + f"\ndata_dir: {data_dir}\n"
    if sorted(g["name"] for g in (yaml.safe_load(text) or {}).get("people", [])) != sorted(people):
        raise Stop("could not rewrite goblins.yaml cleanly; nothing was changed. Ask for help")
    data.mkdir(parents=True, exist_ok=True)
    sec.mkdir(mode=0o700, exist_ok=True)
    sec.chmod(0o700)
    # 1. secrets: everything the old folder kept (Plaid keys, CalDAV, Robinhood, admin password, orb key), then each person's keys as the containers had them
    copy_tree(old.p(old.secrets_dir()), sec, skip={f"{n}.env" for n in people})
    for n, info in people.items():
        private(sec / f"{n}.env", "".join(f"{k}={v}\n" for k, v in info["kv"].items()))
    own(sec, os.getuid())
    # 2. people.yaml
    private(ROOT / "people.yaml", text)
    # 3. each person's data: database (renamed to proxy.db), Robinhood CSVs, imports, Robinhood session
    for n, info in people.items():
        dst = data_root / n
        for sub, src in info["mounts"].items():
            src = old.p(src)
            nested = {"robinhood-csv", "imports"} if sub == "db" else set()
            copy_tree(src, dst / sub, skip=nested)
            for inner in nested:                                       # a hand-made compose that mounted only /data kept these inside it
                if (src / inner).is_dir() and not any(old.p(info["mounts"][inner]).glob("*")):
                    copy_tree(src / inner, dst / inner)
        dbdir, oldname = dst / "db", info["db"].name
        for suffix in ("", "-wal", "-shm", "-journal"):
            if (dbdir / (oldname + suffix)).exists():
                (dbdir / (oldname + suffix)).rename(dbdir / (NEW_DB + suffix))
        con = sqlite3.connect(dbdir / NEW_DB)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Stop(f"{n}: the copied database failed SQLite's integrity check. The old folder is untouched")
        con.close()
        (dbdir / (NEW_DB + "-shm")).unlink(missing_ok=True)
        own(dst)
        data_root.chmod(0o700)
    # 4. the gateway's saved state: people added on the admin page are now in people.yaml; passwords changed in the app, Plaid keys,
    #    Firefly III tokens, limits and Orb keys stay exactly as they were
    state.mkdir(exist_ok=True)
    copy_tree(old.p(old.state_dir), state)
    sf = state / "people.json"
    if sf.exists():
        st = json.loads(sf.read_text())
        st["people"] = [g for g in st.get("people") or [] if g.get("name") not in people]
        for g in st["people"]:
            g["url"] = re.sub(r"^http://goblin-", "http://worker-", g.get("url", ""))
        sf.unlink()
        private(sf, json.dumps(st))
    own(state)
    private(ROOT / ".migrated-from", f"{old.dir}\n")
    for n, info in people.items():
        total, ok = tokens_open(data_root / n / "db" / NEW_DB, info["kv"]["FIELD_KEY"])
        print(f"[{n}] moved: database, {ok}/{total} encrypted bank links and secrets open with the moved key"
              + ("; was added on the admin page, now listed in people.yaml" if info["added_here"] else ""))
        if ok != total:
            raise Stop(f"{n}: {total - ok} encrypted items do not open with the moved key")


def verify(old: Old, data_dir: str | None):
    """After gen_compose.py: everything that signs anyone or anything in is what it was."""
    people = plan(old)
    data, sec, data_root, state = new_paths(data_dir or (yaml.safe_load((ROOT / "people.yaml").read_text()) or {}).get("data_dir"))
    gw = yaml.safe_load((data / "gateway.yaml").read_text())
    new = {g["name"]: g for g in gw["people"]}
    creds = (json.loads((state / "people.json").read_text()) if (state / "people.json").exists() else {}).get("creds") or {}
    bad = []
    for n, info in people.items():
        g = new.get(n) or {}
        if g.get("internal_secret") != info["kv"]["INTERNAL_SECRET"]:
            bad.append(f"{n}: internal secret changed")
        now = {"user_password_scrypt": creds[n]} if creds.get(n) else {k: g[k] for k in ("user_password_scrypt", "user_token_sha256") if g.get(k)}
        if now != old.login(n):                                         # what the gateway will check, against what it checked before
            bad.append(f"{n}: login changed")
        w = read_env((sec / f"{n}.worker.env").read_text().replace("$$", "$"))
        for k in ("INTERNAL_SECRET", "CONNECTOR_SECRET", "FIELD_KEY"):
            if k == "CONNECTOR_SECRET" and info["made_connector"]:          # new for this person: it only has to match what was written for them
                if w.get(k) != read_env((sec / f"{n}.env").read_text()).get(k):
                    bad.append(f"{n}: {k} differs between files")
            elif w.get(k) != info["kv"][k]:
                bad.append(f"{n}: {k} changed")
        total, ok = tokens_open(data_root / n / "db" / NEW_DB, w.get("FIELD_KEY", ""))
        if ok != total:
            bad.append(f"{n}: {total - ok} encrypted items do not open")
    if old.gw.get("admin_token_sha256") and gw.get("admin_token_sha256") != old.gw["admin_token_sha256"]:
        bad.append("the admin password changed")
    if [k.get("sha256") for k in old.gw.get("orb_keys") or []] != [k.get("sha256") for k in gw.get("orb_keys") or []]:
        bad.append("the Pondering Orb key changed")
    if bad:
        raise Stop("VERIFY FAILED:\n  " + "\n  ".join(bad))
    print(f"verified: {len(people)} people, same logins, same internal secrets, same encryption keys, admin password and Orb key unchanged")


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("action", choices=["check", "copy", "verify"])
    a.add_argument("old")
    a.add_argument("--data-dir")
    a.add_argument("--host-root", default="")
    args = a.parse_args(argv)
    try:
        old = Old(args.old, args.host_root)
        if args.action == "check":
            people = plan(old)
            print(f"old folder: {old.dir}")
            for n, info in people.items():
                total, ok = tokens_open(info["db"], info["kv"]["FIELD_KEY"])
                print(f"  {n}: database {info['db'].name}, {ok}/{total} encrypted bank links and secrets open, login: "
                      + ("password" if "USER_PASSWORD_SCRYPT" in info["kv"] else "token") + (", added on the admin page" if info["added_here"] else ""))
                if ok != total:
                    raise Stop(f"{n}: {total - ok} encrypted items do not open with the key the container had")
            extra = sorted({k for s, v in old.services.items() for k in v["environment"]} - {"ROLE", "GOBLIN_NAME", "SHARE", "GATEWAY_URL", "INTERNAL_SECRET", "CONNECTOR_SECRET",
                           "FIELD_KEY", "RH_AGENT_URL", "RH_LIVE_URL", "CONFIG_URL", "RH_LIVE_ENABLED", "GATEWAY_CONFIG", "UI_DIR", "GATEWAY_STATE"})
            if extra:
                print("  note: the old compose files also set " + ", ".join(extra) + " (values not shown). They are not carried over; set them again if you need them.")
            print("  ready to move")
        elif args.action == "copy":
            copy(old, args.data_dir)
        else:
            verify(old, args.data_dir)
    except Stop as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
