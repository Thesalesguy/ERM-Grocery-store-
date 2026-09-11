"""Inventory API: stock lookup, authenticated/audited stock adjustment,
and permission enforcement."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import CASHIER, INVENTORY_CLERK
from app.modules.inventory.models import InventoryMovement
from tests.factories import DEFAULT_TEST_PASSWORD, make_product, make_store, make_user_with_role
from tests.helpers import auth_headers


def test_stock_lookup(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("42"))
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk1")
    db.commit()
    headers = auth_headers(client, "clerk1", DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/inventory/stock/{product.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["current_qty_on_hand"] == "42.000"


def test_low_stock_flag(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_product(db, store, sku="LOW", current_qty_on_hand=Decimal("2"), reorder_point=Decimal("5"))
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk2")
    db.commit()
    headers = auth_headers(client, "clerk2", DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/inventory/stock?store_id={store.id}&low_stock_only=true", headers=headers
    )
    assert response.status_code == 200
    skus = [p["sku"] for p in response.json()]
    assert "LOW" in skus
    assert response.json()[0]["is_low_stock"] is True


def test_stock_adjustment_creates_movement_updates_cache_and_audits(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk3")
    db.commit()
    headers = auth_headers(client, "clerk3", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/inventory/adjustments",
        headers=headers,
        json={
            "product_id": product.id,
            "quantity_delta": "-3",
            "reason_code": "DAMAGE",
            "notes": "Dropped a case",
        },
    )
    assert response.status_code == 201
    adjustment_id = response.json()["id"]

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("7")

    movement = (
        db.query(InventoryMovement)
        .filter_by(reference_type="stock_adjustment", reference_id=adjustment_id)
        .one()
    )
    assert movement.movement_type == "STOCK_ADJUSTMENT_OUT"
    assert movement.quantity_delta == Decimal("-3")

    audit_row = (
        db.query(AuditLog)
        .filter_by(action="STOCK_ADJUSTMENT_CREATED", entity_id=adjustment_id)
        .one()
    )
    assert audit_row.after_state["reason_code"] == "DAMAGE"


def test_zero_delta_adjustment_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk4")
    db.commit()
    headers = auth_headers(client, "clerk4", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/inventory/adjustments",
        headers=headers,
        json={"product_id": product.id, "quantity_delta": "0", "reason_code": "OTHER"},
    )
    assert response.status_code == 422


def test_cashier_cannot_adjust_stock(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    make_user_with_role(db, store, CASHIER, username="cashier_inv")
    db.commit()
    headers = auth_headers(client, "cashier_inv", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/inventory/adjustments",
        headers=headers,
        json={"product_id": product.id, "quantity_delta": "1", "reason_code": "OTHER"},
    )
    assert response.status_code == 403


def test_movement_history_lists_adjustment(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("5"))
    make_user_with_role(db, store, INVENTORY_CLERK, username="clerk5")
    db.commit()
    headers = auth_headers(client, "clerk5", DEFAULT_TEST_PASSWORD)

    client.post(
        "/api/v1/inventory/adjustments",
        headers=headers,
        json={
            "product_id": product.id,
            "quantity_delta": "2",
            "reason_code": "STOCKTAKE_CORRECTION",
        },
    )
    response = client.get(f"/api/v1/inventory/movements?product_id={product.id}", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["movement_type"] == "STOCK_ADJUSTMENT_IN"
