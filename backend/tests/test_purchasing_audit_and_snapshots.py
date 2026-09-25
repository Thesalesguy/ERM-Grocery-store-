"""M3 Sections 10 and 16: every purchasing mutation that matters is
audited, and historical purchase order / goods receipt costs are frozen
at the time they were recorded — never recalculated from the product
master's current price/cost/WAC, which can (and will) change later."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _last_audit_event(db: Session, action: str, entity_type: str) -> AuditLog | None:
    return (
        db.execute(
            select(AuditLog)
            .where(AuditLog.action == action, AuditLog.entity_type == entity_type)
            .order_by(AuditLog.id.desc())
        )
        .scalars()
        .first()
    )


def test_supplier_create_and_update_are_audited(client: TestClient, db: Session) -> None:
    make_user_with_role(db, None, MANAGER, username="audit_sup_mgr")
    db.commit()
    headers = auth_headers(client, "audit_sup_mgr", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/purchasing/suppliers", headers=headers, json={"name": "Audited Supplier"}
    )
    supplier_id = create_response.json()["id"]

    created_event = _last_audit_event(db, "SUPPLIER_CREATED", "supplier")
    assert created_event is not None
    assert created_event.entity_id == supplier_id

    client.put(
        f"/api/v1/purchasing/suppliers/{supplier_id}", headers=headers, json={"name": "Renamed"}
    )
    updated_event = _last_audit_event(db, "SUPPLIER_UPDATED", "supplier")
    assert updated_event is not None
    assert updated_event.entity_id == supplier_id
    assert updated_event.before_state["name"] == "Audited Supplier"
    assert updated_event.after_state["name"] == "Renamed"


def test_purchase_order_lifecycle_is_audited(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="audit_po_mgr")
    db.commit()
    headers = auth_headers(client, "audit_po_mgr", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "5.00"}],
        },
    ).json()

    created_event = _last_audit_event(db, "PURCHASE_ORDER_CREATED", "purchase_order")
    assert created_event is not None
    assert created_event.entity_id == po["id"]
    assert created_event.user_id is not None

    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    submitted_event = _last_audit_event(db, "PURCHASE_ORDER_SUBMITTED", "purchase_order")
    assert submitted_event is not None
    assert submitted_event.entity_id == po["id"]

    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/cancel",
        headers=headers,
        json={"reason": "test cancellation"},
    )
    cancelled_event = _last_audit_event(db, "PURCHASE_ORDER_CANCELLED", "purchase_order")
    assert cancelled_event is not None
    assert cancelled_event.before_state["status"] == "ORDERED"
    assert cancelled_event.after_state["reason"] == "test cancellation"


def test_goods_receipt_and_return_are_audited(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="audit_recv_mgr")
    db.commit()
    headers = auth_headers(client, "audit_recv_mgr", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "5.00"}],
        },
    ).json()
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    item_id = po["items"][0]["id"]

    receipt = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "5.00"}
            ],
        },
    ).json()

    receipt_event = _last_audit_event(db, "GOODS_RECEIPT_COMPLETED", "goods_receipt")
    assert receipt_event is not None
    assert receipt_event.entity_id == receipt["id"]
    assert receipt_event.user_id is not None

    return_response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/returns",
        headers=headers,
        json={
            "store_id": store.id,
            "return_date": "2024-01-06",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity": "2"}],
        },
    ).json()
    return_event = _last_audit_event(db, "PURCHASE_RETURN_COMPLETED", "purchase_return")
    assert return_event is not None
    assert return_event.entity_id == return_response["id"]


def test_over_receipt_is_flagged_in_the_audit_event(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="audit_over_mgr")
    db.commit()
    headers = auth_headers(client, "audit_over_mgr", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity_ordered": "5", "unit_cost": "5.00"}],
        },
    ).json()
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    item_id = po["items"][0]["id"]

    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "8", "unit_cost": "5.00"}
            ],
        },
    )
    receipt_event = _last_audit_event(db, "GOODS_RECEIPT_COMPLETED", "goods_receipt")
    assert receipt_event is not None
    assert item_id in receipt_event.after_state["over_receipt_line_item_ids"]


# --- Cost snapshots (M3 Section 10) --------------------------------------


def test_purchase_order_item_cost_is_immutable_after_a_later_product_price_change(
    client: TestClient, db: Session
) -> None:
    """Editing the product's catalog price later must not retroactively
    change what a purchase order recorded as the agreed cost — the same
    BR-2 discipline sales already prove for unit_price_at_sale."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_price=Decimal("10.00"))
    make_user_with_role(db, store, MANAGER, username="snap_mgr_1")
    db.commit()
    headers = auth_headers(client, "snap_mgr_1", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity_ordered": "10", "unit_cost": "6.50"}],
        },
    ).json()

    client.put(f"/api/v1/products/{product.id}", headers=headers, json={"current_price": "99.99"})

    unchanged_po = client.get(
        f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers
    ).json()
    assert unchanged_po["items"][0]["unit_cost"] == "6.500000"


def test_goods_receipt_item_cost_is_immutable_after_a_later_wac_change(
    client: TestClient, db: Session
) -> None:
    """A goods receipt item freezes the cost it was actually received at.
    A LATER receipt (which recomputes WAC to a new blended value) must
    never rewrite an earlier receipt's own recorded unit_cost."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="snap_mgr_2")
    db.commit()
    headers = auth_headers(client, "snap_mgr_2", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json={
            "store_id": store.id,
            "supplier_id": supplier.id,
            "order_date": "2024-01-01",
            "client_transaction_id": f"po-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity_ordered": "150", "unit_cost": "10.00"}],
        },
    ).json()
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    item_id = po["items"][0]["id"]

    first_receipt = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {
                    "purchase_order_item_id": item_id,
                    "quantity_received": "100",
                    "unit_cost": "10.00",
                }
            ],
        },
    ).json()

    # Second receipt at a different cost recomputes WAC (100@10 + 50@14).
    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-06",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "50", "unit_cost": "14.00"}
            ],
        },
    )

    unchanged_first_receipt = client.get(
        f"/api/v1/purchasing/goods-receipts/{first_receipt['id']}", headers=headers
    ).json()
    assert unchanged_first_receipt["items"][0]["unit_cost"] == "10.000000"

    db.refresh(product)
    assert product.current_cost == Decimal("11.333333")
