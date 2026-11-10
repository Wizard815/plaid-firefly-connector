#!/usr/bin/env bash
# Why are no accounts showing up after linking Plaid? Checks the Plaid setup step by step and tells you what is wrong.
# Secrets are never printed: a key is shown only as "set" or "not set". Run from the repository folder:  ./scripts/doctor.sh
# Optional: PORT=6868 (the app's port), COMPOSE=docker-compose.generated.yml
set -uo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-6868}; COMPOSE=${COMPOSE:-docker-compose.generated.yml}; BASE="http://127.0.0.1:$PORT"
ok() { printf '  [ok]  %s\n' "$*"; }; bad() { printf '  [!!]  %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }; note() { printf '        %s\n' "$*"; }
PROBLEMS=0

echo "1. The deployment"
[ -f "$COMPOSE" ] && ok "compose file: $COMPOSE" || bad "no $COMPOSE here. Run this from the Goblin-Bank folder, after ./scripts/install.sh"
[ -d .secrets ] && ok ".secrets/ folder found" || bad "no .secrets/ folder (install.sh creates it)"
if command -v docker >/dev/null && [ -f "$COMPOSE" ]; then
  docker compose -f "$COMPOSE" ps --format '  {{.Service}}: {{.Status}}' 2>/dev/null | sed 's/^/        /' || true
  docker compose -f "$COMPOSE" ps --format '{{.Status}}' 2>/dev/null | grep -qi "unhealthy\|restarting\|exited" && bad "a container is not healthy: docker compose -f $COMPOSE logs <service>"
fi
curl -fsS -m 5 "$BASE/health" >/dev/null 2>&1 && ok "the app answers at $BASE" || { bad "nothing answers at $BASE (wrong port, or the containers are down)"; }

echo; echo "2. Plaid keys in the optional file .secrets/plaid.env (keys saved in the app are checked in step 4)"
if [ -f .secrets/plaid.env ]; then
  getv() { sed -n "s/^$1=//p" .secrets/plaid.env | head -1; }
  cid=$(getv PLAID_CLIENT_ID); sec=$(getv PLAID_SECRET); env_=$(getv PLAID_ENV)
  [ -n "$cid" ] && ok "PLAID_CLIENT_ID: set" || note "PLAID_CLIENT_ID: not set here"
  [ -n "$sec" ] && ok "PLAID_SECRET: set" || note "PLAID_SECRET: not set here"
  { [ -n "$cid" ] && [ -z "$sec" ]; } && bad "a Client ID alone does nothing: Plaid counts as configured only when BOTH the Client ID and the secret are set"
  { [ -z "$cid" ] && [ -n "$sec" ]; } && bad "a secret alone does nothing: the Client ID is missing"
  note "PLAID_ENV: ${env_:-not set (defaults to sandbox)}"
else
  note "no .secrets/plaid.env (fine if you entered the keys in the app or on the admin page)"
fi

echo; echo "3. Ask the running app (sign in; the password is not shown or saved)"
read -rp "   Your name: " NAME_; read -rsp "   Your password: " PW_; echo
TOK=$(NAME_="$NAME_" PW_="$PW_" python3 - "$BASE" <<'PY' 2>/dev/null
import json, os, sys, urllib.request
req = urllib.request.Request(sys.argv[1] + "/login", data=json.dumps({"name": os.environ["NAME_"], "password": os.environ["PW_"]}).encode(), headers={"Content-Type": "application/json"})
try:
    print(json.load(urllib.request.urlopen(req, timeout=15))["token"])
except Exception as e:
    print("")
PY
)
unset PW_
if [ -z "$TOK" ]; then bad "could not sign in (wrong name or password, or locked for a few minutes after wrong tries)"; echo; echo "$PROBLEMS problem(s) found."; exit 1; fi
ok "signed in as $NAME_"
call() { curl -sS -m 30 -X "$1" -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$BASE/g/$NAME_$2" -d '{}' 2>/dev/null; }
STATUS=$(call GET /api/status); SETTINGS=$(call GET /api/settings)
echo "$STATUS" | python3 -c '
import json, sys
s = json.load(sys.stdin)["plaid"]
print("   configured:", s.get("configured"), "| environment the app is using:", s.get("env"), "| linked banks:", len(s.get("items", [])), "| webhook address set:", s.get("webhook_set"))
for i in s.get("items", []):
    print("     -", i.get("institution"), "| checked:", i.get("schedule_effective"), "|", ("NEEDS SIGN-IN: " + str(i.get("status"))) if i.get("status") else "ok")
' 2>/dev/null || bad "the app did not return its status"
CONF=$(echo "$STATUS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plaid"].get("configured"))' 2>/dev/null)
ENVN=$(echo "$STATUS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["plaid"].get("env"))' 2>/dev/null)
[ "$CONF" = "True" ] && ok "Plaid is configured (Client ID and secret are both present)" || bad "Plaid is NOT configured: enter BOTH the Client ID and the secret (Settings, Connect & import, Plaid keys, or the admin page)"
echo "$SETTINGS" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("   Client ID:", "set" if d.get("plaid_client_id") else "not set", "| secret:", "set" if (d.get("plaid_secret") or {}).get("set") else "not set", "| managed by the admin page:", bool(d.get("managed")))' 2>/dev/null

echo; echo "4. Do the keys work, and for which environment?"
if [ "$CONF" = "True" ]; then
  R=$(call POST /plaid/test)
  if echo "$R" | grep -q '"ok"'; then ok "Plaid accepted the keys ($ENVN)"; else bad "Plaid rejected the keys: $(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("detail"))' 2>/dev/null)"; note "INVALID_API_KEYS usually means the secret does not belong to this Client ID, or it is the Sandbox secret used with Production (or the other way round)."; fi
  note "The app is using: $ENVN. In dashboard.plaid.com, Developers, Keys: use the Sandbox secret with Sandbox and the Production secret with Production. Mixing them authenticates against the wrong environment and shows no real accounts."
fi

echo; echo "5. Things this script cannot see"
note "- Linking happens ONLY inside Goblin-Bank (Settings, Connect & import, Link an account). my.plaid.com is Plaid's consumer portal: banks linked or shown there are not visible to this app."
note "- Production needs Plaid to have approved your account for production access (dashboard.plaid.com, Developers). Until then only Sandbox works, with Plaid's test login user_good / pass_good, not real bank credentials."
note "- Banks that sign you in on their own site (OAuth, e.g. Capital One) need a public HTTPS address set on the admin page and added under Allowed redirect URIs in the Plaid dashboard."
echo; [ "$PROBLEMS" -eq 0 ] && echo "No configuration problems found." || echo "$PROBLEMS problem(s) found. Fix them, restart with: docker compose -f $COMPOSE up -d"
