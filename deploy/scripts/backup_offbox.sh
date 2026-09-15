#!/usr/bin/env bash
# M13 Phase 5: the off-box half of backup/recovery. M12 already proved
# the LOCAL pg_dump/pg_restore cycle (backend/scripts/backup_database.sh,
# backend/scripts/restore_database.sh, tests/test_backup_restore.py) --
# this wraps that same local backup with what M12 explicitly did not
# build: encryption, transport to a destination that is not this host,
# a checksummed manifest so corruption is DETECTED rather than silently
# restored, and retention cleanup on the remote.
#
# Usage:
#   backup_offbox.sh <local_backup_dir> <rclone_remote> [retention_days]
#
# Requires:
#   - backend/scripts/backup_database.sh's own env vars
#     (MIGRATIONS_DATABASE_URL or DATABASE_URL)
#   - BACKUP_AGE_PUBLIC_KEY (an `age` public key — see .env.prod.example
#     and docs/M13_HARDENING_AUDIT.md Section 5 for why the matching
#     PRIVATE key must never live on this host)
#   - `rclone` already configured with <rclone_remote> (`rclone config`
#     — never store the remote's own credentials in this repo or in
#     .env.prod; rclone keeps its own separate config file, conventionally
#     $HOME/.config/rclone/rclone.conf, root-owned, chmod 600)
#
# Writes a one-line status file (BACKUP_STATUS_FILE, default
# /var/lib/erp/backup_status.prom) in Prometheus textfile-collector
# format so Phase 6's monitoring can alert on backup failure without
# needing to parse this script's log output.
set -euo pipefail

LOCAL_BACKUP_DIR="${1:?Usage: backup_offbox.sh <local_backup_dir> <rclone_remote> [retention_days]}"
RCLONE_REMOTE="${2:?Usage: backup_offbox.sh <local_backup_dir> <rclone_remote> [retention_days]}"
RETENTION_DAYS="${3:-14}"
STATUS_FILE="${BACKUP_STATUS_FILE:-/var/lib/erp/backup_status.prom}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/../../backend" && pwd)"

_write_status() {
    local success="$1"
    mkdir -p "$(dirname "$STATUS_FILE")" 2>/dev/null || true
    {
        echo "# HELP erp_backup_last_run_timestamp_seconds Unix time of the last backup attempt"
        echo "# TYPE erp_backup_last_run_timestamp_seconds gauge"
        echo "erp_backup_last_run_timestamp_seconds $(date +%s)"
        echo "# HELP erp_backup_last_success 1 if the last backup attempt succeeded, else 0"
        echo "# TYPE erp_backup_last_success gauge"
        echo "erp_backup_last_success ${success}"
    } > "$STATUS_FILE" 2>/dev/null || echo "(warning: could not write $STATUS_FILE)" >&2
}

on_failure() {
    _write_status 0
    echo "BACKUP FAILED — see output above. Status recorded as failed." >&2
}
trap on_failure ERR

mkdir -p "$LOCAL_BACKUP_DIR"

echo "=== 1/5: local pg_dump ==="
"$BACKEND_DIR/scripts/backup_database.sh" "$LOCAL_BACKUP_DIR"
DUMP_FILE="$(ls -t "$LOCAL_BACKUP_DIR"/*.dump | head -1)"
echo "Using dump: $DUMP_FILE"

echo "=== 2/5: checksum + encrypt ==="
CHECKSUM="$(sha256sum "$DUMP_FILE" | cut -d' ' -f1)"
ENCRYPTED_FILE="${DUMP_FILE}.age"
if [ -z "${BACKUP_AGE_PUBLIC_KEY:-}" ]; then
    echo "BACKUP_AGE_PUBLIC_KEY not set — refusing to transport an unencrypted backup off-box." >&2
    exit 1
fi
age -r "$BACKUP_AGE_PUBLIC_KEY" -o "$ENCRYPTED_FILE" "$DUMP_FILE"
MANIFEST_FILE="${DUMP_FILE}.manifest.json"
python3 -c "
import json, sys
print(json.dumps({
    'dump_filename': sys.argv[1],
    'sha256_plaintext': sys.argv[2],
    'encrypted_bytes': $(stat -c%s "$ENCRYPTED_FILE"),
    'created_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
}, indent=2))
" "$(basename "$DUMP_FILE")" "$CHECKSUM" > "$MANIFEST_FILE"
echo "Encrypted: $ENCRYPTED_FILE ($(du -h "$ENCRYPTED_FILE" | cut -f1))"

echo "=== 3/5: transport off-box (rclone -> ${RCLONE_REMOTE}) ==="
rclone copy "$ENCRYPTED_FILE" "$RCLONE_REMOTE" --checksum
rclone copy "$MANIFEST_FILE" "$RCLONE_REMOTE" --checksum

echo "=== 4/5: verify the remote copy's checksum, not just that rclone exited 0 ==="
REMOTE_CHECKSUM_CHECK="$(rclone check "$LOCAL_BACKUP_DIR" "$RCLONE_REMOTE" --include "$(basename "$ENCRYPTED_FILE")" 2>&1)" || {
    echo "Remote copy failed rclone's own checksum verification:" >&2
    echo "$REMOTE_CHECKSUM_CHECK" >&2
    exit 1
}

echo "=== 5/5: retention cleanup on the remote (older than ${RETENTION_DAYS}d) ==="
rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DAYS}d" --include "*.dump.age" || true
rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DAYS}d" --include "*.manifest.json" || true

_write_status 1
echo "Off-box backup complete and verified: $(basename "$ENCRYPTED_FILE")"
