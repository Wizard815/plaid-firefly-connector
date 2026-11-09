#!/usr/bin/env python3
"""Run gateway + goblin (random seeded data) + a fake model server locally, for clicking around or UI tests.
    python3 scripts/dev_stack.py      -> http://127.0.0.1:9204  (goblin 'alice', token 'login-alice')"""
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import httpx
import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="goblin-dev-"))
    key = Fernet.generate_key().decode()
    os.environ.update({"FIELD_KEY": key, "GOBLIN_NAME": "alice", "INTERNAL_SECRET": "int"})
    from devtools.seed import seed
    from goblinbank.db import DB
    db = DB(str(tmp / "goblin.db"))
    seed(db, 11, date.today())
    db.conn.execute("UPDATE accounts SET subtype='savings', name='Axos High Yield', institution='Axos' WHERE id='sav'")      # a savings account Plaid would label for us
    db.conn.commit()
    db.conn.close()
    (tmp / "gw.yaml").write_text(yaml.safe_dump({"goblins": [{"name": "alice", "url": "http://127.0.0.1:9203", "internal_secret": "int", "user_token_sha256": sha("login-alice")}], "orb_keys": [{"sha256": sha("orb-dev"), "scopes": ["summary:read"]}], "admin_token_sha256": sha("admin-dev")}))
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "devtools.fake_llm_server:app", "--port", "9202", "--log-level", "warning", "--no-access-log"], env=env, cwd=ROOT),
             subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "goblinbank.main:app", "--port", "9203", "--log-level", "warning", "--no-access-log"], cwd=ROOT,
                              env={**env, "GATEWAY_URL": "http://127.0.0.1:9204", "ROLE": "goblin", "GOBLIN_DB": str(tmp / "goblin.db"), "RH_CSV_DIR": str(tmp / "csv"), "IMPORT_DIR": str(tmp / "imp")}),
             subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "goblinbank.main:app", "--port", "9204", "--log-level", "warning", "--no-access-log"], cwd=ROOT,
                              env={**env, "ROLE": "gateway", "GATEWAY_CONFIG": str(tmp / "gw.yaml"), "UI_DIR": str(ROOT / "ui")})]
    for port in (9203, 9204):
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
    # a local model for the agent
    httpx.post("http://127.0.0.1:9204/g/alice/api/agent/providers", headers={"Authorization": "Bearer login-alice"},
               json={"name": "Fake local model", "kind": "openai", "base_url": "http://127.0.0.1:9202/v1", "model": "fake", "tools": True})
    httpx.put("http://127.0.0.1:9204/g/alice/api/settings", headers={"Authorization": "Bearer login-alice"}, json={"share": ["net_worth", "bills", "bill_list", "alerts", "alert_list", "savings", "goals", "spending"]})
    print("ready http://127.0.0.1:9204  goblin=alice token=login-alice", flush=True)
    try:
        procs[-1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
