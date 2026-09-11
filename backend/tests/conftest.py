"""Shared pytest fixtures.

Tests run against a real PostgreSQL database (DATABASE_URL from the
environment/.env), matching the blueprint's rule that NUMERIC/transaction
behavior must be verified against Postgres, not an in-memory substitute.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.rate_limit import login_rate_limiter, refresh_rate_limiter
from app.db.session import engine
from app.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Generator[None, None, None]:
    """The login/refresh rate limiters (app.core.rate_limit) are
    module-level singletons by design (see that module's docstring) —
    without resetting them, every test in the whole suite would share one
    counter per client IP, and the 11th login attempt anywhere in a full
    test run would start returning 429 to whatever test happened to make
    it, regardless of that test's own intent. Runs before AND after every
    test so a rate-limit test's own tripped state never leaks either."""
    login_rate_limiter.clear()
    refresh_rate_limiter.clear()
    yield
    login_rate_limiter.clear()
    refresh_rate_limiter.clear()


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """A session connected as the application's runtime role (erp_app —
    see app/core/config.py), wrapped in an outer transaction that is
    always rolled back at the end of the test — regardless of how many
    times the test itself calls db.commit()/db.rollback().

    `join_transaction_mode="create_savepoint"` (SQLAlchemy 2.0) makes an
    inner commit() release a SAVEPOINT instead of ending the outer
    transaction, and immediately opens a new one — the standard recipe for
    giving each test a fully isolated, auto-cleaned-up view of a real
    database. This is what makes tests safely re-runnable: nothing a test
    writes (products, sales, movements, ...) persists past that test.

    Note: tests proving real cross-connection concurrency (test_concurrency.py)
    deliberately do NOT use this fixture — see that file's module docstring.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    """A TestClient whose requests run against the SAME session/transaction
    as the `db` fixture (via a get_db dependency override), so a test can
    set up fixtures with `db` (factories.py) and then exercise them
    through real HTTP requests — auth, RBAC, request validation, and all —
    with everything rolled back together at the end. Without this
    override, TestClient requests would open their own independent
    SessionLocal() connection and never see this test's uncommitted setup
    (the same cross-connection-visibility issue test_concurrency.py's
    module docstring explains).
    """

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
