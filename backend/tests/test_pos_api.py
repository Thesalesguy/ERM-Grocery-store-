"""POS / sale-finalization API: the full scan -> cart -> checkout ->
receipt workflow, tax/discount computation, payment validation, and
transactional-rollback behavior on failure."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import CASHIER
from app.modules.sales.models import Payment, Sale, SaleItem
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_tax_rate,
    make_user_with_role,
)
from tests.helpers import auth_headers


def _setup_cashier(db: Session, store) -> None:
    make_user_with_role(db, store, CASHIER, username="pos_cashier")
    db.commit()


def test_scan_then_checkout_workflow(client: TestClient, db: Session) -> None:
    """Scan a barcode, look up the product, then check out — the primary
    POS interaction end to end."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("9.99"), current_qty_on_hand=Decimal("50")
    )
    from app.modules.products.models import ProductBarcode

    db.add(ProductBarcode(product_id=product.id, barcode="1234500001", is_primary=True))
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    scan_response = client.get("/api/v1/products/barcode/1234500001", headers=headers)
    assert scan_response.status_code == 200
    found_product = scan_response.json()

    checkout_response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": found_product["id"], "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "9.99"}],
        },
    )
    assert checkout_response.status_code == 201
    sale = checkout_response.json()
    assert sale["status"] == "COMPLETED"
    assert sale["grand_total"] == "9.99"
    assert sale["change_due"] == "0.00"


def test_multiple_quantities_and_line_total(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("3.50"), current_qty_on_hand=Decimal("50")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "4"}],
            "payments": [{"payment_method": "CASH", "amount": "14.00"}],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["subtotal"] == "14.00"
    assert body["items"][0]["line_total"] == "14.00"


def test_tax_is_computed_server_side(client: TestClient, db: Session) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("18.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "11.80"}],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["tax_total"] == "1.80"
    assert body["grand_total"] == "11.80"
    assert body["items"][0]["tax_amount"] == "1.80"


def test_discount_is_applied_and_reduces_tax_base(client: TestClient, db: Session) -> None:
    store = make_store(db)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("10.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("100.00"),
        current_qty_on_hand=Decimal("10"),
        tax_rate_id=tax_rate.id,
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1", "discount_amount": "20.00"}],
            "payments": [{"payment_method": "CASH", "amount": "88.00"}],
        },
    )
    assert response.status_code == 201
    body = response.json()
    # taxable = 100 - 20 = 80; tax = 8.00; grand total = 80 + 8 = 88
    assert body["discount_total"] == "20.00"
    assert body["tax_total"] == "8.00"
    assert body["grand_total"] == "88.00"


def test_discount_exceeding_line_subtotal_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1", "discount_amount": "50.00"}],
            "payments": [{"payment_method": "CASH", "amount": "1.00"}],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DISCOUNT"


def test_split_tender_payment(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("50.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [
                {"payment_method": "CASH", "amount": "20.00"},
                {"payment_method": "CARD", "amount": "30.00"},
            ],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert len(body["payments"]) == 2
    assert sum(Decimal(p["amount"]) for p in body["payments"]) == Decimal(body["grand_total"])


def test_receipt_contains_historical_snapshot_fields(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db,
        store,
        current_price=Decimal("7.00"),
        current_cost=Decimal("3.500000"),
        current_qty_on_hand=Decimal("10"),
        name="Receipt Item",
        sku="RCPT-1",
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "2"}],
            "payments": [{"payment_method": "CASH", "amount": "14.00"}],
        },
    )
    sale_id = create_response.json()["id"]

    receipt_response = client.get(f"/api/v1/sales/{sale_id}", headers=headers)
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["sale_number"].startswith(f"S{store.id}-")
    assert receipt["completed_at"] is not None
    item = receipt["items"][0]
    assert item["product_name"] == "Receipt Item"
    assert item["product_sku"] == "RCPT-1"
    assert item["unit_price_at_sale"] == "7.00"
    assert item["unit_cost_at_sale"] == "3.500000"


def test_insufficient_stock_creates_nothing(client: TestClient, db: Session) -> None:
    """M2 task Section 7/12: a rejected sale must leave no sale, no sale
    items, no payments, and no inventory movement behind."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("5.00"), current_qty_on_hand=Decimal("1")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    sales_before = db.query(Sale).count()
    items_before = db.query(SaleItem).count()
    payments_before = db.query(Payment).count()

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "5"}],
            "payments": [{"payment_method": "CASH", "amount": "25.00"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_STOCK"

    assert db.query(Sale).count() == sales_before
    assert db.query(SaleItem).count() == items_before
    assert db.query(Payment).count() == payments_before
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("1")


def test_underpayment_rejected_and_creates_nothing(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    sales_before = db.query(Sale).count()
    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "5.00"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_PAYMENT"
    assert db.query(Sale).count() == sales_before
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10")


def test_non_cash_overpayment_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CARD", "amount": "50.00"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OVERPAYMENT_NOT_ALLOWED"


def test_cash_overpayment_produces_change(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "50.00"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["change_due"] == "30.00"


def test_empty_cart_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [],
            "payments": [{"payment_method": "CASH", "amount": "1"}],
        },
    )
    assert response.status_code == 422


def test_client_cannot_submit_price_or_total(client: TestClient, db: Session) -> None:
    """M2 task Section 14: the client can only send product_id, quantity,
    and discount — never a price or a total. Sending extra fields is
    simply ignored; the server computes everything from the catalog."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    _setup_cashier(db, store)
    headers = auth_headers(client, "pos_cashier", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1", "unit_price_at_sale": "0.01"}],
            "payments": [{"payment_method": "CASH", "amount": "10.00"}],
        },
    )
    assert response.status_code == 201
    # The bogus client-supplied price is ignored; server used the real
    # catalog price.
    assert response.json()["items"][0]["unit_price_at_sale"] == "10.00"


def test_cashier_cannot_read_sales_without_permission(client: TestClient, db: Session) -> None:
    """Cashier role in the seeded matrix DOES have sales.read — verify
    inventory-only roles (Inventory Clerk) cannot use the POS instead."""
    from app.modules.auth.permissions import INVENTORY_CLERK

    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("5"))
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk_pos")
    db.commit()
    headers = auth_headers(client, "clerk_pos", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "100"}],
        },
    )
    assert response.status_code == 403
