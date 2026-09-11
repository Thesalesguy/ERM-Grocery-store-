"""Shared pytest fixtures.

Tests run against a real PostgreSQL database (DATABASE_URL from the
environment/.env), matching the blueprint's rule that NUMERIC/transaction
behavior must be verified against Postgres, not an in-memory substitute.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import engine
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


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
