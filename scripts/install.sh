#!/usr/bin/env bash
# One-time setup. Needs only Docker (with the compose plugin). Run from the repository folder:  ./scripts/install.sh
# It builds the image, creates your config and secrets (inside Docker, so no Python is needed on the host) and tells you what to do next.
set -euo pipefail
cd "$(dirname "$0")/.."
ARGS=(); YES=0
for a in "$@"; do if [ "$a" = "--yes" ]; then YES=1; else ARGS+=("$a"); fi; done

command -v docker >/dev/null || { echo "Docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The Docker compose plugin is missing. See https://docs.docker.com/compose/install/"; exit 1; }

ask_password() {   # $1 = who it is for. Sets PASSWORD (empty = none). The password is only ever held in memory: it is hashed inside Docker and written nowhere.
  PASSWORD=""
  [ -t 0 ] && [ "${NONINTERACTIVE:-}" != 1 ] || return 0
  while true; do
    read -rsp "Choose a password for $1 (12 or more characters; press Enter for a random login token instead): " p1; echo
    [ -z "$p1" ] && return 0
    if [ "${#p1}" -lt 12 ]; then echo "  That is too short: use at least 12 characters (a short phrase of a few words works well)."; continue; fi
    read -rsp "Type it again: " p2; echo
    [ "$p1" = "$p2" ] && { PASSWORD="$p1"; return 0; }
    echo "  They did not match, try again."
  done
}

if [ "${ARGS[0]:-}" = "--set-password" ]; then
  [ -n "${ARGS[1]:-}" ] || { echo "usage: ./scripts/install.sh --set-password NAME"; exit 1; }
  NONINTERACTIVE=0 ask_password "${ARGS[1]}"
  [ -n "$PASSWORD" ] || { echo "No password entered; nothing changed."; exit 1; }
  export PROXY_NEW_PASSWORD="$PASSWORD"
fi

if [ ! -f people.yaml ]; then
  # Nothing to edit: one person to start with (you are asked for a name and a password). Add more people, and enter your Plaid keys, in the app afterwards.
  NAME_=${NAME:-}
  if [ -z "$NAME_" ] && [ -t 0 ] && [ "${NONINTERACTIVE:-}" != 1 ]; then read -rp "Your name for signing in (lowercase letters, digits, hyphens) [me]: " NAME_; fi
  NAME_=${NAME_:-me}
  NETWORK_=${NETWORK:-plaid-proxy}
  docker network inspect "$NETWORK_" >/dev/null 2>&1 || docker network create "$NETWORK_" >/dev/null
  printf 'people:\n  - name: %s\n    share: []\nnetwork: %s\norb_key: false\n' "$NAME_" "$NETWORK_" > people.yaml
  echo "Created people.yaml with one person: $NAME_ (add more people later on the gateway's /admin page)."
  ask_password "$NAME_"
  if [ -n "$PASSWORD" ]; then export "PROXY_PASSWORD_$(printf '%s' "$NAME_" | tr 'a-z-' 'A-Z_')=$PASSWORD"; fi
fi
unset PASSWORD p1 p2 2>/dev/null || true

echo "Building the image (first time takes a few minutes)..."
[ "${SKIP_BUILD:-}" = 1 ] || docker build -q -t plaid-proxy .

echo "Generating secrets and the compose file..."
# Where live data goes (data_dir: in people.yaml, or PLAID_PROXY_DATA_DIR): default this folder. An outside folder is mounted at the same path.
DATA=${PLAID_PROXY_DATA_DIR:-$(sed -n 's/^data_dir: *["'"'"']*\([^"'"'"' #]*\).*/\1/p' people.yaml | head -1)}
DATA_MOUNT=(); SECRETS=.secrets
if [ -n "$DATA" ]; then
  case "$DATA" in /*) ;; *) DATA="$PWD/$DATA" ;; esac
  mkdir -p "$DATA"; DATA_MOUNT=(-v "$DATA":"$DATA" -e PLAID_PROXY_DATA_DIR="$DATA"); SECRETS="$DATA/.secrets"
fi
docker run --rm -u 0 -v "$PWD":/work -w /work "${DATA_MOUNT[@]}" -e SECRETS="$SECRETS" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" $(env | sed -n 's/^\(PROXY_[A-Z0-9_]*PASSWORD[A-Z0-9_]*\)=.*/-e \1/p' | tr '\n' ' ') --entrypoint sh plaid-proxy -c '
  python scripts/gen_compose.py "$@" &&
  chown -R "$HOST_UID:$HOST_GID" "$SECRETS" docker-compose.generated.yml &&
  chmod 700 "$SECRETS" && chmod 600 "$SECRETS"/*' sh ${ARGS[@]+"${ARGS[@]}"}

PORT=$(sed -n 's/^port: *\([0-9]*\).*/\1/p' people.yaml | head -1); PORT=${PORT:-6421}
LISTEN=$(sed -n 's/^listen: *\([0-9.:a-fA-F]*\).*/\1/p' people.yaml | head -1); LISTEN=${LISTEN:-127.0.0.1}
[ "$LISTEN" = 127.0.0.1 ] && WHERE="on this machine only (to open it from other devices on your network, add 'listen: 0.0.0.0' to people.yaml and run this script again)" || WHERE="on $LISTEN"
cat <<MSG

Done. Any random login token printed above is shown ONCE, so save it now. People who chose a password sign in with it (only a salted hash is stored).
The admin password is a random hex string saved in $SECRETS/admin.password and shown every time you run this script.

Start everything:
  docker compose -f docker-compose.generated.yml up -d

Then open http://127.0.0.1:$PORT and sign in with your name and password. The app is reachable $WHERE.
The admin page is at http://127.0.0.1:$PORT/admin (use the admin password). Everything else is set up in the app, no files to edit:
  - admin page: add people, enter the Plaid keys for everyone, set the public address for webhooks, set the budget
  - or, signed in as a person: Settings, Connect & import, Plaid keys
For other devices and HTTPS, read "Reach it from your phone" in the README.
MSG
