"""M12 Phase 11: health checks and observability.

GET /health and GET /health/db already existed; this file adds coverage
for the new GET /health/migration (docs/M12_DESIGN.md Section 6) and the
new per-request correlation ID (Section 7).
"""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.api.v1.endpoints import health as health_endpoint
from app.core.correlation import RequestIdLogFilter, request_id_var


def test_health_migration_reports_current_when_db_matches_code_head(
    client: TestClient,
) -> None:
    response = client.get("/health/migration")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "migration": "current"}


def test_health_migration_reports_schema_mismatch_without_leaking_internals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a stale app instance against a newer/older schema (M12
    Phase 14 scenario I) by making this deployment's own reported head
    disagree with what's actually in the database -- erp_app only has
    SELECT on alembic_version (M12 Phase 8/11), so the realistic way to
    produce a mismatch is the code-head side, not mutating the table.
    The response must be a clean 503 that never includes the actual
    mismatched version strings, a stack trace, or a connection string."""
    monkeypatch.setattr(health_endpoint, "_code_migration_head", lambda: "not_the_real_code_head")

    response = client.get("/health/migration")
    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "error", "reason": "schema_mismatch"}
    assert "not_the_real_code_head" not in response.text
    assert "postgresql" not in response.text.lower()
    assert "traceback" not in response.text.lower()


def test_erp_app_can_read_but_not_write_alembic_version(db: Session) -> None:
    """M12 Phase 11 grants erp_app SELECT on alembic_version (for this
    health check) but Phase 8's write-immutability must still hold --
    the running application can observe the schema version but never
    change it."""
    db.execute(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("UPDATE alembic_version SET version_num = 'tampered'"))
    db.rollback()


def test_health_endpoints_never_leak_a_stack_trace_or_connection_string(
    client: TestClient,
) -> None:
    for path in ("/health", "/health/db", "/health/migration"):
        response = client.get(path)
        assert "Traceback" not in response.text
        assert "postgresql://" not in response.text
        assert "postgresql+psycopg://" not in response.text


def test_response_includes_a_correlation_id_header(client: TestClient) -> None:
    response = client.get("/health")
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) > 0


def test_two_separate_requests_get_two_different_correlation_ids(client: TestClient) -> None:
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert first != second


def test_an_inbound_request_id_header_is_echoed_back(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "caller-supplied-id-123"})
    assert response.headers["x-request-id"] == "caller-supplied-id-123"


def test_an_oversized_inbound_request_id_is_replaced_not_echoed(client: TestClient) -> None:
    """A client can't use this header to bloat every log line for the
    request -- an absurdly long inbound value is discarded in favor of a
    freshly generated one, not blindly trusted."""
    oversized = "x" * 5000
    response = client.get("/health", headers={"X-Request-ID": oversized})
    assert response.headers["x-request-id"] != oversized
    assert len(response.headers["x-request-id"]) < 100


def test_log_records_emitted_during_a_request_carry_its_correlation_id() -> None:
    """Unit-level proof that the contextvar the middleware sets is what
    the logging filter actually reads -- the same mechanism a service
    function's own log line would pick up mid-request."""
    token = request_id_var.set("unit-test-request-id")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        RequestIdLogFilter().filter(record)
        assert record.request_id == "unit-test-request-id"
    finally:
        request_id_var.reset(token)


def test_log_records_outside_a_request_have_no_correlation_id() -> None:
    assert request_id_var.get() is None
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    RequestIdLogFilter().filter(record)
    assert record.request_id is None
