#!/usr/bin/env bash
# Safe backup while running: copies every worker database with SQLite's own backup (consistent even mid-write),
# then bundles it with the secrets and config into backups/plaid-proxy-<date>.tar.gz (owner-only).
# KEEP THE RESULT PRIVATE AND OFF THIS MACHINE: it contains the keys that decrypt your bank tokens.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$PWD/backups"
DATA=${PLAID_PROXY_DATA_DIR:-$(sed -n 's/^data_dir: *["'"'"']*\([^"'"'"' #]*\).*/\1/p' people.yaml 2>/dev/null | head -1)}
cp people.yaml "${TMPDIR:-/tmp}/.pp-people.$$" 2>/dev/null || true
[ -n "$DATA" ] && cd "$DATA"          # the live data (data/, .secrets/, gateway-state/, gateway.yaml) lives there
stamp=$(date +%Y%m%d-%H%M%S); work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$OUT" "$work/data"; umask 077
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
cp gateway.yaml "$work/" 2>/dev/null || true
mv "${TMPDIR:-/tmp}/.pp-people.$$" "$work/people.yaml" 2>/dev/null || true
cp -r gateway-state "$work/" 2>/dev/null || true
tar -czf "$OUT/plaid-proxy-$stamp.tar.gz" -C "$work" .
chmod 600 "$OUT/plaid-proxy-$stamp.tar.gz"
echo "Wrote $OUT/plaid-proxy-$stamp.tar.gz"
