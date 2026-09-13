"""M11 Phase 13: failure injection for the reports API. Every report
function in app.modules.reports.service is read-only (module docstring:
"no function in this module ever writes to a transactional or GL
table"), so "no partial mutation" is true by construction rather than
something a rollback has to undo -- what this file actually has to
prove is: authorization and validation fail BEFORE touching the
database at all (fail fast, not fail-after-partial-read), a forced
failure mid-aggregation never leaks internals (SQL text, stack traces,
file paths) to the client, and one request's failure never poisons the
next request's session.
"""

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationAppError
from app.modules.auth.permissions import ADMIN, MANAGER
from app.modules.auth.service import CurrentUser
from app.modules.reports import service as reports_service
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role, unique_suffix
from tests.helpers import auth_headers
from tests.test_reports_performance import _count_queries


def test_authorization_denial_never_touches_the_database(db: Session) -> None:
    """A store-scoped caller requesting a foreign store must be rejected
    in resolve_authorized_store_ids -- pure Python, zero SQL -- never by
    first querying and then discovering the mismatch.

    Builds a plain CurrentUser (the dataclass require_permission actually
    hands every route -- see app.modules.auth.service.CurrentUser)
    rather than passing the raw ORM User row: the real call site never
    sees a session-bound, lazily-refreshing object here, so the test
    shouldn't either."""
    store_a = make_store(db)
    store_b = make_store(db)
    manager = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    current_user = CurrentUser(
        id=manager.id,
        username=manager.username,
        full_name=manager.full_name,
        store_id=manager.store_id,
        permissions=frozenset(),
    )
    store_b_id = store_b.id  # read before the count -- see the note above about expiration

    with _count_queries(db) as statements:
        with pytest.raises(ForbiddenError):
            reports_service.resolve_authorized_store_ids(current_user, [store_b_id])
    assert statements == []


def test_invalid_date_range_rejected_before_any_query(db: Session) -> None:
    """date_from > date_to must be rejected before the function issues
    its first SELECT -- validated input, not a query that runs and
    produces a nonsensical (or worse, misleadingly empty) result."""
    store = make_store(db)
    db.commit()
    store_id = store.id  # captured before entering the count -- see below

    with _count_queries(db) as statements:
        with pytest.raises(ValidationAppError):
            reports_service.sales_summary(
                db,
                store_ids=[store_id],
                date_from=date(2024, 6, 1),
                date_to=date(2024, 1, 1),
            )
    # SQLAlchemy expires ORM attributes on commit() by default -- reading
    # store.id again inside the `with` block would itself issue a
    # refresh SELECT having nothing to do with the report, so it's read
    # once beforehand instead. What's actually asserted is unchanged:
    # _validate_date_range must reject this before sales_summary's own
    # first query.
    assert statements == []


def test_no_report_service_function_ever_commits() -> None:
    """Static proof that the read-only claim in the module docstring is
    actually true: grep the whole reports service module for a call to
    .commit() -- there must be none. A read-only analytics layer that
    accidentally committed something (e.g. from a copy-pasted snippet
    that flushed an ORM-tracked change) would be exactly the kind of
    silent transactional-data mutation this milestone forbids."""
    source = Path("app/modules/reports/service.py").read_text()
    assert not re.search(r"\.commit\s*\(", source), (
        "app/modules/reports/service.py calls .commit() somewhere -- a read-only "
        "reporting module must never mutate transactional data"
    )


def _report_headers(client: TestClient, db: Session) -> dict:
    make_store(db)
    username = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)
    db.commit()
    return auth_headers(client, username, DEFAULT_TEST_PASSWORD)


def _no_raise_client(client: TestClient) -> TestClient:
    """The `client` fixture's TestClient defaults to raise_server_exceptions=True
    (pytest-friendly: an uncaught error in a route fails the test loudly
    instead of silently degrading to a 500). That's the wrong tool for
    THESE two tests, whose whole point is to observe what a real client
    receives when app.core.exceptions' global handler catches an
    unexpected exception -- so a second TestClient, wrapping the exact
    same `app` (and therefore the same dependency override the `client`
    fixture already registered), is used here instead."""
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_forced_failure_during_aggregation_returns_a_safe_generic_error(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates an unexpected failure deep inside a report (e.g. a
    transient DB error) and proves the client gets the same sanitized
    500 body every unhandled exception gets (app.core.exceptions'
    global handler) -- never a stack trace, SQL text, or file path."""
    headers = _report_headers(client, db)
    raw_client = _no_raise_client(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure: connection reset by peer at db.example.internal")

    monkeypatch.setattr(reports_service, "sales_summary", _boom)

    response = raw_client.get("/api/v1/reports/sales/summary", headers=headers)
    assert response.status_code == 500
    body = response.json()
    assert body == {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}}
    # the simulated failure message must never reach the client
    assert "db.example.internal" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_report_failure_does_not_poison_the_next_request(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each request gets its own session (app.db.session.get_db); a
    failure in one report call must never affect a completely unrelated
    later request in the same test process."""
    headers = _report_headers(client, db)
    raw_client = _no_raise_client(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(reports_service, "sales_summary", _boom)
    failing = raw_client.get("/api/v1/reports/sales/summary", headers=headers)
    assert failing.status_code == 500

    monkeypatch.undo()
    healthy = client.get("/api/v1/reports/sales/summary", headers=headers)
    assert healthy.status_code == 200


def test_stock_count_variance_for_a_nonexistent_id_is_a_safe_empty_result(
    client: TestClient, db: Session
) -> None:
    """A drill-down endpoint given a made-up id must degrade to an empty,
    well-formed result (docs/M11_DESIGN.md Section 9's drill-down
    contract), never a 500 or a leaked internal error."""
    headers = _report_headers(client, db)
    response = client.get(
        "/api/v1/reports/inventory/stock-counts/999999999/variance", headers=headers
    )
    assert response.status_code == 200
    assert response.json() == []
