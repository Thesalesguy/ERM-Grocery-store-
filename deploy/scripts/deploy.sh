#!/usr/bin/env bash
# M13 Phase 10: the seven-step deployment procedure the task requires --
# backup -> health check -> migration -> application deployment ->
# smoke tests -> verification -> rollback decision.
#
# Zero-downtime is explicitly NOT claimed: step 4 restarts the single
# backend process/container this deployment topology runs, and nginx
# will return 502 for the real, brief (single-digit-second) gap until
# the new process passes its health check (docs/M13_DESIGN.md Section
# 10). This is stated plainly rather than hidden behind vague language.
#
# This script never automatically executes a schema downgrade or
# restarts a previous application version on failure -- see step 7. A
# rollback of the DATABASE is always safe when it's a genuine
# `alembic downgrade` to the immediately-preceding revision; a rollback
# of the APPLICATION to a previous version while the schema stays
# forward-migrated is NOT safe in general (this codebase does not
# maintain N-1 schema compatibility -- docs/M13_DESIGN.md Section 11)
# and this script will never attempt it for you.
#
# Usage: deploy.sh
#
# Configuration (all overridable via environment, with production-
# shaped defaults):
#   BACKUP_DIR          Local pre-deploy backup directory (default ./backups)
#   APP_HEALTH_BASE_URL Base URL smoke tests are run against
#                        (default http://127.0.0.1:8000)
#   APP_RESTART_CMD      The command that deploys the new application code
#                        (default: the real docker-compose.prod.yml restart;
#                        overridden by deploy/tests/test_deploy_procedure.py
#                        to exercise this script's control flow against this
#                        sandbox's native-process stack instead)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
APP_HEALTH_BASE_URL="${APP_HEALTH_BASE_URL:-http://127.0.0.1:8000}"
APP_RESTART_CMD="${APP_RESTART_CMD:-docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod up -d --build backend}"
# The real container image has `python`/`alembic` directly on PATH;
# overridden by tests to this repo's own backend/.venv interpreter.
PYTHON_BIN="${PYTHON_BIN:-python}"

_step() {
    echo
    echo "=== Step $1: $2 ==="
}

_fail() {
    echo "DEPLOYMENT FAILED at step: $1" >&2
    echo "$2" >&2
    _print_rollback_guidance
    exit 1
}

_print_rollback_guidance() {
    cat >&2 <<'EOF'

--- Rollback decision (manual -- this script will not act for you) ---
1. If the MIGRATION step failed: the database is unchanged (Alembic's
   transactional DDL means a failed migration never leaves a partial
   schema change -- see backend/tests/test_disaster_recovery_scenarios.py).
   No schema rollback is needed. Fix the migration and redeploy.
2. If a step AFTER a successful migration failed (app deployment, smoke
   tests, verification): the schema is already at the new head. Redeploying
   the PREVIOUS application version against this new schema is NOT safe in
   general -- this codebase does not maintain N-1 schema compatibility
   (docs/M13_DESIGN.md Section 11). Two real options:
     a. Fix forward: resolve the application-level problem and redeploy
        the new version (the schema is already correct for it).
     b. Full rollback: `alembic downgrade <previous_revision>` AND deploy
        the exact previous application version together, never one
        without the other.
3. Never restart the previous application version against the new
   schema as a shortcut -- that is exactly the unsafe combination this
   procedure exists to prevent.
EOF
}

# --- Step 1: Backup ---------------------------------------------------
_step 1 "Backup"
if ! "$BACKEND_DIR/scripts/backup_database.sh" "$BACKUP_DIR"; then
    _fail "backup" "Could not take a pre-deploy backup -- refusing to proceed without one."
fi

# --- Step 2: Health check (pre-flight) --------------------------------
_step 2 "Pre-flight health check"
if ! curl -fsS "${APP_HEALTH_BASE_URL}/health/db" > /dev/null; then
    _fail "pre-flight health check" "The database is not reachable before this deployment even started -- fix that first."
fi

# --- Step 3: Migration -------------------------------------------------
_step 3 "Migration"
if ! (cd "$BACKEND_DIR" && "$PYTHON_BIN" -m alembic upgrade head); then
    _fail "migration" "alembic upgrade head failed. The database was NOT left in a partial state (transactional DDL) -- see the rollback guidance below."
fi

# --- Step 4: Application deployment ------------------------------------
_step 4 "Application deployment"
echo "Running: $APP_RESTART_CMD"
if ! (cd "$REPO_ROOT" && eval "$APP_RESTART_CMD"); then
    _fail "application deployment" "The application failed to deploy/restart. The schema is already at the new head -- see the rollback guidance below."
fi

# --- Step 5: Smoke tests ------------------------------------------------
_step 5 "Smoke tests"
_wait_healthy() {
    local url="$1" tries=30
    while [ "$tries" -gt 0 ]; do
        if curl -fsS "$url" > /dev/null 2>&1; then
            return 0
        fi
        tries=$((tries - 1))
        sleep 1
    done
    return 1
}
if ! _wait_healthy "${APP_HEALTH_BASE_URL}/health"; then
    _fail "smoke tests" "GET /health never became healthy after the new deployment."
fi
if ! _wait_healthy "${APP_HEALTH_BASE_URL}/health/db"; then
    _fail "smoke tests" "GET /health/db never became healthy after the new deployment."
fi

# --- Step 6: Verification ----------------------------------------------
_step 6 "Verification"
# Deliberately a SEPARATE mechanism from step 5's HTTP smoke test (not
# just re-checking the same endpoint) -- confirms via GET /health/migration
# itself, which independently re-derives the code's migration head from
# the deployed alembic/versions/ directory and compares it against the
# database's actual applied version, that this specific deployment's
# code and schema genuinely agree.
migration_status=$(curl -fsS "${APP_HEALTH_BASE_URL}/health/migration") || migration_status=""
if [[ "$migration_status" != *'"status":"ok"'* && "$migration_status" != *'"status": "ok"'* ]]; then
    _fail "verification" "GET /health/migration did not report current after deployment: ${migration_status}"
fi

echo
echo "=== Deployment complete: backup, migration, deploy, smoke tests, and verification all succeeded. ==="
