#!/usr/bin/env bash
#
# Pull a fresh PostgreSQL snapshot from the Hetzner production server
# into the local development database.
#
# Reuses .deploy.env so DEPLOY_HOST / DEPLOY_USER / DEPLOY_PATH are
# shared with deploy.sh. Run from anywhere — the script cd's into the
# repo root before touching the local docker compose stack.
#
# WARNING: this DROPs and recreates the local ogn_monitor database.
# Tenants, login hashes and flight history are all replaced by the
# production data. Local Redis hot state is left untouched and will
# rebuild from incoming OGN beacons after the worker restart.
#
# Usage:
#   ./scripts/pull-db.sh           # interactive confirm
#   ./scripts/pull-db.sh --yes     # skip confirmation (for cron etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .deploy.env ]]; then
    echo "ERROR: .deploy.env not found in $REPO_ROOT" >&2
    exit 1
fi
# shellcheck disable=SC1091
source .deploy.env

: "${DEPLOY_HOST:?DEPLOY_HOST not set in .deploy.env}"
: "${DEPLOY_USER:=root}"
: "${DEPLOY_PATH:=/opt/ogn_monitor}"

ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
done

log()  { printf '\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

mkdir -p backups
DUMP="backups/prod_$(date +%Y%m%d-%H%M%S).dump"
SSH_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

# ---- 1. Dump from server ----
log "Dumping production DB from $SSH_TARGET"
ssh "$SSH_TARGET" \
    "docker compose -f ${DEPLOY_PATH}/docker-compose.yml exec -T postgres \
     pg_dump -U ogn_monitor -Fc --no-owner --no-acl ogn_monitor" \
    > "$DUMP"

if [[ ! -s "$DUMP" ]]; then
    err "Dump file is empty — aborting"
    rm -f "$DUMP"
    exit 1
fi
SIZE=$(du -h "$DUMP" | cut -f1)
ok "Dump saved: $DUMP ($SIZE)"

# ---- 2. Confirm ----
if [[ $ASSUME_YES -eq 0 ]]; then
    warn "Local database 'ogn_monitor' will be DROPPED and replaced."
    read -r -p "Continue? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ---- 3. Local restore ----
log "Checking local stack"
if ! docker compose ps postgres --format '{{.Status}}' | grep -q 'Up'; then
    err "Local postgres container is not running. Start it with: docker compose up -d postgres"
    exit 1
fi

log "Dropping local database"
docker compose exec -T postgres psql -U ogn_monitor -d postgres -v ON_ERROR_STOP=1 -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
       WHERE datname = 'ogn_monitor' AND pid <> pg_backend_pid();" > /dev/null
docker compose exec -T postgres psql -U ogn_monitor -d postgres -v ON_ERROR_STOP=1 -c \
    "DROP DATABASE IF EXISTS ogn_monitor;"
docker compose exec -T postgres psql -U ogn_monitor -d postgres -v ON_ERROR_STOP=1 -c \
    "CREATE DATABASE ogn_monitor OWNER ogn_monitor;"

log "Restoring dump"
docker compose exec -T postgres pg_restore -U ogn_monitor -d ogn_monitor \
    --no-owner --no-acl < "$DUMP"

# ---- 4. Restart worker + api so they pick up the new state ----
log "Restarting worker and api"
docker compose restart worker api

# ---- 5. Quick sanity check ----
log "Sanity check"
docker compose exec -T postgres psql -U ogn_monitor -d ogn_monitor -tAc \
    "SELECT 'tenants=' || COUNT(*) FROM tenants;
     SELECT 'airfields=' || COUNT(*) FROM airfields;
     SELECT 'flight_log=' || COUNT(*) FROM flight_log;"

ok "Done. Dump retained at $DUMP"
