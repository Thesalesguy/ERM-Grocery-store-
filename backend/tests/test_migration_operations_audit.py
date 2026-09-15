"""M12 Phase 13: migration operations audit.

tests/test_migrations.py already proves the full M0->M12 upgrade/
downgrade/upgrade chain works from scratch, with correct table counts
and RBAC seed data at every step. What that file does NOT prove: that
GET /health/migration (M12 Phase 11) actually detects a REAL schema
mismatch against a real database one revision behind, not merely a
mocked comparison (see tests/test_health_and_observability.py, which
only exercises the comparison logic with `_code_migration_head`
monkeypatched). This file closes that gap using the same dedicated
`erp_test` database and `migrations_db` fixture test_migrations.py
already established for real up/down migration cycles.

App-level "does the service layer actually work against a freshly
migrated-to-head schema" is already proven end-to-end by
tests/test_backup_restore.py's realistic seeded dataset running against
a freshly-`alembic upgrade head`-ed database -- not duplicated here.
"""

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app.api.v1.endpoints import health as health_endpoint
from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_MIGRATIONS_DATABASE_URL = os.environ.get(
    "TEST_MIGRATIONS_DATABASE_URL",
    "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_test",
)
M11_HEAD_REVISION = "32e51bcda102"  # mirrors tests/test_migrations.py's own constant


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture
def migrations_db(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    """Same fixture as tests/test_migrations.py -- redefined locally
    rather than imported cross-module, since pytest fixtures are scoped
    to the module (or conftest.py) that defines them."""
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", TEST_MIGRATIONS_DATABASE_URL)
    get_settings.cache_clear()
    yield TEST_MIGRATIONS_DATABASE_URL
    get_settings.cache_clear()


def test_health_migration_detects_a_real_schema_mismatch_one_revision_behind(
    migrations_db: str,
) -> None:
    """Migrates a real, dedicated database to one revision short of head,
    then calls the actual GET /health/migration route function (not a
    mocked stand-in) against a Session bound to that database -- proving
    the comparison genuinely reads the DB's applied alembic_version, not
    just a hardcoded expectation."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M11_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with Session(bind=engine) as session:
            response = health_endpoint.health_migration(db=session)
        assert response.status_code == 503
        body = json.loads(response.body)
        assert body == {"status": "error", "reason": "schema_mismatch"}

        command.upgrade(cfg, "head")
        with Session(bind=engine) as session:
            response = health_endpoint.health_migration(db=session)
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {"status": "ok", "migration": "current"}
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")
