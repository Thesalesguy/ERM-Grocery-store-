"""M13 Phase 16: permanent integrated production-topology test session.

One continuous session against the REAL stack -- client -> nginx/TLS ->
FastAPI -> PostgreSQL -> monitoring/exporters -- covering the 15 named
scenarios (A-O). Nothing here is mocked: nginx, PostgreSQL,
authentication, and the monitoring path are all real processes, reused
from earlier M13 phases' own fixtures (conftest.py's `production_stack`,
test_monitoring.py's `monitoring_stack`) rather than re-implemented.

Tests run in file order deliberately (pytest's default, no reordering
plugin in this repo) -- later tests depend on state earlier ones create
(a store, a product, a finalized sale) and on services earlier tests
deliberately broke being restored before the session continues. This
mirrors a real single support/ops session working through a checklist,
not fifteen independent unit tests.

Where a scenario already has a dedicated, deeper test elsewhere (e.g.
Phase 4's full rate-limiting suite, Phase 6's full monitoring suite),
this file exercises the same real mechanism but through the INTEGRATED
session's own state (the same sale, the same correlation ID, the same
login) rather than duplicating that file's exhaustive edge cases.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from decimal import Decimal

import httpx
import pytest
from conftest import BACKEND_DIR, BASE_URL, DB_URL, SUDO, run, wait_for
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(BACKEND_DIR))

_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"


def _uvicorn_pids() -> list[str]:
    result = subprocess.run(
        ["pgrep", "-f", "uvicorn app.main:app.*--port 8000"], capture_output=True, text=True
    )
    return [pid for pid in result.stdout.split() if pid]


def _stop_backend() -> None:
    for pid in _uvicorn_pids():
        subprocess.run(["kill", "-TERM", pid])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _uvicorn_pids():
        time.sleep(0.2)


def _start_backend() -> None:
    subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    wait_for("http://127.0.0.1:8000/health", verify=False)


@pytest.fixture(scope="module")
def session_setup(production_stack):
    """One-time setup for the whole integrated session: two stores (for
    store-isolation checks), a cross-store Admin, a store-scoped
    Manager, and a product -- all real rows in erp_dev via direct
    service-layer calls (setup speed isn't part of what's measured)."""
    from app.modules.auth.permissions import ADMIN, MANAGER
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_store,
        make_user_with_role,
        unique_suffix,
    )

    # pool_pre_ping=True (matching backend/app/db/session.py's own real
    # engine): this session holds a connection across steps F/G, which
    # deliberately stop and restart PostgreSQL -- without pre-ping, the
    # pooled connection from before the restart is dead and every query
    # after G fails with AdminShutdown until the pool happens to recycle
    # it. Found as a real failure in step K during this file's own
    # development.
    engine = create_engine(DB_URL, pool_pre_ping=True)
    db = Session(engine)

    store_a = make_store(db, name=f"Session Store A {unique_suffix()}")
    store_b = make_store(db, name=f"Session Store B {unique_suffix()}")
    admin_username = f"m13_session_admin_{unique_suffix()}"
    manager_username = f"m13_session_manager_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=admin_username)
    make_user_with_role(db, store_a, MANAGER, username=manager_username)
    product = make_product(
        db,
        store_a,
        sku=f"SESSION-{unique_suffix()}",
        current_price=Decimal("12.50"),
        current_cost=Decimal("6.00"),
        current_qty_on_hand=Decimal("500"),
    )
    db.commit()

    state = {
        "engine": engine,
        "db": db,
        "store_a_id": store_a.id,
        "store_b_id": store_b.id,
        "admin_username": admin_username,
        "manager_username": manager_username,
        "product_id": product.id,
    }

    def _login(username: str) -> dict[str, str]:
        resp = httpx.post(
            f"{BASE_URL}/api/v1/auth/login",
            verify=False,
            json={"username": username, "password": DEFAULT_TEST_PASSWORD},
        )
        resp.raise_for_status()
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    state["admin_headers"] = _login(admin_username)
    state["manager_headers"] = _login(manager_username)

    yield state
    db.close()
    engine.dispose()


# --- A: normal authenticated API request through nginx --------------------


def test_A_normal_authenticated_api_request_through_nginx(session_setup) -> None:
    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers=session_setup["manager_headers"],
        params={"store_id": session_setup["store_a_id"]},
    )
    assert resp.status_code == 200
    assert any(p["id"] == session_setup["product_id"] for p in resp.json())


# --- B: store-isolated request through nginx -------------------------------


def test_B_store_isolated_request_through_nginx(session_setup) -> None:
    """The manager is scoped to store_a -- explicitly requesting store_b
    through the real proxy must be refused, not silently redirected or
    silently returning store_a's data instead."""
    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers=session_setup["manager_headers"],
        params={"store_id": session_setup["store_b_id"]},
    )
    assert resp.status_code == 403


# --- C: sale finalization through the production-shaped path --------------


def test_C_sale_finalization_through_the_production_path(session_setup) -> None:
    client_transaction_id = f"session-sale-{uuid.uuid4()}"
    resp = httpx.post(
        f"{BASE_URL}/api/v1/sales",
        verify=False,
        headers=session_setup["manager_headers"],
        json={
            "store_id": session_setup["store_a_id"],
            "client_transaction_id": client_transaction_id,
            "lines": [{"product_id": session_setup["product_id"], "quantity": "3"}],
            "payments": [{"payment_method": "CASH", "amount": "37.50"}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    session_setup["sale_id"] = body["id"]
    session_setup["sale_client_transaction_id"] = client_transaction_id
    session_setup["sale_qty"] = Decimal("3")


# --- D: inventory movement and accounting integrity after the sale --------


def test_D_inventory_and_accounting_integrity_after_the_sale(session_setup) -> None:
    from app.modules.accounting.models import JournalEntry, JournalLine
    from app.modules.inventory.models import InventoryMovement
    from app.modules.products.models import Product

    db = session_setup["db"]
    db.expire_all()

    product = db.get(Product, session_setup["product_id"])
    assert product.current_qty_on_hand == Decimal("500") - session_setup["sale_qty"]

    movement = (
        db.execute(
            select(InventoryMovement)
            .where(
                InventoryMovement.reference_type == "sale",
                InventoryMovement.reference_id == session_setup["sale_id"],
            )
            .order_by(InventoryMovement.id.desc())
        )
        .scalars()
        .first()
    )
    assert movement is not None
    assert movement.quantity_delta == -session_setup["sale_qty"]

    entry = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE",
                JournalEntry.source_id == session_setup["sale_id"],
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None, "no journal entry was posted for this sale"

    lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )
    assert lines, "journal entry has no lines"
    total_debit = sum(line.debit for line in lines)
    total_credit = sum(line.credit for line in lines)
    assert total_debit == total_credit, (
        f"journal entry {entry.id} does not balance: debit={total_debit} credit={total_credit}"
    )


# --- E: idempotent retry of the same sale ----------------------------------


def test_E_idempotent_retry_of_the_same_sale(session_setup) -> None:
    from app.modules.accounting.models import JournalEntry
    from app.modules.sales.models import Sale

    resp = httpx.post(
        f"{BASE_URL}/api/v1/sales",
        verify=False,
        headers=session_setup["manager_headers"],
        json={
            "store_id": session_setup["store_a_id"],
            "client_transaction_id": session_setup["sale_client_transaction_id"],
            "lines": [{"product_id": session_setup["product_id"], "quantity": "3"}],
            "payments": [{"payment_method": "CASH", "amount": "37.50"}],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == session_setup["sale_id"]

    db = session_setup["db"]
    db.expire_all()
    sale_count = db.execute(
        select(func.count())
        .select_from(Sale)
        .where(Sale.client_transaction_id == session_setup["sale_client_transaction_id"])
    ).scalar_one()
    assert sale_count == 1

    entry_count = db.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == session_setup["sale_id"]
        )
    ).scalar_one()
    assert entry_count == 1, "the retry must not have posted a second journal entry"

    # Release the connection back to the pool before F/G deliberately
    # stop/restart PostgreSQL. A SQLAlchemy Session holds one connection
    # checked out continuously for as long as it has an open transaction
    # (SQLAlchemy's default) -- pool_pre_ping only validates a connection
    # at CHECKOUT time, so a connection that is never returned to the
    # pool never gets re-validated, no matter how the engine was
    # constructed. Found as a real failure in step K during this file's
    # own development: pre_ping alone did not fix it until this close()
    # was added too.
    db.close()


# --- F: database connectivity failure ---------------------------------------


def test_F_database_connectivity_failure(session_setup) -> None:
    subprocess.run([*SUDO, "service", "postgresql", "stop"], check=True, capture_output=True)
    try:
        health = httpx.get(f"{BASE_URL}/health/db", verify=False, timeout=10.0)
        assert health.status_code == 503
        assert health.json() == {"status": "error", "database": "unreachable"}

        # A real business endpoint must fail cleanly (a clean 5xx via the
        # catch-all handler), not hang and not leak a stack trace/DSN.
        products = httpx.get(
            f"{BASE_URL}/api/v1/products",
            verify=False,
            headers=session_setup["manager_headers"],
            params={"store_id": session_setup["store_a_id"]},
            timeout=10.0,
        )
        assert products.status_code >= 500
        assert "erp_app" not in products.text and "password" not in products.text.lower()
    finally:
        subprocess.run([*SUDO, "service", "postgresql", "start"], check=True, capture_output=True)


# --- G: recovery after PostgreSQL restart ------------------------------------


def test_G_recovery_after_postgresql_restart(session_setup) -> None:
    wait_for(f"{BASE_URL}/health/db", verify=False, timeout=20.0)
    health = httpx.get(f"{BASE_URL}/health/db", verify=False)
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "reachable"}

    # Confirm normal traffic actually works again, not just the health check.
    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers=session_setup["manager_headers"],
        params={"store_id": session_setup["store_a_id"]},
    )
    assert resp.status_code == 200


# --- H: nginx/backend failure behavior ---------------------------------------


def test_H_nginx_backend_failure_behavior(session_setup) -> None:
    _stop_backend()
    try:
        resp = httpx.get(f"{BASE_URL}/health", verify=False, timeout=10.0)
        assert resp.status_code == 502
    finally:
        _start_backend()
        wait_for(f"{BASE_URL}/health", verify=False)

    # Confirm the session can continue normally after backend recovery.
    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers=session_setup["manager_headers"],
        params={"store_id": session_setup["store_a_id"]},
    )
    assert resp.status_code == 200


# --- I: request correlation propagation --------------------------------------


def test_I_request_correlation_propagation(session_setup) -> None:
    my_id = f"m13-session-{uuid.uuid4()}"
    resp = httpx.get(
        f"{BASE_URL}/api/v1/products",
        verify=False,
        headers={**session_setup["manager_headers"], "X-Request-ID": my_id},
        params={"store_id": session_setup["store_a_id"]},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == my_id

    time.sleep(0.3)
    tail = run([*SUDO, "tail", "-n", "50", _NGINX_ACCESS_LOG]).stdout
    assert my_id in tail


# --- J: rate limiting ---------------------------------------------------------


def test_J_rate_limiting(session_setup) -> None:
    statuses = []
    for _ in range(60):
        resp = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
        statuses.append(resp.status_code)
    assert 429 in statuses, "rapid requests against /health never got rate-limited"

    time.sleep(2.0)
    resp = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert resp.status_code == 200, "rate limiting never recovered after the burst window"


# --- K: audit-log retrieval authorization ------------------------------------


def test_K_audit_log_retrieval_authorization(session_setup) -> None:
    from app.modules.auth.models import Store
    from app.modules.auth.permissions import CASHIER
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_user_with_role,
        unique_suffix,
    )

    # A real sale in store_b (via the Admin, unscoped) so the cross-store
    # isolation check below has an actual store_b row to fail against,
    # not a placeholder.
    store_b = session_setup["db"].get(Store, session_setup["store_b_id"])
    store_b_product = make_product(
        session_setup["db"],
        store_b,
        current_price=Decimal("5.00"),
        current_qty_on_hand=Decimal("100"),
    )
    session_setup["db"].commit()
    store_b_sale = httpx.post(
        f"{BASE_URL}/api/v1/sales",
        verify=False,
        headers=session_setup["admin_headers"],
        json={
            "store_id": session_setup["store_b_id"],
            "client_transaction_id": f"session-store-b-sale-{uuid.uuid4()}",
            "lines": [{"product_id": store_b_product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "5.00"}],
        },
    )
    assert store_b_sale.status_code == 201
    session_setup["store_b_sale_id"] = store_b_sale.json()["id"]

    # Admin (cross-store) sees the sale from step C.
    resp = httpx.get(
        f"{BASE_URL}/api/v1/audit-log",
        verify=False,
        headers=session_setup["admin_headers"],
        params={"entity_type": "sale", "action": "SALE_COMPLETED"},
    )
    assert resp.status_code == 200
    assert any(row["entity_id"] == session_setup["sale_id"] for row in resp.json())

    # A Cashier (no AUDIT_READ) is refused entirely.
    db = session_setup["db"]
    cashier_username = f"m13_session_cashier_{unique_suffix()}"
    from app.modules.auth.models import Store

    store_a = db.get(Store, session_setup["store_a_id"])
    make_user_with_role(db, store_a, CASHIER, username=cashier_username)
    db.commit()
    cashier_login = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": cashier_username, "password": DEFAULT_TEST_PASSWORD},
    )
    cashier_headers = {"Authorization": f"Bearer {cashier_login.json()['access_token']}"}
    resp = httpx.get(f"{BASE_URL}/api/v1/audit-log", verify=False, headers=cashier_headers)
    assert resp.status_code == 403

    # The store_a-scoped Manager never sees store_b's audit rows.
    resp = httpx.get(
        f"{BASE_URL}/api/v1/audit-log",
        verify=False,
        headers=session_setup["manager_headers"],
        params={"entity_type": "sale"},
    )
    assert resp.status_code == 200
    for row in resp.json():
        assert row["entity_id"] != session_setup.get("store_b_sale_id")


# --- L: backup status visibility (real backup -> real monitoring bridge) ----


def test_L_backup_status_visibility(session_setup, tmp_path_factory) -> None:
    from conftest import DEPLOY_DIR

    work_dir = tmp_path_factory.mktemp("session_backup")
    local_dir = work_dir / "local"
    remote_dir = work_dir / "remote"
    local_dir.mkdir()
    remote_dir.mkdir()

    age_keygen = run(["age-keygen", "-o", str(work_dir / "identity.txt")])
    public_key = None
    for line in age_keygen.stderr.splitlines():
        if line.startswith("Public key:"):
            public_key = line.split(":", 1)[1].strip()
    assert public_key

    rclone_conf = work_dir / "rclone.conf"
    rclone_conf.write_text("[session_test]\ntype = local\n")
    status_file = work_dir / "backup_status.prom"

    import os as _os

    result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(local_dir),
            f"session_test:{remote_dir}",
            "14",
        ],
        env={
            **_os.environ,
            "MIGRATIONS_DATABASE_URL": DB_URL,
            "BACKUP_AGE_PUBLIC_KEY": public_key,
            "RCLONE_CONFIG": str(rclone_conf),
            "BACKUP_STATUS_FILE": str(status_file),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert status_file.exists()
    status_content = status_file.read_text()
    assert "erp_backup_last_success 1" in status_content
    session_setup["backup_status_file"] = status_file
    session_setup["backup_status_content"] = status_content


# --- M: monitoring alert generation and recovery -----------------------------


def test_M_monitoring_alert_generation_and_recovery(session_setup) -> None:
    """Reuses the exact ERPDatabaseDown mechanism Phase 6 proved in
    isolation, here as one more step of the SAME continuous session that
    already broke and recovered PostgreSQL once (steps F/G) -- proving
    monitoring would have caught that exact incident, not a separately
    staged one."""
    import tempfile
    from pathlib import Path

    from test_monitoring import (
        ALERTMANAGER_PORT,
        ERP_EXPORTER_PORT,
        PROMETHEUS_PORT,
        _wait_for_target_up,
    )

    work_dir = Path(tempfile.mkdtemp(prefix="erp_m13_session_monitoring_"))
    textfile_dir = work_dir / "textfile_collector"
    textfile_dir.mkdir()

    run(
        [
            *SUDO,
            "-u",
            "postgres",
            "psql",
            "-d",
            "erp_dev",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(BACKEND_DIR.parent / "deploy" / "scripts" / "bootstrap_monitoring_role.sql"),
        ]
    )
    run(
        [
            *SUDO,
            "-u",
            "postgres",
            "psql",
            "-d",
            "erp_dev",
            "-c",
            "ALTER ROLE erp_monitor WITH PASSWORD 'erp_monitor_session_password'",
        ]
    )

    import re

    deploy_dir = BACKEND_DIR.parent / "deploy"
    real_alerts = (deploy_dir / "prometheus" / "alerts.yml").read_text()
    test_alerts = re.sub(r"for: \d+m\b", "for: 3s", real_alerts)
    (work_dir / "alerts.yml").write_text(test_alerts)
    (work_dir / "prometheus.yml").write_text(
        f"""
global:
  scrape_interval: 2s
  evaluation_interval: 2s
rule_files:
  - "{work_dir}/alerts.yml"
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:{ALERTMANAGER_PORT + 1}"]
scrape_configs:
  - job_name: "erp_exporter"
    static_configs:
      - targets: ["127.0.0.1:{ERP_EXPORTER_PORT + 1}"]
"""
    )
    (work_dir / "alertmanager.yml").write_text(
        "route:\n  receiver: default\n  group_wait: 1s\nreceivers:\n  - name: default\n"
    )

    procs = []
    try:
        erp_exp = subprocess.Popen(
            [
                sys.executable,
                str(deploy_dir / "prometheus" / "erp_exporter.py"),
                "--port",
                str(ERP_EXPORTER_PORT + 1),
                "--app-base-url",
                "http://127.0.0.1:8000",
                "--nginx-log",
                _NGINX_ACCESS_LOG,
                "--db-url",
                "postgresql://erp_monitor:erp_monitor_session_password@localhost:5432/erp_dev",
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
                f"--web.listen-address=127.0.0.1:{ALERTMANAGER_PORT + 1}",
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
                f"--web.listen-address=127.0.0.1:{PROMETHEUS_PORT + 1}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(prom)

        prom_url = f"http://127.0.0.1:{PROMETHEUS_PORT + 1}"
        _wait_for_target_up(prom_url, "erp_exporter", timeout=15.0)

        subprocess.run([*SUDO, "service", "postgresql", "stop"], check=True, capture_output=True)
        try:
            deadline = time.monotonic() + 40
            fired = False
            while time.monotonic() < deadline:
                resp = httpx.get(f"{prom_url}/api/v1/alerts", timeout=3.0)
                states = {
                    a["labels"]["alertname"]: a["state"] for a in resp.json()["data"]["alerts"]
                }
                if states.get("ERPDatabaseDown") == "firing":
                    fired = True
                    break
                time.sleep(1.0)
            assert fired, "ERPDatabaseDown never fired during this session's DB outage"
        finally:
            subprocess.run(
                [*SUDO, "service", "postgresql", "start"], check=True, capture_output=True
            )

        deadline = time.monotonic() + 40
        resolved = False
        while time.monotonic() < deadline:
            resp = httpx.get(f"{prom_url}/api/v1/alerts", timeout=3.0)
            names = {a["labels"]["alertname"] for a in resp.json()["data"]["alerts"]}
            if "ERPDatabaseDown" not in names:
                resolved = True
                break
            time.sleep(1.0)
        assert resolved, "ERPDatabaseDown never cleared after PostgreSQL recovered"
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        wait_for(f"{BASE_URL}/health/db", verify=False, timeout=20.0)


# --- N: migration health endpoint ---------------------------------------------


def test_N_migration_health_endpoint(session_setup) -> None:
    resp = httpx.get(f"{BASE_URL}/health/migration", verify=False)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "migration": "current"}


# --- O: production configuration fail-closed behavior (real process boot) --


def test_O_production_configuration_fail_closed_behavior() -> None:
    """Not a Settings()-construction unit test (already exhaustive in
    backend/tests/test_config_safety.py) -- this boots a REAL uvicorn
    process with a known-insecure production config and proves the
    PROCESS itself refuses to start, not just that a Python object
    raises."""
    import os

    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "SECRET_KEY": "dev-only-insecure-secret-key-change-me",
        "DATABASE_URL": "postgresql+psycopg://erp_app:erp_app_password@localhost:5432/erp_dev",
    }
    proc = subprocess.run(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8099"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0, "a production boot with a known-insecure SECRET_KEY must fail"
    assert "SECRET_KEY" in (proc.stdout + proc.stderr)
