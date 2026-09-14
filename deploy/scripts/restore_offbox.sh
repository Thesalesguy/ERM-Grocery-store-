#!/usr/bin/env bash
# M13 Phase 5: restore from an off-box backup created by
# backup_offbox.sh. Downloads the encrypted dump + its manifest,
# decrypts, and — critically — verifies the decrypted plaintext's
# SHA-256 against the manifest BEFORE ever handing it to pg_restore.
# A truncated/corrupted/tampered backup is refused here, loudly, rather
# than silently fed into pg_restore (which might partially succeed on a
# truncated custom-format dump, leaving an operator with a
# plausible-looking but actually incomplete database and no warning).
#
# Usage:
#   restore_offbox.sh <rclone_remote> <encrypted_filename> <staging_dir> <target_db_name>
#
# Requires the `age` PRIVATE key to be available locally as
# $AGE_IDENTITY_FILE (default: /etc/erp/age_identity.txt) — this file
# must NEVER live on the same host as the primary database in a real
# deployment (docs/M13_HARDENING_AUDIT.md Section 5); it exists here
# only because this sandbox's test needs to actually decrypt what it
# encrypted to prove the round trip works.
set -euo pipefail

RCLONE_REMOTE="${1:?Usage: restore_offbox.sh <rclone_remote> <encrypted_filename> <staging_dir> <target_db_name>}"
ENCRYPTED_FILENAME="${2:?Usage: restore_offbox.sh <rclone_remote> <encrypted_filename> <staging_dir> <target_db_name>}"
STAGING_DIR="${3:?Usage: restore_offbox.sh <rclone_remote> <encrypted_filename> <staging_dir> <target_db_name>}"
TARGET_DB="${4:?Usage: restore_offbox.sh <rclone_remote> <encrypted_filename> <staging_dir> <target_db_name>}"
AGE_IDENTITY_FILE="${AGE_IDENTITY_FILE:-/etc/erp/age_identity.txt}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/../../backend" && pwd)"

mkdir -p "$STAGING_DIR"

DUMP_BASENAME="${ENCRYPTED_FILENAME%.age}"
MANIFEST_FILENAME="${DUMP_BASENAME}.manifest.json"

echo "=== 1/4: download encrypted backup + manifest from ${RCLONE_REMOTE} ==="
rclone copy "${RCLONE_REMOTE}/${ENCRYPTED_FILENAME}" "$STAGING_DIR" --checksum
rclone copy "${RCLONE_REMOTE}/${MANIFEST_FILENAME}" "$STAGING_DIR" --checksum

echo "=== 2/4: decrypt ==="
DECRYPTED_FILE="${STAGING_DIR}/${DUMP_BASENAME}"
age -d -i "$AGE_IDENTITY_FILE" -o "$DECRYPTED_FILE" "${STAGING_DIR}/${ENCRYPTED_FILENAME}"

echo "=== 3/4: verify checksum against the manifest — REFUSE if it doesn't match ==="
EXPECTED_CHECKSUM="$(python3 -c "import json; print(json.load(open('${STAGING_DIR}/${MANIFEST_FILENAME}'))['sha256_plaintext'])")"
ACTUAL_CHECKSUM="$(sha256sum "$DECRYPTED_FILE" | cut -d' ' -f1)"
if [ "$EXPECTED_CHECKSUM" != "$ACTUAL_CHECKSUM" ]; then
    echo "CHECKSUM MISMATCH — this backup is corrupted or was tampered with." >&2
    echo "  expected: $EXPECTED_CHECKSUM" >&2
    echo "  actual:   $ACTUAL_CHECKSUM" >&2
    echo "Refusing to restore. Try an earlier backup." >&2
    rm -f "$DECRYPTED_FILE"
    exit 1
fi
echo "Checksum verified: $ACTUAL_CHECKSUM"

echo "=== 4/4: restore into ${TARGET_DB} ==="
# Deliberately does NOT shell out to backend/scripts/restore_database.sh
# for the drop/recreate step: that script assumes the invoking OS user
# already has CREATEDB (a real deployment's operator account, set up
# once at provisioning time) -- this sandbox only grants that via
# `sudo -n -u postgres` peer auth (the same constraint every other test
# in this repo that creates/drops a database already works around, see
# backend/tests/test_backup_restore.py). Uses createdb/dropdb/pg_restore
# directly here so this script works the same way in both contexts: a
# real deployment's operator account and this sandbox's sudo-only path.
if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
    DB_ADMIN_PREFIX=(sudo -n -u postgres)
else
    DB_ADMIN_PREFIX=()
fi
"${DB_ADMIN_PREFIX[@]}" dropdb --if-exists --force "$TARGET_DB"
"${DB_ADMIN_PREFIX[@]}" createdb -O erp_user "$TARGET_DB"
# Explicit TCP connection as the schema-owning role (erp_user) rather
# than relying on ambient PGHOST/PGUSER/peer auth, which this sandbox's
# `root` OS user does not have a matching Postgres role for.
PGPASSWORD="${RESTORE_OWNER_PASSWORD:-erp_password}" pg_restore \
    --clean --if-exists \
    -h 127.0.0.1 -U "${RESTORE_OWNER_ROLE:-erp_user}" \
    -d "$TARGET_DB" "$DECRYPTED_FILE"

echo "Restore complete. Verify with:"
echo "  python -m scripts.integrity_snapshot postgresql+psycopg://erp_user:<password>@localhost:5432/${TARGET_DB}"
