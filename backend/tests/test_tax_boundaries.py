"""M2 hardening audit Section 5: tax-effective-date behavior at
finalize_sale time — currently active, future, expired, inactive, absent,
and exactly on a boundary date. Also documents/tests the timezone policy
decision (UTC calendar date, not server-local time — see
app.modules.sales.service._resolve_tax's docstring for the full
rationale)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import CASHIER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_tax_rate,
    make_user_with_role,
)
from tests.helpers import auth_headers


def _today_utc():
    return datetime.now(UTC).date()


def _setup(db: Session, store):
    username = f"tax_cashier_{uuid.uuid4().hex[:8]}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    return username


def _checkout(client, headers, store, product, amount):
    return client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": f"txn-{uuid.uuid4().hex}",
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": amount}],
        },
    )


def test_currently_active_tax_rate_applies(client: TestClient, db: Session) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(
        db, rate_percent=Decimal("10.000"), effective_from=_today_utc() - timedelta(days=30)
    )
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "11.00")
    assert response.status_code == 201
    assert response.json()["tax_total"] == "1.00"


def test_future_dated_tax_rate_is_rejected(client: TestClient, db: Session) -> None:
    """A tax rate that hasn't taken effect yet must not be silently
    applied (nor silently skipped) — the product's tax configuration is
    treated as an error to be fixed, not guessed around."""
    store = make_store(db)
    tax_rate = make_tax_rate(
        db, rate_percent=Decimal("10.000"), effective_from=_today_utc() + timedelta(days=30)
    )
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.00")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_TAX_CONFIGURATION_INVALID"


def test_expired_tax_rate_is_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(
        db,
        rate_percent=Decimal("10.000"),
        effective_from=_today_utc() - timedelta(days=60),
        effective_to=_today_utc() - timedelta(days=1),
    )
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.00")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_TAX_CONFIGURATION_INVALID"


def test_inactive_tax_rate_is_rejected_even_within_date_range(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(
        db,
        rate_percent=Decimal("10.000"),
        effective_from=_today_utc() - timedelta(days=30),
        is_active=False,
    )
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.00")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_TAX_CONFIGURATION_INVALID"


def test_no_tax_rate_configured_means_zero_tax(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.00")
    assert response.status_code == 201
    assert response.json()["tax_total"] == "0.00"


def test_tax_rate_effective_exactly_today_applies(client: TestClient, db: Session) -> None:
    """effective_from == today (the boundary itself) must count as
    already effective, not "not yet"."""
    store = make_store(db)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("5.000"), effective_from=_today_utc())
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.50")
    assert response.status_code == 201
    assert response.json()["tax_total"] == "0.50"


def test_tax_rate_expiring_exactly_today_still_applies_through_today(
    client: TestClient, db: Session
) -> None:
    """effective_to == today must still count as effective for the whole
    of today (the rate expires AFTER today, not AT the start of it) —
    only effective_to < today is expired."""
    store = make_store(db)
    tax_rate = make_tax_rate(
        db,
        rate_percent=Decimal("5.000"),
        effective_from=_today_utc() - timedelta(days=10),
        effective_to=_today_utc(),
    )
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    username = _setup(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = _checkout(client, headers, store, product, "10.50")
    assert response.status_code == 201
    assert response.json()["tax_total"] == "0.50"
