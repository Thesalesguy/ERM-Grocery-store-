"""M13 Phase 10-11: the real deploy/scripts/deploy.sh procedure, run as
an actual subprocess (not inspected as text) against a real backend
process and real databases.

Two scenarios: the full seven-step procedure succeeding end-to-end
against erp_dev (already at head, so the migration step is a real,
harmless no-op -- exactly what a routine deploy with no pending schema
change looks like), and a migration failure -- injected the same way
backend/tests/test_disaster_recovery_scenarios.py already proved
Alembic itself rolls back cleanly -- proving deploy.sh's OWN control
flow reacts correctly: it aborts before step 4 and never runs
APP_RESTART_CMD, so a failed migration can never result in the
(unsafe) combination of a new application version against a
half-attempted schema change.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import BACKEND_DIR, DEPLOY_DIR, REPO_ROOT, wait_for
from sqlalchemy import create_engine, inspect

from alembic import command

DEPLOY_SCRIPT = DEPLOY_DIR / "scripts" / "deploy.sh"
PYTHON_BIN = str(BACKEND_DIR / ".venv" / "bin" / "python")

TEST_MIGRATIONS_DATABASE_URL = "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_test"
DEV_MIGRATIONS_DATABASE_URL = "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev"
DEV_DATABASE_URL = "postgresql+psycopg://erp_app:erp_app_password@localhost:5432/erp_dev"

sys.path.insert(0, str(BACKEND_DIR))


def _alembic_config() -> Config:
    # alembic.ini's own script_location="alembic" is relative to the
    # process's cwd, not to the ini file -- other backend/tests/ files
    # get away with the bare Config(...) call because pytest's cwd for
    # them is backend/ itself; this file's pytest invocation runs from
    # the repo root (deploy/tests/), so the location is set explicitly.
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _current_head(cfg: Config) -> str:
    return ScriptDirectory.from_config(cfg).get_current_head()


@pytest.fixture
def running_backend(tmp_path: Path):
    """A real uvicorn process bound to erp_dev, for deploy.sh's
    pre-flight/smoke-test HTTP calls -- independent of whichever
    database the migration step itself targets in a given test."""
    log_path = tmp_path / "uvicorn.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        wait_for("http://127.0.0.1:8000/health", verify=False)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def test_full_deploy_procedure_succeeds_end_to_end(running_backend, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    sentinel = tmp_path / "app_restarted.sentinel"

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "BACKUP_DIR": str(backup_dir),
            "APP_HEALTH_BASE_URL": "http://127.0.0.1:8000",
            "PYTHON_BIN": PYTHON_BIN,
            "APP_RESTART_CMD": f"touch {sentinel}",
            "DATABASE_URL": DEV_DATABASE_URL,
            "MIGRATIONS_DATABASE_URL": DEV_MIGRATIONS_DATABASE_URL,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Deployment complete" in result.stdout
    assert sentinel.exists(), "APP_RESTART_CMD (step 4) should have run on a successful deploy"
    assert any(backup_dir.glob("*.dump")), "step 1 should have produced a real backup file"


def test_deploy_aborts_before_restarting_the_app_when_migration_fails(tmp_path: Path) -> None:
    """The load-bearing test: a migration failure must stop the
    procedure before step 4 ever runs -- never restart the application
    against a schema change that didn't complete."""
    cfg = _alembic_config()
    real_head = _current_head(cfg)

    broken_revision_id = "m13deploytest"
    broken_migration_source = f'''"""m13 deploy-procedure test: a broken migration

Revision ID: {broken_revision_id}
Revises: {real_head}
Create Date: 2026-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "{broken_revision_id}"
down_revision = "{real_head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "m13_deploy_test_table",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    raise RuntimeError("simulated migration failure for deploy.sh's own test")


def downgrade() -> None:
    op.drop_table("m13_deploy_test_table")
'''

    versions_dir = BACKEND_DIR / "alembic" / "versions"
    broken_migration_path = versions_dir / f"{broken_revision_id}_deploy_test.py"

    # Bring erp_test to real head first (same pattern as
    # test_disaster_recovery_scenarios.py) so the broken migration is
    # the only pending one, then hand deploy.sh the broken database.
    os.environ["MIGRATIONS_DATABASE_URL"] = TEST_MIGRATIONS_DATABASE_URL
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, real_head)

    running_backend_proc = None
    log_file = None
    try:
        broken_migration_path.write_text(broken_migration_source)

        log_path = tmp_path / "uvicorn.log"
        log_file = open(log_path, "w")
        running_backend_proc = subprocess.Popen(
            ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"],
            cwd=str(BACKEND_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "DATABASE_URL": DEV_DATABASE_URL},
        )
        wait_for("http://127.0.0.1:8001/health", verify=False)

        sentinel = tmp_path / "app_restarted.sentinel"
        backup_dir = tmp_path / "backups"

        result = subprocess.run(
            ["bash", str(DEPLOY_SCRIPT)],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "BACKUP_DIR": str(backup_dir),
                "APP_HEALTH_BASE_URL": "http://127.0.0.1:8001",
                "PYTHON_BIN": PYTHON_BIN,
                "APP_RESTART_CMD": f"touch {sentinel}",
                "DATABASE_URL": DEV_DATABASE_URL,
                "MIGRATIONS_DATABASE_URL": TEST_MIGRATIONS_DATABASE_URL,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "DEPLOYMENT FAILED at step: migration" in combined
        assert "Rollback decision" in combined
        assert not sentinel.exists(), (
            "step 4 (APP_RESTART_CMD) must never run after a migration failure"
        )

        # Prove the failure genuinely left no partial schema change,
        # same guarantee backend/tests/test_disaster_recovery_scenarios.py
        # already established for Alembic itself -- re-confirmed here at
        # the level deploy.sh actually observes it.
        engine = create_engine(TEST_MIGRATIONS_DATABASE_URL)
        try:
            with engine.connect() as conn:
                version = conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                table_names = inspect(engine).get_table_names()
        finally:
            engine.dispose()
        assert version == real_head
        assert "m13_deploy_test_table" not in table_names
    finally:
        if running_backend_proc is not None:
            running_backend_proc.terminate()
            try:
                running_backend_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                running_backend_proc.kill()
        if log_file is not None:
            log_file.close()
        broken_migration_path.unlink(missing_ok=True)
        command.downgrade(cfg, "base")
        os.environ.pop("MIGRATIONS_DATABASE_URL", None)
        get_settings.cache_clear()
