"""M12 Phase 14: disaster/recovery scenarios.

Most of the nine scenarios this phase is meant to cover are already
proven by tests written in earlier phases -- the evidence belongs next
to where it was actually produced, not duplicated here (see
docs/M12_HARDENING_AUDIT.md for the full scenario-by-scenario writeup
with pointers to each proof):

  A. Full restore from backup                 -> test_backup_restore.py
  B. Accounting/inventory integrity survives
     a backup/restore round trip              -> test_backup_restore.py
     (Phases 4/5)
  C. A crashed process mid-transaction never
     leaves partial state                     -> test_m8_failure_injection.py,
     test_ap_failure_injection.py,
     test_sales_returns_failure_injection.py,
     test_payroll_hardening.py,
     test_sale_finalization_failure_injection.py (Phase 12)
  D. A compromised/stolen credential can be
     revoked immediately, mid-session         -> test_user_deactivation.py,
     test_auth_hardening.py (Phase 6)
  E. A concurrent replay/race on a single-use
     credential is treated as compromise, not
     silently allowed                         -> test_auth_hardening.py
     (Phase 6)
  F. A stale app instance against a
     mismatched schema is observable, not a
     silent 500                               -> test_health_and_observability.py,
     test_migration_operations_audit.py
     (Phases 11/13)
  G. The runtime role cannot tamper with
     financial/audit/migration ledgers even
     with a compromised app-level credential   -> test_constraints.py,
     test_backup_restore.py, test_migrations.py
     (Phase 8)
  H. A network retry/double-submit during a
     financial mutation never double-books    -> test_idempotency.py and
     per-module idempotency tests (pre-M12),
     test_sale_finalization_failure_injection.py
  I. A migration interrupted before completion
     leaves the database in its exact
     pre-migration state, not a half-applied
     schema                                    -> THIS FILE (new for M12)

Scenario I was the one genuinely untested gap: every migration this
codebase runs relies on Alembic's "Will assume transactional DDL"
guarantee (visible in every migration's log output), but nothing had
ever actually forced a migration to fail partway through and proven
Postgres really does roll back every DDL statement that ran before the
failure, not just the one that raised.
"""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_MIGRATIONS_DATABASE_URL = os.environ.get(
    "TEST_MIGRATIONS_DATABASE_URL",
    "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_test",
)
M11_HEAD_REVISION = "32e51bcda102"  # mirrors tests/test_migrations.py's own constant

_BROKEN_REVISION_ID = "z9z9z9interrupted"
_BROKEN_MIGRATION_SOURCE = f'''"""m12 disaster-recovery test: an interrupted migration

Revision ID: {_BROKEN_REVISION_ID}
Revises: {M11_HEAD_REVISION}
Create Date: 2026-01-01 00:00:00.000000

Deliberately creates one real table, then raises -- simulating a crash
or a bad statement partway through a multi-step migration. Written to
alembic/versions/ only for the duration of one test and removed
immediately after (see the test's try/finally) -- never committed to
the repository.
"""

from alembic import op
import sqlalchemy as sa

revision = "{_BROKEN_REVISION_ID}"
down_revision = "{M11_HEAD_REVISION}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "m12_disaster_recovery_test_table",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    raise RuntimeError("simulated crash partway through this migration")


def downgrade() -> None:
    op.drop_table("m12_disaster_recovery_test_table")
'''


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture
def migrations_db(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", TEST_MIGRATIONS_DATABASE_URL)
    get_settings.cache_clear()
    yield TEST_MIGRATIONS_DATABASE_URL
    get_settings.cache_clear()


def test_migration_interrupted_before_completion_leaves_no_partial_schema_change(
    migrations_db: str,
) -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M11_HEAD_REVISION)

    versions_dir = BACKEND_DIR / "alembic" / "versions"
    broken_migration_path = versions_dir / f"{_BROKEN_REVISION_ID}_disaster_recovery_test.py"
    broken_migration_path.write_text(_BROKEN_MIGRATION_SOURCE)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            command.upgrade(cfg, _BROKEN_REVISION_ID)

        engine = create_engine(migrations_db)
        try:
            with engine.connect() as conn:
                version = conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                # The table the failed migration tried to create must not
                # exist -- Postgres rolled back the whole transaction,
                # CREATE TABLE included, not just the statement that
                # raised.
                table_names = inspect(engine).get_table_names()
        finally:
            engine.dispose()

        assert version == M11_HEAD_REVISION  # never advanced past the failed migration
        assert "m12_disaster_recovery_test_table" not in table_names
    finally:
        broken_migration_path.unlink(missing_ok=True)
        command.downgrade(cfg, "base")
