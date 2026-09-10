"""Shared pytest fixtures.

Tests run against a real PostgreSQL database (DATABASE_URL from the
environment/.env), matching the blueprint's rule that NUMERIC/transaction
behavior must be verified against Postgres, not an in-memory substitute.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
