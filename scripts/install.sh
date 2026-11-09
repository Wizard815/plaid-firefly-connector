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
  # Nothing to edit: start with one person. Add more, and enter your Plaid keys, in the app afterwards.
  NAME_=${NAME:-me}
  printf 'goblins:\n  - name: %s\n    share: []\norb_key: false\n' "$NAME_" > goblins.yaml
  echo "Created goblins.yaml with one person: $NAME_ (add more people later on the gateway's /admin page)."
fi

echo "Building the image (first time takes a few minutes)..."
[ "${SKIP_BUILD:-}" = 1 ] || docker build -q -t goblin-bank .

echo "Generating secrets and the compose file..."
docker run --rm -u 0 -v "$PWD":/work -w /work -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" --entrypoint sh goblin-bank -c '
  python scripts/gen_compose.py "$@" &&
  chown -R "$HOST_UID:$HOST_GID" .secrets docker-compose.generated.yml &&
  chmod 600 .secrets/*' sh ${ARGS[@]+"${ARGS[@]}"}

PORT=$(sed -n 's/^port: *\([0-9]*\).*/\1/p' goblins.yaml | head -1); PORT=${PORT:-6767}
cat <<MSG

Done. The login tokens and the admin token above are shown ONCE. Save them now.

Start everything:
  docker compose -f docker-compose.generated.yml up -d

Then open http://127.0.0.1:$PORT and sign in with a name from goblins.yaml and its token.
The admin page is at http://127.0.0.1:$PORT/admin (use the admin token). Everything else is set up in the app, no files to edit:
  - admin page: add people, enter the Plaid keys for everyone, set the public address for webhooks, set the budget
  - or, signed in as a person: Settings, Connect & import, Plaid keys
For other devices and HTTPS, read "Reach it from your phone" in the README.
MSG
