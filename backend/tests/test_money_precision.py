"""M2 hardening audit Section 4: every financial calculation in
finalize_sale must use Decimal semantics throughout, with correct
rounding at exact currency-relevant boundaries — never a float, and never
an off-by-a-cent from naive rounding. These exercise real HTTP requests
end to end (server-computed response, not just the internal function),
so a regression in JSON (de)serialization of Decimal would also be
caught here, not just in a unit test of the arithmetic alone."""

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


def _checkout(client, headers, store, product, quantity, amount, discount="0", txn=None):
    import uuid

    return client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": txn or f"txn-{uuid.uuid4().hex}",
            "lines": [
                {"product_id": product.id, "quantity": quantity, "discount_amount": discount}
            ],
            "payments": [{"payment_method": "CASH", "amount": amount}],
        },
    )


def _setup_cashier(db: Session, store) -> None:
    import uuid

    make_user_with_role(db, store, CASHIER, username=f"money_cashier_{uuid.uuid4().hex[:8]}")
    db.commit()


def test_penny_price_exact_payment(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("0.01"), current_qty_on_hand=Decimal("100")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "0.01")
    assert response.status_code == 201
    assert response.json()["grand_total"] == "0.01"


def _last_username(db: Session) -> str:
    from sqlalchemy import select

    from app.modules.auth.models import User

    return db.execute(select(User.username).order_by(User.id.desc())).scalars().first()


def test_ten_cent_price_with_quantity(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("0.10"), current_qty_on_hand=Decimal("100")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "3", "0.30")
    assert response.status_code == 201
    assert response.json()["grand_total"] == "0.30"


def test_ninety_nine_cent_price(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("0.99"), current_qty_on_hand=Decimal("100")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "0.99")
    assert response.status_code == 201
    assert response.json()["grand_total"] == "0.99"


def test_large_amount_near_column_precision_limit(client: TestClient, db: Session) -> None:
    """Numeric(12, 2) allows up to 9,999,999,999.99 — a large-but-valid
    amount must round-trip exactly, with no float precision loss."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("999999.99"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "999999.99")
    assert response.status_code == 201
    assert response.json()["grand_total"] == "999999.99"


def test_tax_rounding_half_up_at_the_half_cent_boundary(client: TestClient, db: Session) -> None:
    """0.145 -> rounds to 0.15 under ROUND_HALF_UP (ties away from zero),
    not 0.14 (banker's rounding) and not a float artifact like
    0.14499999999999996."""
    store = make_store(db)
    # rate chosen so the raw tax on a $1.00 line is exactly 0.145.
    tax_rate = make_tax_rate(db, rate_percent=Decimal("14.500"))
    product = make_product(
        db,
        store,
        current_price=Decimal("1.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "1.15")
    assert response.status_code == 201
    body = response.json()
    assert body["tax_total"] == "0.15"
    assert body["grand_total"] == "1.15"


def test_multiple_line_items_sum_exactly(client: TestClient, db: Session) -> None:
    """Three lines whose individual amounts are innocuous in floating
    point but whose SUM is the classic 0.1 + 0.2 != 0.3 float trap —
    Decimal arithmetic must get this exactly right."""
    import uuid

    store = make_store(db)
    p1 = make_product(db, store, current_price=Decimal("0.10"), current_qty_on_hand=Decimal("10"))
    p2 = make_product(db, store, current_price=Decimal("0.20"), current_qty_on_hand=Decimal("10"))
    p3 = make_product(db, store, current_price=Decimal("0.30"), current_qty_on_hand=Decimal("10"))
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": f"txn-{uuid.uuid4().hex}",
            "lines": [
                {"product_id": p1.id, "quantity": "1"},
                {"product_id": p2.id, "quantity": "1"},
                {"product_id": p3.id, "quantity": "1"},
            ],
            "payments": [{"payment_method": "CASH", "amount": "0.60"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["grand_total"] == "0.60"


def test_discount_reduces_taxable_base_with_exact_cents(client: TestClient, db: Session) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("7.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    # taxable = 10.00 - 3.33 = 6.67; tax = 6.67 * 0.07 = 0.4669 -> 0.47
    response = _checkout(client, headers, store, product, "1", "7.14", discount="3.33")
    assert response.status_code == 201
    body = response.json()
    assert body["discount_total"] == "3.33"
    assert body["tax_total"] == "0.47"
    assert body["grand_total"] == "7.14"


def test_exact_cash_payment_produces_zero_change(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("19.99"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "19.99")
    assert response.status_code == 201
    assert response.json()["change_due"] == "0.00"


def test_cash_overpayment_change_is_exact_decimal(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("0.10"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, _last_username(db), DEFAULT_TEST_PASSWORD)
    response = _checkout(client, headers, store, product, "1", "1.00")
    assert response.status_code == 201
    assert response.json()["change_due"] == "0.90"


def test_no_float_type_used_for_money_in_sales_service() -> None:
    """Static check backing up the dynamic tests above: the sales service
    module must not import or construct `float` for any monetary value.
    (A grep-style guard, not a runtime behavior test — cheap insurance
    against a future edit reintroducing float arithmetic.)"""
    import inspect

    from app.modules.sales import service

    source = inspect.getsource(service)
    assert "float(" not in source
