"""M3 Section 8: goods receiving and purchase returns must be safe
against retries — a duplicate request with the same client_transaction_id
returns the original result rather than creating a second receipt/return
and double-applying its inventory effect.

See tests/test_purchasing_concurrency.py
(test_f_concurrent_duplicate_receipt_requests_create_only_one_receipt) for
the genuinely-concurrent-duplicate case (two real threads/connections);
this file covers the sequential cases HTTP clients actually hit: a
double-click, a retry after the response was lost, and a retry after a
business failure.
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.auth.permissions import MANAGER
from app.modules.purchasing.models import GoodsReceipt, PurchaseOrder, PurchaseReturn
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _setup_ordered_po(client, headers, store, supplier, product, quantity="10"):
    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [
                {"product_id": product.id, "quantity_ordered": quantity, "unit_cost": "5.00"}
            ],
        },
    ).json()
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    return client.get(f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers).json()


def test_exact_duplicate_receipt_request_returns_the_same_receipt(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="idem_recv_mgr_1")
    db.commit()
    headers = auth_headers(client, "idem_recv_mgr_1", DEFAULT_TEST_PASSWORD)

    po = _setup_ordered_po(client, headers, store, supplier, product)
    item_id = po["items"][0]["id"]
    key = f"txn-{unique_suffix()}"
    payload = {
        "received_date": "2024-01-05",
        "client_transaction_id": key,
        "lines": [
            {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "5.00"}
        ],
    }

    first = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive", headers=headers, json=payload
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive", headers=headers, json=payload
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    count = db.execute(
        select(func.count())
        .select_from(GoodsReceipt)
        .where(GoodsReceipt.client_transaction_id == key)
    ).scalar_one()
    assert count == 1

    db.refresh(product)
    # Only received once (10), not twice (20) — the second request did
    # not double-apply the inventory effect.
    assert product.current_qty_on_hand == Decimal("10")


def test_retry_after_business_failure_is_a_fresh_receipt_attempt(
    client: TestClient, db: Session
) -> None:
    """A failed attempt (e.g. an invalid line) creates nothing, so its key
    must not block a later, corrected retry — same principle as sales'
    equivalent test."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="idem_recv_mgr_2")
    db.commit()
    headers = auth_headers(client, "idem_recv_mgr_2", DEFAULT_TEST_PASSWORD)

    po = _setup_ordered_po(client, headers, store, supplier, product)
    item_id = po["items"][0]["id"]
    key = f"txn-{unique_suffix()}"

    failing_response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": key,
            "lines": [
                {"purchase_order_item_id": 999999999, "quantity_received": "1", "unit_cost": "1.00"}
            ],
        },
    )
    assert failing_response.status_code == 404

    corrected_response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": key,
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "5.00"}
            ],
        },
    )
    assert corrected_response.status_code == 201

    count = db.execute(
        select(func.count())
        .select_from(GoodsReceipt)
        .where(GoodsReceipt.client_transaction_id == key)
    ).scalar_one()
    assert count == 1


def test_exact_duplicate_return_request_returns_the_same_return(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="idem_ret_mgr_1")
    db.commit()
    headers = auth_headers(client, "idem_ret_mgr_1", DEFAULT_TEST_PASSWORD)

    po = _setup_ordered_po(client, headers, store, supplier, product)
    item_id = po["items"][0]["id"]
    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "5.00"}
            ],
        },
    )

    key = f"txn-{unique_suffix()}"
    payload = {
        "store_id": store.id,
        "return_date": "2024-01-06",
        "client_transaction_id": key,
        "lines": [{"product_id": product.id, "quantity": "2"}],
    }
    first = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/returns", headers=headers, json=payload
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/returns", headers=headers, json=payload
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    count = db.execute(
        select(func.count())
        .select_from(PurchaseReturn)
        .where(PurchaseReturn.client_transaction_id == key)
    ).scalar_one()
    assert count == 1

    db.refresh(product)
    # Returned once (2), not twice (4) — stock is 10 - 2 = 8, not 6.
    assert product.current_qty_on_hand == Decimal("8")


def test_exact_duplicate_po_creation_request_returns_the_same_purchase_order(
    client: TestClient, db: Session
) -> None:
    """M19: purchase-order creation gained the same client_transaction_id
    idempotency key already used by receipts/returns above — a
    double-click or retried POST must not silently create two separate
    DRAFT/ORDERED purchase orders for the same intent."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="idem_po_mgr_1")
    db.commit()
    headers = auth_headers(client, "idem_po_mgr_1", DEFAULT_TEST_PASSWORD)

    key = f"po-{unique_suffix()}"
    payload = {
        "store_id": store.id,
        "supplier_id": supplier.id,
        "order_date": "2024-01-01",
        "client_transaction_id": key,
        "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "5.00"}],
    }

    first = client.post("/api/v1/purchasing/purchase-orders", headers=headers, json=payload)
    assert first.status_code == 201
    second = client.post("/api/v1/purchasing/purchase-orders", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    count = db.execute(
        select(func.count()).select_from(PurchaseOrder).where(
            PurchaseOrder.client_transaction_id == key
        )
    ).scalar_one()
    assert count == 1


def test_retry_after_po_creation_business_failure_is_a_fresh_attempt(
    client: TestClient, db: Session
) -> None:
    """A failed PO-creation attempt (e.g. a nonexistent supplier) creates
    nothing, so its key must not block a later, corrected retry — same
    principle as receiving's equivalent test above."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="idem_po_mgr_2")
    db.commit()
    headers = auth_headers(client, "idem_po_mgr_2", DEFAULT_TEST_PASSWORD)

    key = f"po-{unique_suffix()}"
    failing_response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": 999999999,
            "order_date": "2024-01-01",
            "client_transaction_id": key,
            "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "5.00"}],
        },
    )
    assert failing_response.status_code == 422

    corrected_response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": key,
            "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "5.00"}],
        },
    )
    assert corrected_response.status_code == 201

    count = db.execute(
        select(func.count()).select_from(PurchaseOrder).where(
            PurchaseOrder.client_transaction_id == key
        )
    ).scalar_one()
    assert count == 1
