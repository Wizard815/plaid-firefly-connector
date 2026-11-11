#!/usr/bin/env bash
# Upgrade an existing plaid-firefly-connector install (the old full-UI version) to the plaid-firefly-connector router, KEEPING all data, logins and Plaid links.
# Run it inside the old folder (the one with people.yaml, data/ and .secrets/):   bash upgrade_from_full_ui.sh
# What it does: stops the containers, makes a full private copy of everything outside the folder, points git at the renamed repository,
# pulls the new version, rebuilds, starts, and checks the admin page answers. It never deletes data and never touches Firefly III.
set -euo pipefail
NEW_URL=${NEW_URL:-https://github.com/Wizard815/plaid-firefly-connector.git}
COMPOSE=${COMPOSE:-docker-compose.generated.yml}
cd "${1:-.}"

[ -f people.yaml ] && [ -d data ] || { echo "This does not look like your install folder (no people.yaml and data/ here). cd into it first."; exit 1; }
[ -f "$COMPOSE" ] || { echo "No $COMPOSE here, so I cannot tell how it was started. Stop here and ask for help."; exit 1; }
command -v docker >/dev/null || { echo "Docker is needed."; exit 1; }
command -v git >/dev/null || { echo "git is needed."; exit 1; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "You have local edits to tracked files. Commit or stash them first:"; git status --short --untracked-files=no; exit 1
fi

echo "1/6 Stopping the containers so the copy is consistent (nothing is deleted)..."
docker compose -f "$COMPOSE" stop

echo "2/6 Backing everything up OUTSIDE this folder..."
stamp=$(date +%Y%m%d-%H%M%S); dest="$(cd .. && pwd)/plaid-proxy-backup-$stamp.tar.gz"
umask 077
tar -czf "$dest" data .secrets people.yaml gateway.yaml gateway-state "$COMPOSE" 2>/dev/null || tar -czf "$dest" data .secrets people.yaml "$COMPOSE"
chmod 600 "$dest"
tar -tzf "$dest" >/dev/null && echo "    Backup written and readable: $dest"
echo "    KEEP IT PRIVATE: it holds the keys that decrypt your bank links. To roll back: stop, untar it over this folder, git checkout the old commit, rebuild."

echo "3/6 Pointing git at the renamed repository..."
git remote set-url origin "$NEW_URL"
git fetch origin main full-ui
git branch -f full-ui-old-backup HEAD 2>/dev/null || true       # a local bookmark of exactly what you ran until now

echo "4/6 Pulling the new version (the old per-person screens stay on the 'full-ui' branch)..."
git checkout main
git merge --ff-only origin/main || { echo "Could not fast-forward. Your folder has its own history. Do not force it: ask for help."; docker compose -f "$COMPOSE" up -d; exit 1; }

echo "5/6 Rebuilding and starting (database upgrades itself on first start; old tables are kept)..."
docker build -q -t plaid-proxy .
docker compose -f "$COMPOSE" build
docker compose -f "$COMPOSE" up -d

echo "6/6 Checking..."
PORT=$(sed -n 's/^port: *\([0-9]*\).*/\1/p' people.yaml | head -1); PORT=${PORT:-6421}
ok=0
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/admin" || true)
  [ "$code" = 200 ] && { ok=1; break; }
  sleep 2
done
docker compose -f "$COMPOSE" ps
if [ "$ok" = 1 ]; then echo "The admin page answers at http://127.0.0.1:$PORT/admin"; else echo "The admin page did not answer. Run: docker compose -f $COMPOSE logs --tail 50   (your data is safe; the backup is $dest)"; exit 1; fi
cat <<MSG

Done. Your people, Plaid links, transactions and passwords are as they were.
What changed: the old screens are gone from '/'. Everything is done on the admin page now (password: .secrets/admin.password).
NEXT, on /admin:
  1. Manage each person -> Firefly III: enter that person's Firefly address and Personal Access Token, Save, then "Test the connection".
  2. Every existing bank account now says "Needs approval": nothing has been sent anywhere. Choose per account: Create in Firefly III / Link to an existing Firefly account / Do not send.
  3. Press "Send now". Joint accounts: set "Joint with" on the account.
MSG
