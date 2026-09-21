#!/usr/bin/env bash
# M12 Phase 3: take a real, restorable PostgreSQL backup.
#
# Usage: ./scripts/backup_database.sh [output_directory]
#
# Reads connection details from the same environment variables the
# application itself uses (MIGRATIONS_DATABASE_URL, falling back to
# DATABASE_URL) so this never needs its own separate credential
# configuration -- see app/core/config.py. Dumps as the schema-owning
# role (erp_user), not erp_app, so the dump captures every table
# regardless of erp_app's restricted grants.
#
# Uses pg_dump's custom format (-Fc): compressed, and the only format
# pg_restore can selectively replay from -- deliberately NOT
# --no-owner/--no-privileges, so the dump captures the actual current
# GRANT/REVOKE state (including the REVOKE UPDATE/DELETE that makes
# journal_entries/audit_logs/inventory_movements/etc. append-only for
# erp_app) and replays it exactly on restore. See
# docs/M12_HARDENING_AUDIT.md for the full backup/restore procedure
# and the tested proof this round-trips correctly
# (tests/test_backup_restore.py).
set -euo pipefail

OUTPUT_DIR="${1:-./backups}"
mkdir -p "$OUTPUT_DIR"

DB_URL="${MIGRATIONS_DATABASE_URL:-${DATABASE_URL:?Set DATABASE_URL or MIGRATIONS_DATABASE_URL}}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB_NAME="$(echo "$DB_URL" | sed -E 's#.*/([^/?]+).*#\1#')"
OUTPUT_FILE="${OUTPUT_DIR}/${DB_NAME}_${TIMESTAMP}.dump"

# pg_dump doesn't understand the SQLAlchemy "+psycopg" driver suffix in
# the URL scheme -- strip it before handing the URL to a plain libpq tool.
PG_URL="${DB_URL/postgresql+psycopg:/postgresql:}"

echo "Backing up ${DB_NAME} to ${OUTPUT_FILE} ..."
pg_dump -Fc -f "$OUTPUT_FILE" "$PG_URL"
# M13 Phase 18: pg_dump creates its output under the invoking process's
# umask -- typically 0644/0664, world- or group-readable. This file is
# the full plaintext database (every table, unencrypted) until
# backup_offbox.sh encrypts it, so it must never be readable by any OS
# user other than whoever owns it (found as a real gap: a default
# umask leaves every local backup world-readable).
chmod 600 "$OUTPUT_FILE"
echo "Backup complete: $(du -h "$OUTPUT_FILE" | cut -f1) at ${OUTPUT_FILE}"
echo
echo "Retention is operator-managed in M12 (no automatic rotation/off-box"
echo "transport is built here -- see docs/M12_DESIGN.md 'Known limitations')."
echo "Restore with: ./scripts/restore_database.sh ${OUTPUT_FILE} <target_db_name>"
