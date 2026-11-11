#!/usr/bin/env bash
# Move an old Goblin Bank install into THIS folder (a fresh clone of plaid-firefly-connector) in one go. Needs only Docker.
#
#   ./scripts/migrate_from_goblin_bank.sh /mnt/user/appdata/goblin-bank                     # move everything over and start plaid-proxy
#   ./scripts/migrate_from_goblin_bank.sh /mnt/user/appdata/goblin-bank --data-dir /mnt/user/appdata/plaid-proxy-data
#   ./scripts/migrate_from_goblin_bank.sh --remove-old                                       # later, once you are happy: back up and delete the old folder
#
# Same people, same passwords and login tokens, same admin password, same Pondering Orb key, same API tokens, same Plaid links (nothing to re-link),
# same Firefly III tokens and account links, same data. The old folder is mounted READ-ONLY while copying and is never changed, so if anything
# fails the old containers are started again automatically and you are where you began.
set -euo pipefail
cd "$(dirname "$0")/.."
NEW="$(pwd -P)"
IMAGE=plaid-proxy
say() { printf '\n== %s\n' "$*"; }

command -v docker >/dev/null || { echo "Docker is needed."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The Docker compose plugin is needed."; exit 1; }

project_of() {   # the compose project(s) the old folder's containers belong to (Compose labels every container with its folder)
  docker ps -a --format '{{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.project.working_dir"}}' \
    | awk -v a="$1" -v b="$2" '$2 == a || $2 == b {print $1}' | sort -u
}

if [ "${1:-}" = "--remove-old" ]; then
  [ -f .migrated-from ] || { echo "No migration was done from this folder (.migrated-from is missing). Nothing to remove."; exit 1; }
  OLD=$(sed -n 1p .migrated-from); OLDPROJ=$(sed -n 2p .migrated-from)
  [ -d "$OLD" ] || { echo "$OLD is already gone."; exit 0; }
  [ -f "$OLD/goblins.yaml" ] || { echo "$OLD does not look like the old Goblin Bank folder. Not touching it."; exit 1; }
  bad=$(docker compose -f docker-compose.generated.yml ps --format '{{.Service}} {{.State}}' | awk '$2 != "running"')
  [ -z "$bad" ] && docker compose -f docker-compose.generated.yml ps --format '{{.Service}}' | grep -q gateway || { echo "plaid-proxy is not fully running here, so the old folder is kept:"; echo "$bad"; exit 1; }
  if [ "${2:-}" != "--yes" ]; then
    read -rp "Delete $OLD for good (a private backup copy is made first)? Type DELETE: " ok
    [ "$ok" = DELETE ] || { echo "Nothing deleted."; exit 1; }
  fi
  stamp=$(date +%Y%m%d-%H%M%S); dest="$(dirname "$NEW")/goblin-bank-final-backup-$stamp.tar.gz"
  say "Backing up $OLD to $dest (owner-only: it holds the keys to your bank links)"
  (umask 077; tar -czf "$dest" -C "$(dirname "$OLD")" "$(basename "$OLD")")
  chmod 600 "$dest"; tar -tzf "$dest" >/dev/null
  say "Removing the old containers' images"
  # only the old images by name (<project>-goblin-<name>, goblin-bank): the new stack may use the same project name, so never by project label
  pat='(^goblin-bank:|-goblin-[a-z0-9-]+:)'
  for p in $OLDPROJ; do [ "$p" = plaid-proxy ] || pat="$pat|^$p-"; done
  docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "$pat" | xargs -r docker rmi >/dev/null 2>&1 || true
  say "Deleting $OLD"
  rm -rf -- "$OLD"
  echo "Done. The old Goblin Bank is gone; the backup is $dest. Keep it private, and delete it once you no longer want a way back."
  exit 0
fi

OLD_IN=${1:-}; [ -n "$OLD_IN" ] || { echo "usage: $0 /path/to/old/goblin-bank [--data-dir /path]   |   $0 --remove-old"; exit 1; }
shift
DATA=""
while [ $# -gt 0 ]; do case "$1" in --data-dir) DATA=${2:?}; shift 2 ;; *) echo "unknown option $1"; exit 1 ;; esac; done
[ -d "$OLD_IN" ] || { echo "$OLD_IN is not a folder."; exit 1; }
OLD=$(cd "$OLD_IN" && pwd -P); OLD_LOGICAL=$(cd "$OLD_IN" && pwd)
[ "$OLD" != "$NEW" ] || { echo "Run this from the NEW folder (a fresh clone), giving the old folder as the argument."; exit 1; }
[ -f "$OLD/goblins.yaml" ] || { echo "$OLD has no goblins.yaml: that is not an old Goblin Bank folder."; exit 1; }
MADE=(people.yaml .secrets data gateway-state gateway.yaml docker-compose.generated.yml .migrated-from)      # what this script creates here; none may exist yet
for f in "${MADE[@]}"; do [ ! -e "$f" ] || { echo "This folder already has $f, so it is not a fresh clone. Use a fresh clone (git clone ...)."; exit 1; }; done
if [ -n "$DATA" ]; then case "$DATA" in /*) ;; *) DATA="$NEW/$DATA" ;; esac; mkdir -p "$DATA"; DATA=$(cd "$DATA" && pwd -P); fi
DATA_MOUNT=(); DATA_ARG=(); SECRETS="$NEW/.secrets"
if [ -n "$DATA" ]; then
  DATA_MOUNT=(-v "$DATA":"$DATA"); DATA_ARG=(--data-dir "$DATA"); SECRETS="$DATA/.secrets"
  for f in .secrets data gateway-state gateway.yaml; do [ ! -e "$DATA/$f" ] || { echo "$DATA already has $f. Use an empty data folder."; exit 1; }; done
fi
run() { docker run --rm -u 0 -v /:/host:ro -v "$NEW":"$NEW" -w "$NEW" "${DATA_MOUNT[@]}" --entrypoint python "$IMAGE" "$@"; }

say "1/7 Building plaid-proxy (first time takes a few minutes)"
[ "${SKIP_BUILD:-}" = 1 ] || docker build -q -t "$IMAGE" . >/dev/null

say "2/7 Checking the old install (changes nothing)"
run scripts/migrate_from_goblin_bank.py check "$OLD" --host-root /host

PROJECTS=$(project_of "$OLD" "$OLD_LOGICAL")
say "3/7 Stopping the old containers so the copy is consistent (their files are not touched)"
if [ -n "$PROJECTS" ]; then
  for p in $PROJECTS; do echo "   stopping compose project $p"; docker compose -p "$p" down; done
fi
left=$(docker ps --format '{{.Names}}' | grep -E '(^|[-_])goblin-' || true)
[ -z "$left" ] || { echo "These old containers are still running and were not started from $OLD:"; echo "$left"; echo "Stop them (docker stop ...), then run this again. Nothing was changed."; exit 1; }

FINISHED=0
restart_old() {      # on any failure or interrupt after the old containers were stopped: undo the half-made copy and start the old ones again
  [ "$FINISHED" = 1 ] && return
  trap - EXIT INT TERM
  echo; echo "!! Something failed. Putting the old Goblin Bank back exactly as it was..."
  [ -f "$NEW/docker-compose.generated.yml" ] && docker compose -f "$NEW/docker-compose.generated.yml" down >/dev/null 2>&1 || true
  (cd "$NEW" && rm -rf -- "${MADE[@]}")
  [ -n "$DATA" ] && (cd "$DATA" && rm -rf -- .secrets data gateway-state gateway.yaml)
  local files=(); for f in "$OLD"/docker-compose*.yml; do files+=(-f "$f"); done
  for p in $PROJECTS; do (cd "$OLD" && docker compose -p "$p" "${files[@]}" up -d) || true; done
  if [ -n "$PROJECTS" ]; then echo "!! Done: the old containers are running again and the half-made copy was removed."
  else echo "!! Done: the half-made copy was removed (the old containers were not running before, so they were left stopped)."; fi
  echo "!! The old folder was never changed. Fix the problem shown above and run this again."
}
trap restart_old EXIT
trap 'exit 1' INT TERM

say "4/7 Copying people, keys, logins and data"
run scripts/migrate_from_goblin_bank.py copy "$OLD" --host-root /host "${DATA_ARG[@]}"
printf '%s\n' "$PROJECTS" | tr '\n' ' ' | sed 's/ *$//' | { read -r p || true; printf '%s\n%s\n' "$OLD" "$p" > .migrated-from; }

say "5/7 Writing the new compose file"
docker run --rm -u 0 -v "$NEW":"$NEW" -w "$NEW" "${DATA_MOUNT[@]}" -e SECRETS="$SECRETS" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" --entrypoint sh "$IMAGE" -c '
  python scripts/gen_compose.py &&
  chown -R "$HOST_UID:$HOST_GID" "$SECRETS" docker-compose.generated.yml people.yaml .migrated-from'

say "6/7 Verifying every key and login is what it was"
run scripts/migrate_from_goblin_bank.py verify "$OLD" --host-root /host "${DATA_ARG[@]}"

say "7/7 Starting plaid-proxy"
docker compose -f docker-compose.generated.yml up -d --build
PORT=$(sed -n 's/^port: *\([0-9]*\).*/\1/p' people.yaml | head -1); PORT=${PORT:-6421}
ADMIN=$(cat "$SECRETS/admin.password" 2>/dev/null || true)
up=""
for _ in $(seq 1 60); do
  if [ -n "$ADMIN" ]; then
    st=$(curl -s -H "Authorization: Bearer $ADMIN" "http://127.0.0.1:$PORT/admin/status" || true)
    if printf '%s' "$st" | grep -q '"workers":\[{' && ! printf '%s' "$st" | grep -q '"up":false'; then up="every person's container answers the gateway"; fi
  else
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/admin")" = 200 ] && up="(admin page answers)"
  fi
  [ -n "$up" ] && break
  sleep 2
done
[ -n "$up" ] || { docker compose -f docker-compose.generated.yml ps; docker compose -f docker-compose.generated.yml logs --tail 20; echo "plaid-proxy did not come up."; exit 1; }
FINISHED=1; trap - EXIT
docker compose -f docker-compose.generated.yml ps
cat <<MSG

Done. plaid-proxy is running with everything from $OLD: $up
  - Sign in as before at http://127.0.0.1:$PORT (same names, passwords and tokens). The admin page and its password are unchanged.
  - Plaid links, Firefly III tokens and account links, Pondering Orb and Home Assistant tokens all carry on as they were.
  - The old folder was not changed. Once you have checked everything, remove it (a private backup copy is made first):
      ./scripts/migrate_from_goblin_bank.sh --remove-old
MSG
