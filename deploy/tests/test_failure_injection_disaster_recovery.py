"""M13 Phase 17: failure injection / disaster recovery.

The task names 15 scenarios. Most already have real, non-mocked proof
elsewhere in this repo -- duplicating that proof here would only make
it harder to find the real evidence, so this file follows the same
discipline backend/tests/test_disaster_recovery_scenarios.py (M12
Phase 14) established: cite where a scenario is already proven, and
write a NEW test only for a genuine gap.

  1. PostgreSQL unavailable during an
     API request                            -> test_integrated_production_session.py
                                                 test_F (Phase 16)
  2. PostgreSQL restart during normal
     traffic                                 -> test_integrated_production_session.py
                                                 test_G (Phase 16)
  3. Backend process termination             -> test_connectivity_failure.py
                                                 test_nginx_returns_a_clean_gateway_error...;
                                                 test_integrated_production_session.py test_H
  4. nginx/backend connection failure        -> test_connectivity_failure.py (same tests
                                                 as #3 -- nginx's view of a dead backend
                                                 IS this scenario)
  5. Interrupted migration                   -> backend/tests/test_disaster_recovery_scenarios.py
                                                 test_migration_interrupted_before_completion...;
                                                 deploy/tests/test_deploy_procedure.py
                                                 test_deploy_aborts_before_restarting_the_app...
  6. Failed backup                           -> NEW: test_backup_script_failure_is_detected...
                                                 below. The existing
                                                 test_a_failed_backup_status_file_fires...
                                                 (test_monitoring.py) proves the monitoring
                                                 half by writing a status file by hand; this
                                                 proves the SCRIPT itself, for real, detects
                                                 and reports its own failure.
  7. Corrupted backup artifact               -> test_backup_offbox.py
                                                 test_a_corrupted_remote_backup_is_detected...
  8. Restore to a clean database             -> test_backup_offbox.py (the full-cycle test
                                                 restores into a freshly recreated, empty
                                                 database); backend/tests/test_backup_restore.py
  9. Restore followed by application
     startup                                 -> NEW: test_restore_to_clean_database_followed...
                                                 below. Existing restore tests verify the
                                                 DATA via SQLAlchemy; none of them actually
                                                 boots the real application process against
                                                 the restored database and proves it serves
                                                 traffic.
 10. Duplicate/replayed client transaction
     after recovery                          -> NEW: test_duplicate_sale_retry_survives...
                                                 below. Phase 16's test_E proves retry
                                                 idempotency with no process boundary in
                                                 between; this proves it survives an actual
                                                 backend restart, so nothing in-memory could
                                                 be doing the de-duplication.
 11. Monitoring component unavailable       -> NEW: test_monitoring_exporter_itself_going...
                                                 below. A real gap was found while writing
                                                 this test: no existing alert rule covered
                                                 erp_exporter itself crashing (every other
                                                 alert's value comes FROM erp_exporter, so
                                                 its own death produces silence, not a firing
                                                 alert). Fixed by adding ERPMonitoringTargetDown
                                                 to deploy/prometheus/alerts.yml and
                                                 docs/RUNBOOKS.md Section 16.
 12. Disk-pressure condition                -> NEW: test_backup_fails_cleanly_under_disk_pressure
                                                 below.
 13. Invalid production secret/
     configuration                           -> backend/tests/test_production_secret_management.py;
                                                 test_integrated_production_session.py test_O
 14. Expired/invalid authentication
     session                                 -> NEW (thin but real): test_expired_access_token...
                                                 below. backend/tests/test_auth_hardening.py
                                                 already proves the JWT-expiry logic itself
                                                 exhaustively via TestClient; this proves the
                                                 same forged/expired token is rejected the
                                                 same way through the REAL nginx/TLS proxy,
                                                 not just the in-process app.
 15. Concurrent requests during service
     recovery                                -> NEW: test_concurrent_sale_requests_during...
                                                 below.

For every genuine gap: root cause, fix, permanent regression test below,
and the test passing is the rerun proof.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import jwt
import pytest
from conftest import BACKEND_DIR, BASE_URL, DB_URL, DEPLOY_DIR, SUDO, run, wait_for
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(BACKEND_DIR))

SUDO_POSTGRES = ["sudo", "-n", "-u", "postgres"]


def _uvicorn_pids(port: int = 8000) -> list[str]:
    result = subprocess.run(
        ["pgrep", "-f", f"uvicorn app.main:app.*--port {port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [pid for pid in result.stdout.split() if pid]


def _stop_backend() -> None:
    for pid in _uvicorn_pids():
        subprocess.run(["kill", "-TERM", pid], check=False)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _uvicorn_pids():
        time.sleep(0.2)


def _start_backend() -> None:
    subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    wait_for("http://127.0.0.1:8000/health", verify=False)


# --- shared fixture for scenarios that need a live store/user/product ------


@pytest.fixture(scope="module")
def local_stack_env(production_stack):
    from app.modules.auth.permissions import MANAGER
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_store,
        make_user_with_role,
        unique_suffix,
    )

    engine = create_engine(DB_URL, pool_pre_ping=True)
    db = Session(engine)
    store = make_store(db, name=f"Phase17 Store {unique_suffix()}")
    username = f"m13_phase17_manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    product = make_product(
        db,
        store,
        sku=f"PHASE17-{unique_suffix()}",
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.00"),
        current_qty_on_hand=Decimal(1000),
    )
    db.commit()

    login = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": username, "password": DEFAULT_TEST_PASSWORD},
    )
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    yield {
        "db": db,
        "store_id": store.id,
        "product_id": product.id,
        "headers": headers,
    }
    db.close()
    engine.dispose()


# --- 6: failed backup is detected and reported honestly, for real ----------


def test_backup_script_failure_is_detected_and_reported_honestly(
    tmp_path: Path,
) -> None:
    """Runs the REAL backup_offbox.sh against erp_dev (pg_dump is
    read-only -- never destructive) but points it at an rclone remote
    that does not exist, so step 3 (transport) genuinely fails. Proves
    the script's own on_failure trap (not a hand-written status file)
    reports the failure, exits nonzero, and never claims success."""
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    status_file = tmp_path / "backup_status.prom"

    age_keygen = run(["age-keygen", "-o", str(tmp_path / "identity.txt")])
    public_key = next(
        line.split(":", 1)[1].strip()
        for line in age_keygen.stderr.splitlines()
        if line.startswith("Public key:")
    )

    # A syntactically valid rclone config, but the remote name given to
    # the script does not appear in it -- rclone itself refuses with
    # "didn't find section" during step 3, after the local dump and
    # encryption (steps 1-2) have already genuinely succeeded.
    rclone_conf = tmp_path / "rclone.conf"
    rclone_conf.write_text("[unrelated_remote]\ntype = local\n")

    result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(local_dir),
            "nonexistent_remote:/tmp/does_not_matter",
            "14",
        ],
        env={
            **os.environ,
            "MIGRATIONS_DATABASE_URL": DB_URL,
            "BACKUP_AGE_PUBLIC_KEY": public_key,
            "RCLONE_CONFIG": str(rclone_conf),
            "BACKUP_STATUS_FILE": str(status_file),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0, "a backup script that cannot reach its remote must not exit 0"
    assert "BACKUP FAILED" in result.stderr

    assert status_file.exists(), "the script must write a status file even on failure"
    status_content = status_file.read_text()
    assert "erp_backup_last_success 0" in status_content, (
        f"a failed backup must never report success:\n{status_content}"
    )


# --- 9: restore to a clean database, then a real application startup -------


_RESTORE_DB_NAME = "erp_phase17_restore_test"
_OWNER_ROLE = "erp_user"
_OWNER_PASSWORD = "erp_password"


def _owner_url(db_name: str) -> str:
    return f"postgresql+psycopg://{_OWNER_ROLE}:{_OWNER_PASSWORD}@localhost:5432/{db_name}"


def _recreate_empty_database(db_name: str) -> None:
    run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", db_name])
    run([*SUDO_POSTGRES, "createdb", "-O", _OWNER_ROLE, db_name])


def test_restore_to_clean_database_followed_by_real_application_startup(
    tmp_path: Path,
) -> None:
    """test_backup_offbox.py and backend/tests/test_backup_restore.py
    both prove a restored database's ROWS are correct via SQLAlchemy --
    neither boots the actual application against the restored database.
    This does: dump erp_dev (read-only), restore into a fresh disposable
    database, then start a real uvicorn process bound to it and prove
    it serves real traffic, not just that a Session can query it."""
    from app.modules.auth.permissions import CASHIER
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_store,
        make_user_with_role,
    )

    # pg_dump runs as the `postgres` OS user (sudo) below, so the dump
    # directory must be traversable and writable by that user too.
    # pytest's tmp_path nests under /tmp/pytest-of-root/ (mode 0700,
    # owned by root) -- chmod'ing only the leaf directory isn't enough
    # because `postgres` still can't traverse its 0700 parents. A
    # dedicated, fully-open directory outside that hierarchy sidesteps
    # it entirely (same fix backend/tests/test_backup_restore.py already
    # uses for the identical reason).
    dump_dir = Path("/tmp/erp_phase17_restore_test")
    dump_dir.mkdir(mode=0o777, exist_ok=True)
    dump_dir.chmod(0o777)
    dump_path = dump_dir / "erp_dev.dump"
    run([*SUDO_POSTGRES, "pg_dump", "-Fc", "-f", str(dump_path), "-d", "erp_dev"])
    assert dump_path.exists() and dump_path.stat().st_size > 0

    # Seed one row we can prove came through the restored data (not a
    # coincidental pre-existing row) by querying it through the API.
    engine = create_engine(DB_URL)
    db = Session(engine)
    marker = f"phase17-restore-marker-{uuid.uuid4().hex[:8]}"
    store = make_store(db, name=marker)
    username = f"m13_p17_restore_user_{uuid.uuid4().hex[:8]}"
    make_user_with_role(db, store, CASHIER, username=username)
    make_product(db, store, sku=marker, current_price=Decimal("9.99"))
    db.commit()
    store_id = store.id
    db.close()
    engine.dispose()

    # Re-dump now that the marker row exists, then restore into a
    # disposable database.
    run([*SUDO_POSTGRES, "pg_dump", "-Fc", "-f", str(dump_path), "-d", "erp_dev"])
    _recreate_empty_database(_RESTORE_DB_NAME)
    run(
        [
            *SUDO_POSTGRES,
            "pg_restore",
            "--clean",
            "--if-exists",
            "-d",
            _RESTORE_DB_NAME,
            str(dump_path),
        ]
    )

    restore_log = tmp_path / "uvicorn_restore.log"
    with restore_log.open("w") as log_fh:
        proc = subprocess.Popen(
            [
                "python",
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8098",
            ],
            cwd=str(BACKEND_DIR),
            env={**os.environ, "DATABASE_URL": _owner_url(_RESTORE_DB_NAME)},
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    try:
        wait_for("http://127.0.0.1:8098/health", verify=False, timeout=15.0)
        health_db = httpx.get("http://127.0.0.1:8098/health/db", timeout=5.0)
        assert health_db.status_code == 200
        assert health_db.json() == {"status": "ok", "database": "reachable"}

        login = httpx.post(
            "http://127.0.0.1:8098/api/v1/auth/login",
            json={"username": username, "password": DEFAULT_TEST_PASSWORD},
            timeout=5.0,
        )
        assert login.status_code == 200, (
            f"the restored user must be able to log in through the real app:\n{login.text}"
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        products = httpx.get(
            "http://127.0.0.1:8098/api/v1/products",
            headers=headers,
            params={"store_id": store_id},
            timeout=5.0,
        )
        assert products.status_code == 200
        assert any(p["sku"] == marker for p in products.json()), (
            "the marker product created before the dump must be present after restore"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", _RESTORE_DB_NAME])
        if dump_path.exists():
            dump_path.unlink()


# --- 10: duplicate/replayed transaction survives a real recovery event -----


def test_duplicate_sale_retry_survives_a_backend_restart(local_stack_env) -> None:
    """Phase 16's test_E proves retry idempotency with the backend
    process never restarting in between -- leaving open the question of
    whether de-duplication is DB-backed (safe) or could ever be relying
    on anything in-memory (unsafe). This proves it by actually killing
    and restarting the backend between the original request and the
    identical retry."""
    from app.modules.accounting.models import JournalEntry
    from app.modules.inventory.models import InventoryMovement
    from app.modules.sales.models import Sale

    client_transaction_id = f"phase17-duprestart-{uuid.uuid4()}"
    body = {
        "store_id": local_stack_env["store_id"],
        "client_transaction_id": client_transaction_id,
        "lines": [{"product_id": local_stack_env["product_id"], "quantity": "4"}],
        "payments": [{"payment_method": "CASH", "amount": "40.00"}],
    }

    first = httpx.post(
        f"{BASE_URL}/api/v1/sales",
        verify=False,
        headers=local_stack_env["headers"],
        json=body,
    )
    assert first.status_code == 201, first.text
    sale_id = first.json()["id"]

    _stop_backend()
    _start_backend()
    wait_for(f"{BASE_URL}/health", verify=False)

    retry = httpx.post(
        f"{BASE_URL}/api/v1/sales",
        verify=False,
        headers=local_stack_env["headers"],
        json=body,
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == sale_id, "a post-restart retry must return the SAME sale"

    db = local_stack_env["db"]
    db.expire_all()
    sale_count = db.execute(
        select(func.count())
        .select_from(Sale)
        .where(Sale.client_transaction_id == client_transaction_id)
    ).scalar_one()
    assert sale_count == 1

    entry_count = db.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.source_type == "SALE", JournalEntry.source_id == sale_id)
    ).scalar_one()
    assert entry_count == 1, "a post-restart retry must not post a second journal entry"

    movement_count = db.execute(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.reference_type == "sale",
            InventoryMovement.reference_id == sale_id,
        )
    ).scalar_one()
    assert movement_count == 1, "a post-restart retry must not create a second inventory movement"
    db.close()


# --- 11: a monitoring component itself going down is detected --------------


def test_monitoring_exporter_itself_going_down_fires_an_alert() -> None:
    """The genuine gap found while writing this file: killing
    erp_exporter used to be invisible to every alert in this system,
    because every alert's expression is computed FROM erp_exporter's
    own scrape. Fixed by adding ERPMonitoringTargetDown (deploy/
    prometheus/alerts.yml) -- this proves it fires when the exporter
    process dies and clears when it comes back, using Prometheus's own
    `up` metric rather than anything erp_exporter reports about itself."""
    import re

    from test_monitoring import ALERTMANAGER_PORT, ERP_EXPORTER_PORT, PROMETHEUS_PORT

    port_offset = 2  # avoid colliding with test_monitoring.py's own base ports
    work_dir = Path(tempfile.mkdtemp(prefix="erp_m13_p17_monitoring_"))

    real_alerts = (DEPLOY_DIR / "prometheus" / "alerts.yml").read_text()
    test_alerts = re.sub(r"for: \d+m\b", "for: 3s", real_alerts)
    (work_dir / "alerts.yml").write_text(test_alerts)
    (work_dir / "prometheus.yml").write_text(
        f"""
global:
  scrape_interval: 1s
  evaluation_interval: 1s
rule_files:
  - "{work_dir}/alerts.yml"
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:{ALERTMANAGER_PORT + port_offset}"]
scrape_configs:
  - job_name: "erp_exporter"
    static_configs:
      - targets: ["127.0.0.1:{ERP_EXPORTER_PORT + port_offset}"]
"""
    )
    (work_dir / "alertmanager.yml").write_text(
        "route:\n  receiver: default\n  group_wait: 1s\nreceivers:\n  - name: default\n"
    )

    procs: list[subprocess.Popen] = []
    try:
        erp_exp = subprocess.Popen(
            [
                sys.executable,
                str(DEPLOY_DIR / "prometheus" / "erp_exporter.py"),
                "--port",
                str(ERP_EXPORTER_PORT + port_offset),
                "--app-base-url",
                "http://127.0.0.1:8000",
                "--nginx-log",
                "/var/log/nginx/access.log",
                "--db-url",
                DB_URL.replace("+psycopg", ""),
                "--interval",
                "1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(erp_exp)
        am = subprocess.Popen(
            [
                "/usr/bin/prometheus-alertmanager",
                f"--config.file={work_dir}/alertmanager.yml",
                f"--web.listen-address=127.0.0.1:{ALERTMANAGER_PORT + port_offset}",
                f"--storage.path={work_dir}/am_data",
                "--cluster.listen-address=",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(am)
        prom = subprocess.Popen(
            [
                "/usr/bin/prometheus",
                f"--config.file={work_dir}/prometheus.yml",
                f"--storage.tsdb.path={work_dir}/prom_data",
                f"--web.listen-address=127.0.0.1:{PROMETHEUS_PORT + port_offset}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(prom)

        prom_url = f"http://127.0.0.1:{PROMETHEUS_PORT + port_offset}"

        def _target_up(job: str) -> bool:
            resp = httpx.get(f"{prom_url}/api/v1/targets", timeout=3.0)
            targets = resp.json()["data"]["activeTargets"]
            return any(t["labels"]["job"] == job and t["health"] == "up" for t in targets)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _target_up("erp_exporter"):
            time.sleep(0.5)
        assert _target_up("erp_exporter"), "erp_exporter never came up in the isolated stack"

        erp_exp.terminate()
        erp_exp.wait(timeout=10)
        procs.remove(erp_exp)

        deadline = time.monotonic() + 30
        fired = False
        while time.monotonic() < deadline:
            resp = httpx.get(f"{prom_url}/api/v1/alerts", timeout=3.0)
            states = {a["labels"]["alertname"]: a["state"] for a in resp.json()["data"]["alerts"]}
            if states.get("ERPMonitoringTargetDown") == "firing":
                fired = True
                break
            time.sleep(1.0)
        assert fired, "ERPMonitoringTargetDown never fired after erp_exporter was killed"

        erp_exp = subprocess.Popen(
            [
                sys.executable,
                str(DEPLOY_DIR / "prometheus" / "erp_exporter.py"),
                "--port",
                str(ERP_EXPORTER_PORT + port_offset),
                "--app-base-url",
                "http://127.0.0.1:8000",
                "--nginx-log",
                "/var/log/nginx/access.log",
                "--db-url",
                DB_URL.replace("+psycopg", ""),
                "--interval",
                "1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(erp_exp)

        deadline = time.monotonic() + 30
        resolved = False
        while time.monotonic() < deadline:
            resp = httpx.get(f"{prom_url}/api/v1/alerts", timeout=3.0)
            names = {a["labels"]["alertname"] for a in resp.json()["data"]["alerts"]}
            if "ERPMonitoringTargetDown" not in names:
                resolved = True
                break
            time.sleep(1.0)
        assert resolved, "ERPMonitoringTargetDown never cleared after erp_exporter recovered"
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# --- 12: disk-pressure condition ---------------------------------------------


def test_backup_fails_cleanly_under_disk_pressure(tmp_path: Path) -> None:
    """A tiny, isolated tmpfs mount (never the shared host disk -- that
    would risk the whole sandbox) stands in for a full disk. Proves
    pg_dump genuinely fails on ENOSPC and backup_offbox.sh reports it
    as a failure rather than silently accepting a truncated dump."""
    mount_point = tmp_path / "tiny_disk"
    mount_point.mkdir()
    run([*SUDO, "mount", "-t", "tmpfs", "-o", "size=200k", "tmpfs", str(mount_point)])
    try:
        result = subprocess.run(
            [str(BACKEND_DIR / "scripts" / "backup_database.sh"), str(mount_point)],
            env={**os.environ, "MIGRATIONS_DATABASE_URL": DB_URL},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode != 0, (
            "pg_dump into a 200KB filesystem must fail, not silently truncate: "
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "no space" in combined or "disk" in combined or "space" in combined, (
            f"expected a clear disk-space failure message, got:\n{result.stdout}\n{result.stderr}"
        )
    finally:
        run([*SUDO, "umount", str(mount_point)])


# --- 14: expired/invalid authentication session, through the real proxy ----


def test_expired_access_token_is_rejected_through_the_real_proxy(
    local_stack_env,
) -> None:
    """backend/tests/test_auth_hardening.py proves JWT-expiry rejection
    exhaustively via an in-process TestClient. This proves the identical
    forged/expired token is rejected the same way through the REAL
    nginx/TLS proxy -- nginx must not cache, retry, or otherwise paper
    over an expired credential on its way to the backend."""
    from app.core.config import get_settings

    settings = get_settings()
    now = datetime.now(UTC)

    db = local_stack_env["db"]
    from app.modules.auth.models import User

    user_id = httpx.get(
        f"{BASE_URL}/api/v1/auth/me", verify=False, headers=local_stack_env["headers"]
    ).json()["id"]
    user = db.get(User, user_id)

    expired_token = jwt.encode(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "access",
            "iat": now - timedelta(minutes=30),
            "exp": now - timedelta(minutes=15),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers={"Authorization": f"Bearer {expired_token}"},
        params={"store_id": local_stack_env["store_id"]},
    )
    assert resp.status_code == 401
    assert "erp_app" not in resp.text and "secret" not in resp.text.lower()

    # A syntactically-forged token (wrong secret) is rejected the same way.
    forged = jwt.encode(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        "an-attacker-controlled-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    resp2 = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers={"Authorization": f"Bearer {forged}"},
        params={"store_id": local_stack_env["store_id"]},
    )
    assert resp2.status_code == 401


# --- 15: concurrent requests during service recovery ------------------------


def test_concurrent_sale_requests_during_service_recovery(local_stack_env) -> None:
    """Fires a burst of distinct sale requests while the backend is
    mid-restart, waits for full recovery, then retries (with the SAME
    client_transaction_id) every request that did not get a clean 201.
    Proves: no duplicate sale is ever created for one transaction id,
    inventory decreases by exactly the sum of the successful sales'
    quantities, and every posted journal entry balances -- i.e. the
    recovery window produces clean failures or clean successes, never a
    partial financial effect."""
    from app.modules.accounting.models import JournalEntry, JournalLine
    from app.modules.inventory.models import InventoryMovement
    from app.modules.products.models import Product
    from app.modules.sales.models import Sale

    # local_stack_env's product is shared (module-scoped fixture) with
    # earlier tests in this file that also sell it (e.g. scenario 10's
    # retry test) -- snapshot the quantity actually on hand right now
    # rather than assuming the fixture's original seed value.
    db = local_stack_env["db"]
    db.expire_all()
    qty_before = db.get(Product, local_stack_env["product_id"]).current_qty_on_hand

    n_requests = 12
    transaction_ids = [f"phase17-concurrent-{uuid.uuid4()}" for _ in range(n_requests)]
    results: dict[str, httpx.Response | Exception] = {}
    lock = threading.Lock()

    def _attempt(client_transaction_id: str) -> None:
        try:
            resp = httpx.post(
                f"{BASE_URL}/api/v1/sales",
                verify=False,
                headers=local_stack_env["headers"],
                json={
                    "store_id": local_stack_env["store_id"],
                    "client_transaction_id": client_transaction_id,
                    "lines": [{"product_id": local_stack_env["product_id"], "quantity": "1"}],
                    "payments": [{"payment_method": "CASH", "amount": "10.00"}],
                },
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001 -- a connection failure is an expected outcome here
            resp = exc
        with lock:
            results[client_transaction_id] = resp

    threads = [threading.Thread(target=_attempt, args=(tid,)) for tid in transaction_ids]
    for t in threads:
        t.start()
    time.sleep(0.05)
    _stop_backend()
    _start_backend()
    for t in threads:
        t.join(timeout=30)

    wait_for(f"{BASE_URL}/health", verify=False)

    # Retry (same client_transaction_id, idempotent by design) anything
    # that did not get a clean 201 the first time around.
    for tid in transaction_ids:
        first = results[tid]
        if isinstance(first, Exception) or first.status_code != 201:
            retry = httpx.post(
                f"{BASE_URL}/api/v1/sales",
                verify=False,
                headers=local_stack_env["headers"],
                json={
                    "store_id": local_stack_env["store_id"],
                    "client_transaction_id": tid,
                    "lines": [{"product_id": local_stack_env["product_id"], "quantity": "1"}],
                    "payments": [{"payment_method": "CASH", "amount": "10.00"}],
                },
                timeout=15.0,
            )
            assert retry.status_code == 201, (
                f"post-recovery retry for {tid} must succeed: {retry.status_code} {retry.text}"
            )
            results[tid] = retry

    db = local_stack_env["db"]
    db.expire_all()

    sale_ids: set[int] = set()
    for tid in transaction_ids:
        count = db.execute(
            select(func.count()).select_from(Sale).where(Sale.client_transaction_id == tid)
        ).scalar_one()
        assert count == 1, f"transaction {tid} must resolve to exactly one sale, got {count}"
        sale = db.execute(select(Sale).where(Sale.client_transaction_id == tid)).scalar_one()
        sale_ids.add(sale.id)

    assert len(sale_ids) == n_requests, (
        "every distinct client_transaction_id must be a distinct sale"
    )

    for sale_id in sale_ids:
        entry = (
            db.execute(
                select(JournalEntry).where(
                    JournalEntry.source_type == "SALE",
                    JournalEntry.source_id == sale_id,
                )
            )
            .scalars()
            .first()
        )
        assert entry is not None, f"sale {sale_id} has no posted journal entry"
        lines = (
            db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
            .scalars()
            .all()
        )
        total_debit = sum(line.debit for line in lines)
        total_credit = sum(line.credit for line in lines)
        assert total_debit == total_credit, f"journal entry {entry.id} does not balance"

        movement_count = db.execute(
            select(func.count())
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.reference_type == "sale",
                InventoryMovement.reference_id == sale_id,
            )
        ).scalar_one()
        assert movement_count == 1, f"sale {sale_id} must have exactly one inventory movement"

    product = db.get(Product, local_stack_env["product_id"])
    db.refresh(product)
    expected_qty = qty_before - Decimal(n_requests)
    assert product.current_qty_on_hand == expected_qty, (
        f"inventory must decrease by exactly {n_requests} units total across all "
        f"{n_requests} distinct sales, no more and no less"
    )
    db.close()
