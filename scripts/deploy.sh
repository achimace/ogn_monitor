#!/usr/bin/env bash
#
# Deploy OGN FlightMonitor to the Hetzner production server.
#
# What it does:
#   1. Sanity-check that the local git tree is clean (or pass --dirty)
#   2. Push current branch to origin (skip with --no-push)
#   3. Copy db/migrations/*.sql to the server (idempotent SQL migrations)
#   4. SSH to the server and:
#        - git pull
#        - psql each migration file (idempotent)
#        - docker compose up -d --build
#        - wait for /health to return 200
#
# Configuration: create a .deploy.env file in the repo root with:
#
#   DEPLOY_HOST=flight-monitor.de
#   DEPLOY_USER=root
#   DEPLOY_PATH=/opt/ogn_monitor
#   DEPLOY_BRANCH=master
#   HEALTH_URL=https://flight-monitor.de/health
#   # Optional: GitHub HTTPS auth for the remote `git pull`. A fine-grained
#   # Personal Access Token is strongly preferred over a real password.
#   GIT_USER=achimace
#   GIT_PASSWORD=ghp_xxxxxxxxxxxxxxxxxxxxxxxxx
#
# .deploy.env is gitignored (see .gitignore).
#
# Usage:
#   ./scripts/deploy.sh                # standard deploy
#   ./scripts/deploy.sh --no-push      # skip git push (server already has the commit)
#   ./scripts/deploy.sh --dirty        # allow uncommitted changes (rsync code instead of git pull)
#   ./scripts/deploy.sh --skip-migrate # don't run SQL migrations

set -euo pipefail

# ---- locate repo root ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---- load config ----
if [[ ! -f .deploy.env ]]; then
    echo "ERROR: .deploy.env not found in $REPO_ROOT" >&2
    echo "Create it with DEPLOY_HOST, DEPLOY_USER, DEPLOY_PATH, DEPLOY_BRANCH, HEALTH_URL" >&2
    exit 1
fi
# shellcheck disable=SC1091
source .deploy.env

: "${DEPLOY_HOST:?DEPLOY_HOST not set in .deploy.env}"
: "${DEPLOY_USER:=root}"
: "${DEPLOY_PATH:=/opt/ogn_monitor}"
: "${DEPLOY_BRANCH:=master}"
: "${HEALTH_URL:=https://${DEPLOY_HOST}/health}"
# Optional GitHub HTTPS credentials. If both are set, the remote git pull
# will use them via a one-shot credential helper. Recommendation: use a
# fine-grained Personal Access Token as GIT_PASSWORD instead of a real
# password. The values are sent to the server through the SSH stdin pipe
# and never written to a file on disk.
: "${GIT_USER:=}"
: "${GIT_PASSWORD:=}"

SSH_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

# ---- parse args ----
PUSH=1
DIRTY=0
MIGRATE=1
for arg in "$@"; do
    case "$arg" in
        --no-push)      PUSH=0 ;;
        --dirty)        DIRTY=1 ;;
        --skip-migrate) MIGRATE=0 ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

# ---- helpers ----
log()  { printf '\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

# ---- 1. sanity checks ----
log "Sanity checks"
if [[ $DIRTY -eq 0 ]] && ! git diff --quiet HEAD --; then
    err "Working tree has uncommitted changes. Commit or pass --dirty."
    git status --short
    exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
    warn "Current branch is '$CURRENT_BRANCH', expected '$DEPLOY_BRANCH'"
    read -r -p "Continue anyway? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 1
fi
ok "Local repo OK ($CURRENT_BRANCH @ $(git rev-parse --short HEAD))"

# ---- 2. push ----
if [[ $PUSH -eq 1 ]]; then
    log "Pushing $CURRENT_BRANCH to origin"
    git push origin "$CURRENT_BRANCH"
    ok "Pushed"
else
    warn "Skipping git push (--no-push)"
fi

# ---- 3. remote update ----
# Note: migrations live in the repo and are pulled along with the code,
# so there is no separate rsync step. Doing rsync before `git pull` would
# leave untracked files on disk that git refuses to overwrite. The remote
# script below pulls first, then runs every db/migrations/*.sql file.
log "Running remote deployment on $SSH_TARGET"

REMOTE_SCRIPT=$(cat <<'EOF'
set -euo pipefail
cd "__DEPLOY_PATH__"

# Credentials are passed in via the environment (GIT_USER / GIT_PASSWORD).
# We never echo them, never persist them, and only inject them into git
# via a one-shot in-process credential helper.
if [[ -n "${GIT_USER:-}" && -n "${GIT_PASSWORD:-}" ]]; then
    echo "→ git pull (HTTPS, user ${GIT_USER})"
    GIT_TERMINAL_PROMPT=0 \
    git -c "credential.helper=" \
        -c "credential.helper=!f() { echo username=${GIT_USER}; echo password=${GIT_PASSWORD}; }; f" \
        pull --ff-only origin "__DEPLOY_BRANCH__"
else
    echo "→ git pull"
    git pull --ff-only origin "__DEPLOY_BRANCH__"
fi

if [[ "__MIGRATE__" == "1" ]]; then
    echo "→ applying SQL migrations"
    if compgen -G "db/migrations/*.sql" > /dev/null; then
        for f in db/migrations/*.sql; do
            echo "   - $f"
            docker compose exec -T postgres psql -U "${DB_USER:-ogn_monitor}" -d ogn_monitor \
                -v ON_ERROR_STOP=1 -f - < "$f"
        done
    else
        echo "   (no migration files found)"
    fi
fi

echo "→ docker compose up -d --build"
docker compose up -d --build

echo "→ docker compose ps"
docker compose ps
EOF
)

# Substitute placeholders (avoid heredoc env expansion issues)
REMOTE_SCRIPT="${REMOTE_SCRIPT//__DEPLOY_PATH__/$DEPLOY_PATH}"
REMOTE_SCRIPT="${REMOTE_SCRIPT//__DEPLOY_BRANCH__/$DEPLOY_BRANCH}"
REMOTE_SCRIPT="${REMOTE_SCRIPT//__MIGRATE__/$MIGRATE}"

# Forward git credentials through SSH via -o SendEnv. We pre-pend explicit
# `export` lines so the helper above sees them, without the values ever
# touching the server's filesystem.
REMOTE_PRELUDE="export GIT_USER=$(printf '%q' "$GIT_USER"); "
REMOTE_PRELUDE+="export GIT_PASSWORD=$(printf '%q' "$GIT_PASSWORD"); "

ssh "$SSH_TARGET" "bash -s" <<< "${REMOTE_PRELUDE}${REMOTE_SCRIPT}"
ok "Remote deployment finished"

# ---- 5. health check ----
log "Health check: $HEALTH_URL"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null; then
        ok "Service healthy"
        exit 0
    fi
    sleep 3
done

err "Health check failed after 30s"
ssh "$SSH_TARGET" "cd $DEPLOY_PATH && docker compose logs --tail=30 api worker"
exit 1
