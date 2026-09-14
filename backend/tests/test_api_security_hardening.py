"""M12 Phase 7: API security hardening.

Idempotency (client_transaction_id and equivalents) is already heavily
covered across the existing suite (tests/test_idempotency.py and
per-module idempotency/concurrency files) -- not duplicated here.

This file covers what M12 actually added or changed in this phase:
- the application-layer max-request-body-size middleware (the
  M2-documented gap, app.core.http_hardening.MaxBodySizeMiddleware)
- the security-headers middleware
- the new upper bound on every `limit` list-endpoint query parameter
- that oversized/out-of-range pagination input fails cleanly (422), not
  with a 500 from an unbounded query
"""

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.auth.permissions import CASHIER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role
from tests.helpers import auth_headers


def test_oversized_request_body_is_rejected_with_413(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="oversized_body")
    db.commit()
    headers = auth_headers(client, "oversized_body", DEFAULT_TEST_PASSWORD)

    settings = get_settings()
    oversized_payload = "x" * (settings.MAX_REQUEST_BODY_BYTES + 1)
    response = client.post(
        "/api/v1/products",
        headers={**headers, "Content-Type": "application/json"},
        content=json.dumps({"padding": oversized_payload}).encode("utf-8"),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"


def test_request_body_under_the_limit_is_not_rejected_by_size(
    client: TestClient, db: Session
) -> None:
    """A request comfortably under the cap must reach normal validation
    (a 422 for a still-invalid product payload here, never a 413) --
    proves the middleware is actually keyed on size, not rejecting
    everything."""
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="normal_body")
    db.commit()
    headers = auth_headers(client, "normal_body", DEFAULT_TEST_PASSWORD)

    response = client.post("/api/v1/products", headers=headers, json={"sku": "TOO-SHORT"})
    assert response.status_code != 413


def test_security_headers_present_on_every_response(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_list_endpoint_limit_beyond_the_cap_is_rejected_with_422(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="limit_cap")
    db.commit()
    headers = auth_headers(client, "limit_cap", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products", headers=headers, params={"limit": 999_999_999})
    assert response.status_code == 422


def test_list_endpoint_negative_limit_is_rejected_with_422(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="limit_negative")
    db.commit()
    headers = auth_headers(client, "limit_negative", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products", headers=headers, params={"limit": -1})
    assert response.status_code == 422


def test_list_endpoint_negative_offset_is_rejected_with_422(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="offset_negative")
    db.commit()
    headers = auth_headers(client, "offset_negative", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products", headers=headers, params={"offset": -1})
    assert response.status_code == 422


def test_list_endpoint_limit_at_the_cap_is_accepted(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="limit_at_cap")
    db.commit()
    headers = auth_headers(client, "limit_at_cap", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products", headers=headers, params={"limit": 500})
    assert response.status_code == 200
