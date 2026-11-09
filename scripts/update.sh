#!/usr/bin/env bash
# Update to the latest version: back up, pull, rebuild, restart. Your data and secrets are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/backup.sh
git pull --ff-only
docker compose -f docker-compose.generated.yml build
docker compose -f docker-compose.generated.yml up -d
docker compose -f docker-compose.generated.yml ps
