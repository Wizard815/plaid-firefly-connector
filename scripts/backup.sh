#!/usr/bin/env bash
# Safe backup while running: copies every goblin database with SQLite's own backup (consistent even mid-write),
# then bundles it with the secrets and config into backups/goblin-bank-<date>.tar.gz (owner-only).
# KEEP THE RESULT PRIVATE AND OFF THIS MACHINE: it contains the keys that decrypt your bank tokens.
set -euo pipefail
cd "$(dirname "$0")/.."
stamp=$(date +%Y%m%d-%H%M%S); work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p backups "$work/data"; umask 077
for db in data/*/db/*.db; do
  [ -e "$db" ] || continue
  dest="$work/$db"; mkdir -p "$(dirname "$dest")"
  python3 - "$db" "$dest" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
done
cp -r .secrets "$work/" 2>/dev/null || true
cp gateway.yaml goblins.yaml "$work/" 2>/dev/null || true
cp -r gateway-state "$work/" 2>/dev/null || true
tar -czf "backups/goblin-bank-$stamp.tar.gz" -C "$work" .
chmod 600 "backups/goblin-bank-$stamp.tar.gz"
echo "Wrote backups/goblin-bank-$stamp.tar.gz"
