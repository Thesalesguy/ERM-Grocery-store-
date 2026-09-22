"""M13 Phase 5: off-box backup and recovery.

M12 already proved the LOCAL pg_dump/pg_restore cycle exhaustively
(backend/tests/test_backup_restore.py) against a realistic dataset,
including accounting/inventory/AP integrity across the round trip via
scripts/integrity_snapshot.py. This file does not repeat that — it
proves what M12 explicitly did not build: encryption
(deploy/scripts/backup_offbox.sh), transport to a destination that is
not this host, checksum verification, and — critically — that a
corrupted/tampered backup is DETECTED and REFUSED rather than silently
fed into pg_restore.

"Off-box" here means an `rclone` `local`-type remote pointing at a
directory this script treats as opaque — the same `rclone copy`/
`rclone check` commands run unchanged against a real S3-compatible
endpoint (MinIO or a cloud bucket); only the remote's URL/config
changes (docs/M13_DESIGN.md Section 5). This sandbox has no network
path to a real object-storage endpoint to prove the S3 HTTP layer
itself (attempted: dl.min.io is unreachable through this environment's
egress proxy) — stated as a limitation, not hidden.

Runs against a dedicated, disposable `erp_offbox_test` database created
and destroyed by this file (never erp_dev/erp_test), following the
exact `sudo -n -u postgres` lifecycle pattern
backend/tests/test_backup_restore.py already established.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEPLOY_DIR = REPO_ROOT / "deploy"
SUDO_POSTGRES = ["sudo", "-n", "-u", "postgres"]

_TEST_DB_NAME = "erp_offbox_test"
_RESTORE_DB_NAME = "erp_offbox_restore_test"
_OWNER_ROLE = "erp_user"
_OWNER_PASSWORD = "erp_password"

sys.path.insert(0, str(BACKEND_DIR))


def _owner_url(db_name: str) -> str:
    return f"postgresql+psycopg://{_OWNER_ROLE}:{_OWNER_PASSWORD}@localhost:5432/{db_name}"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (
        f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _recreate_empty_database(db_name: str) -> None:
    _run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", db_name])
    _run([*SUDO_POSTGRES, "createdb", "-O", _OWNER_ROLE, db_name])


def _bootstrap_app_role(db_name: str) -> None:
    bootstrap_sql = BACKEND_DIR / "scripts" / "bootstrap_db_roles.sql"
    _run([*SUDO_POSTGRES, "psql", "-d", db_name, "-v", "ON_ERROR_STOP=1", "-f", str(bootstrap_sql)])


def _migrate_to_head(db_name: str) -> None:
    env = {**os.environ, "MIGRATIONS_DATABASE_URL": _owner_url(db_name)}
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"migration failed:\n{result.stdout}\n{result.stderr}"


def _seed_small_dataset(db: Session) -> dict:
    from decimal import Decimal

    from app.modules.auth.permissions import CASHIER
    from app.modules.sales import service as sales_service
    from app.modules.sales.service import PaymentInput, SaleLineInput
    from tests.factories import make_product, make_store, make_user_with_role

    store = make_store(db, name="Offbox Test Store")
    cashier = make_user_with_role(db, store, CASHIER, username="offbox_cashier")
    product = make_product(
        db, store, current_price=Decimal("12.50"), current_qty_on_hand=Decimal("50")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id="offbox-seed-1",
        caller_store_id=store.id,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("25.00"))],
    )
    db.commit()
    return {"store_id": store.id, "product_id": product.id, "sale_id": sale.id}


@pytest.fixture
def offbox_env():
    """A dedicated seeded database, a local `age` keypair, a local
    rclone `local`-type remote (a directory standing in for off-box
    storage), and the local backup staging directory — all disposable,
    created here and torn down at the end."""
    _recreate_empty_database(_TEST_DB_NAME)
    _bootstrap_app_role(_TEST_DB_NAME)
    _migrate_to_head(_TEST_DB_NAME)

    engine = create_engine(_owner_url(_TEST_DB_NAME))
    db = Session(bind=engine)
    seed_ids = _seed_small_dataset(db)
    db.close()
    engine.dispose()

    with tempfile.TemporaryDirectory(prefix="erp_offbox_") as tmp:
        tmp_path = Path(tmp)
        local_backup_dir = tmp_path / "local_backups"
        remote_dir = tmp_path / "simulated_offbox_remote"
        age_dir = tmp_path / "age"
        local_backup_dir.mkdir()
        remote_dir.mkdir()
        age_dir.mkdir()

        _run(["age-keygen", "-o", str(age_dir / "identity.txt")])
        identity_text = (age_dir / "identity.txt").read_text()
        public_key = next(
            line.split(": ", 1)[1].strip()
            for line in identity_text.splitlines()
            if line.startswith("# public key:")
        )

        rclone_conf = tmp_path / "rclone.conf"
        rclone_conf.write_text("[offbox_test]\ntype = local\n")

        yield {
            "db_url": _owner_url(_TEST_DB_NAME),
            "local_backup_dir": local_backup_dir,
            "remote_dir": remote_dir,
            "rclone_remote": f"offbox_test:{remote_dir}",
            "rclone_conf": rclone_conf,
            "age_public_key": public_key,
            "age_identity_file": age_dir / "identity.txt",
            "seed_ids": seed_ids,
        }

    _run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", _TEST_DB_NAME])
    _run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", _RESTORE_DB_NAME])


def _backup_env(offbox_env: dict) -> dict:
    return {
        **os.environ,
        "MIGRATIONS_DATABASE_URL": offbox_env["db_url"],
        "BACKUP_AGE_PUBLIC_KEY": offbox_env["age_public_key"],
        "RCLONE_CONFIG": str(offbox_env["rclone_conf"]),
        "BACKUP_STATUS_FILE": str(offbox_env["local_backup_dir"] / "backup_status.prom"),
    }


def test_full_offbox_backup_and_restore_cycle_preserves_integrity(offbox_env: dict) -> None:
    backup_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(offbox_env["local_backup_dir"]),
            offbox_env["rclone_remote"],
            "14",
        ],
        env=_backup_env(offbox_env),
        capture_output=True,
        text=True,
    )
    assert backup_result.returncode == 0, (
        f"backup failed:\n{backup_result.stdout}\n{backup_result.stderr}"
    )

    encrypted_files = list(offbox_env["remote_dir"].glob("*.dump.age"))
    manifest_files = list(offbox_env["remote_dir"].glob("*.manifest.json"))
    assert len(encrypted_files) == 1, (
        f"expected exactly one encrypted backup, got {encrypted_files}"
    )
    assert len(manifest_files) == 1

    status_file = offbox_env["local_backup_dir"] / "backup_status.prom"
    assert status_file.exists()
    assert "erp_backup_last_success 1" in status_file.read_text()

    # Take an integrity snapshot BEFORE restoring, from the source DB.
    sys.path.insert(0, str(BACKEND_DIR))
    from scripts.integrity_snapshot import take_snapshot_from_url

    before_snapshot = take_snapshot_from_url(offbox_env["db_url"])

    restore_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "restore_offbox.sh"),
            offbox_env["rclone_remote"],
            encrypted_files[0].name,
            str(offbox_env["local_backup_dir"] / "restore_staging"),
            _RESTORE_DB_NAME,
        ],
        env={
            **os.environ,
            "RCLONE_CONFIG": str(offbox_env["rclone_conf"]),
            "AGE_IDENTITY_FILE": str(offbox_env["age_identity_file"]),
        },
        capture_output=True,
        text=True,
    )
    assert restore_result.returncode == 0, (
        f"restore failed:\n{restore_result.stdout}\n{restore_result.stderr}"
    )
    assert "Checksum verified" in restore_result.stdout

    after_snapshot = take_snapshot_from_url(_owner_url(_RESTORE_DB_NAME))
    assert before_snapshot == after_snapshot, (
        f"integrity snapshot mismatch after off-box restore: "
        f"{set(before_snapshot.to_dict().items()) ^ set(after_snapshot.to_dict().items())}"
    )


def test_a_corrupted_remote_backup_is_detected_and_restore_is_refused(
    offbox_env: dict,
) -> None:
    backup_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(offbox_env["local_backup_dir"]),
            offbox_env["rclone_remote"],
            "14",
        ],
        env=_backup_env(offbox_env),
        capture_output=True,
        text=True,
    )
    assert backup_result.returncode == 0

    encrypted_files = list(offbox_env["remote_dir"].glob("*.dump.age"))
    assert len(encrypted_files) == 1
    corrupted_file = encrypted_files[0]

    # Corrupt the REMOTE copy directly -- simulating bit rot, a failed
    # transfer, or tampering after the fact, not a bad local dump.
    original_bytes = corrupted_file.read_bytes()
    corrupted_bytes = bytearray(original_bytes)
    midpoint = len(corrupted_bytes) // 2
    for i in range(midpoint, min(midpoint + 64, len(corrupted_bytes))):
        corrupted_bytes[i] ^= 0xFF
    corrupted_file.write_bytes(bytes(corrupted_bytes))

    restore_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "restore_offbox.sh"),
            offbox_env["rclone_remote"],
            corrupted_file.name,
            str(offbox_env["local_backup_dir"] / "restore_staging_corrupt"),
            _RESTORE_DB_NAME,
        ],
        env={
            **os.environ,
            "RCLONE_CONFIG": str(offbox_env["rclone_conf"]),
            "AGE_IDENTITY_FILE": str(offbox_env["age_identity_file"]),
        },
        capture_output=True,
        text=True,
    )
    assert restore_result.returncode != 0, (
        "restore_offbox.sh must refuse a corrupted backup, not succeed silently"
    )
    combined_output = restore_result.stdout + restore_result.stderr
    assert (
        "CHECKSUM MISMATCH" in combined_output
        or "age: error" in combined_output.lower()
        or "failed to decrypt" in combined_output.lower()
    ), f"expected a clear corruption/decryption failure message, got:\n{combined_output}"

    # The target database must never have been created/populated from
    # corrupted data.
    check = subprocess.run(
        [
            *SUDO_POSTGRES,
            "psql",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{_RESTORE_DB_NAME}'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.stdout.strip() != "1", (
        "the restore target database must not exist after a refused corrupt restore"
    )


def test_a_tampered_manifest_checksum_is_detected_and_restore_is_refused(
    offbox_env: dict,
) -> None:
    """M13 Phase 19 (mutation testing): the previous test's byte-flip
    corruption is always caught by `age`'s own authenticated encryption
    failing to decrypt at all, before the manual checksum comparison in
    restore_offbox.sh ever runs -- so disabling that checksum comparison
    entirely was NOT detected by the existing suite (a real mutation-
    testing finding, not a bug). This isolates the checksum step on its
    own: the encrypted file decrypts perfectly cleanly (untouched), but
    the MANIFEST's recorded checksum is tampered, so only the checksum
    comparison itself -- not `age` -- can catch it."""
    backup_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(offbox_env["local_backup_dir"]),
            offbox_env["rclone_remote"],
            "14",
        ],
        env=_backup_env(offbox_env),
        capture_output=True,
        text=True,
    )
    assert backup_result.returncode == 0

    manifest_files = list(offbox_env["remote_dir"].glob("*.manifest.json"))
    assert len(manifest_files) == 1
    manifest_file = manifest_files[0]

    manifest = json.loads(manifest_file.read_text())
    manifest["sha256_plaintext"] = "0" * 64
    manifest_file.write_text(json.dumps(manifest))

    encrypted_files = list(offbox_env["remote_dir"].glob("*.dump.age"))
    assert len(encrypted_files) == 1

    restore_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "restore_offbox.sh"),
            offbox_env["rclone_remote"],
            encrypted_files[0].name,
            str(offbox_env["local_backup_dir"] / "restore_staging_tampered_manifest"),
            _RESTORE_DB_NAME,
        ],
        env={
            **os.environ,
            "RCLONE_CONFIG": str(offbox_env["rclone_conf"]),
            "AGE_IDENTITY_FILE": str(offbox_env["age_identity_file"]),
        },
        capture_output=True,
        text=True,
    )
    assert restore_result.returncode != 0, (
        "restore_offbox.sh must refuse a backup whose manifest checksum doesn't match"
    )
    combined_output = restore_result.stdout + restore_result.stderr
    assert "CHECKSUM MISMATCH" in combined_output, (
        f"expected the checksum-comparison step itself to catch this, got:\n{combined_output}"
    )

    check = subprocess.run(
        [
            *SUDO_POSTGRES,
            "psql",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{_RESTORE_DB_NAME}'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.stdout.strip() != "1", (
        "the restore target database must not exist after a refused checksum-mismatch restore"
    )


def test_retention_cleanup_removes_backups_older_than_the_window(offbox_env: dict) -> None:
    """A backup older than the retention window must be deleted from
    the remote by the next backup run's cleanup step."""
    import time

    old_encrypted = offbox_env["remote_dir"] / "erp_offbox_test_19990101T000000Z.dump.age"
    old_manifest = offbox_env["remote_dir"] / "erp_offbox_test_19990101T000000Z.dump.manifest.json"
    old_encrypted.write_bytes(b"stale encrypted content")
    old_manifest.write_text(json.dumps({"dump_filename": "stale", "sha256_plaintext": "x"}))
    old_timestamp = time.time() - (30 * 86400)
    os.utime(old_encrypted, (old_timestamp, old_timestamp))
    os.utime(old_manifest, (old_timestamp, old_timestamp))

    backup_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(offbox_env["local_backup_dir"]),
            offbox_env["rclone_remote"],
            "14",
        ],
        env=_backup_env(offbox_env),
        capture_output=True,
        text=True,
    )
    assert backup_result.returncode == 0, (
        f"backup failed:\n{backup_result.stdout}\n{backup_result.stderr}"
    )

    assert not old_encrypted.exists(), (
        "a 30-day-old backup must be cleaned up under a 14-day retention"
    )
    assert not old_manifest.exists()
    # The fresh backup this run just made must still be present.
    assert list(offbox_env["remote_dir"].glob("*.dump.age"))
