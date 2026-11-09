#!/usr/bin/env bash
# One-time setup. Needs only Docker (with the compose plugin). Run from the repository folder:  ./scripts/install.sh
# It builds the image, creates your config and secrets (inside Docker, so no Python is needed on the host) and tells you what to do next.
set -euo pipefail
cd "$(dirname "$0")/.."
ARGS=(); YES=0
for a in "$@"; do if [ "$a" = "--yes" ]; then YES=1; else ARGS+=("$a"); fi; done

command -v docker >/dev/null || { echo "Docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The Docker compose plugin is missing. See https://docs.docker.com/compose/install/"; exit 1; }

if [ ! -f goblins.yaml ]; then
  cp goblins.example.yaml goblins.yaml
  echo "Created goblins.yaml from the example. Edit it now to list your people (names), then run this script again."
  echo "  (You can skip editing: just add people later on the gateway's /admin page.)"
  [ "$YES" = 1 ] || exit 0
fi

echo "Building the image (first time takes a few minutes)..."
[ "${SKIP_BUILD:-}" = 1 ] || docker build -q -t goblin-bank .

echo "Generating secrets and the compose file..."
docker run --rm -u 0 -v "$PWD":/work -w /work -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" --entrypoint sh goblin-bank -c '
  python scripts/gen_compose.py "$@" &&
  chown -R "$HOST_UID:$HOST_GID" .secrets docker-compose.generated.yml &&
  chmod 600 .secrets/*' sh ${ARGS[@]+"${ARGS[@]}"}

for f in .secrets/plaid.env; do
  [ -f "$f" ] && ! grep -q '^PLAID_CLIENT_ID=.\+' "$f" && echo "NEXT: put your Plaid keys in $f (see examples/plaid.env.example). You can start without them and add them later."
done
cat <<MSG

Done. The login tokens and the admin token above are shown ONCE. Save them now.

Start everything:
  docker compose -f docker-compose.generated.yml up -d

Then open http://127.0.0.1:8080 and sign in with a name from goblins.yaml and its token.
The admin page (add people, health) is at http://127.0.0.1:8080/admin.
For other devices and HTTPS, read "Reach it from your phone" in the README.
MSG
