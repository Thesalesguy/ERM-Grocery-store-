#!/usr/bin/env bash
# M12 Phase 3: restore a backup taken by backup_database.sh.
#
# Usage: ./scripts/restore_database.sh <dump_file> <target_db_name>
#
# DESTRUCTIVE: this drops and recreates <target_db_name> before
# restoring. Never run this against a database anyone still needs --
# it exists to restore into a fresh/recovery target, not to "merge"
# a backup into a live database.
#
# After this script, verify the restore before trusting it:
#   python -m scripts.integrity_snapshot <restored_db_url>
# and compare against a snapshot taken before the backup was made (see
# docs/M12_HARDENING_AUDIT.md's backup/restore procedure and
# tests/test_backup_restore.py for the tested reference implementation
# of this exact comparison).
set -euo pipefail

DUMP_FILE="${1:?Usage: restore_database.sh <dump_file> <target_db_name>}"
TARGET_DB="${2:?Usage: restore_database.sh <dump_file> <target_db_name>}"

if [ ! -f "$DUMP_FILE" ]; then
    echo "Dump file not found: $DUMP_FILE" >&2
    exit 1
fi

read -r -p "This will DROP and recreate database '${TARGET_DB}'. Type the database name to confirm: " CONFIRM
if [ "$CONFIRM" != "$TARGET_DB" ]; then
    echo "Confirmation did not match. Aborting." >&2
    exit 1
fi

echo "Dropping and recreating ${TARGET_DB} ..."
dropdb --if-exists --force "$TARGET_DB"
createdb -O erp_user "$TARGET_DB"

echo "Restoring ${DUMP_FILE} into ${TARGET_DB} ..."
pg_restore --clean --if-exists -d "$TARGET_DB" "$DUMP_FILE"

echo "Restore complete."
echo
echo "erp_app's password is NOT captured by this dump/restore (roles are"
echo "cluster-wide, not per-database) -- if this is a genuinely new"
echo "cluster, run backend/scripts/bootstrap_db_roles.sql FIRST, before"
echo "this script, so the GRANT/REVOKE statements in the dump have a"
echo "role to apply to."
echo
echo "Next: verify the restore before trusting it -- run"
echo "  python -m scripts.integrity_snapshot postgresql+psycopg://erp_user:<password>@localhost:5432/${TARGET_DB}"
echo "and compare against the pre-backup snapshot."
