"""Liveness and readiness checks.

GET /health            -> process is up (no dependencies checked)
GET /health/db         -> the API can actually reach PostgreSQL
GET /health/migration  -> the DB's applied schema matches this deployed
                          code's own migration head
"""

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent


def _code_migration_head() -> str | None:
    """Reads the migration head from the local `alembic/versions/`
    history at import time -- a filesystem read of the code actually
    deployed in this process, not a network call to anywhere."""
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    return script.get_current_head()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "unreachable"},
        )
    return JSONResponse(content={"status": "ok", "database": "reachable"})


@router.get("/health/migration")
def health_migration(db: Session = Depends(get_db)) -> JSONResponse:
    """Catches a stale app instance running against a newer/older schema
    (M12 Phase 14 disaster scenario I) as an observable 503 instead of a
    confusing 500 on the first query that touches a changed column.

    Never includes a stack trace, connection string, or credential in
    the response body on any failure path -- verified by
    tests/test_health_and_observability.py forcing each one."""
    try:
        code_head = _code_migration_head()
    except Exception:
        logger.exception("Could not read this deployment's own migration head")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "reason": "code_head_unreadable"},
        )

    try:
        db_version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except SQLAlchemyError:
        logger.exception("Could not read the database's applied migration version")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "reason": "db_version_unreadable"},
        )

    if db_version != code_head:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "reason": "schema_mismatch"},
        )
    return JSONResponse(content={"status": "ok", "migration": "current"})
