"""M2 hardening audit Section 12: a user scoped to one store must never
be able to read or write another store's data by manipulating an
ID/store_id in the request — regardless of what the frontend shows them.
Every one of these attempts a real cross-store access through the actual
HTTP API and asserts the server rejects it; none of them rely on the
frontend hiding a button.
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, CASHIER, INVENTORY_CLERK
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def test_cashier_cannot_read_another_stores_product_by_id(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    other_product = make_product(db, store_b, name="Store B Widget")
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_1")
    db.commit()
    headers = auth_headers(client, "iso_cashier_1", DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/products/{other_product.id}", headers=headers)
    # 404, not 403 — existence of another store's product is not
    # confirmed either way.
    assert response.status_code == 404


def test_cashier_cannot_scan_another_stores_barcode(client: TestClient, db: Session) -> None:
    from app.modules.products.models import ProductBarcode

    store_a = make_store(db)
    store_b = make_store(db)
    other_product = make_product(db, store_b)
    db.add(ProductBarcode(product_id=other_product.id, barcode="ISO-BC-1", is_primary=True))
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_2")
    db.commit()
    headers = auth_headers(client, "iso_cashier_2", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products/barcode/ISO-BC-1", headers=headers)
    assert response.status_code == 404


def test_cashier_cannot_list_another_stores_products_via_store_id_filter(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_3")
    db.commit()
    headers = auth_headers(client, "iso_cashier_3", DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/products?store_id={store_b.id}", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_cashier_omitting_store_id_only_sees_their_own_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_product(db, store_a, name="A Product", sku=f"A-{unique_suffix()}")
    make_product(db, store_b, name="B Product", sku=f"B-{unique_suffix()}")
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_4")
    db.commit()
    headers = auth_headers(client, "iso_cashier_4", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products", headers=headers)
    assert response.status_code == 200
    names = {p["name"] for p in response.json()}
    assert "A Product" in names
    assert "B Product" not in names


def test_inventory_clerk_cannot_create_product_for_another_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_user_with_role(db, store_a, INVENTORY_CLERK, username="iso_clerk_1")
    db.commit()
    headers = auth_headers(client, "iso_clerk_1", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/products",
        headers=headers,
        json={
            "store_id": store_b.id,
            "sku": f"ISO-{unique_suffix()}",
            "name": "Smuggled Product",
            "current_price": "1.00",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_inventory_clerk_cannot_adjust_another_stores_stock(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    other_product = make_product(db, store_b, current_qty_on_hand=Decimal("10"))
    make_user_with_role(db, store_a, INVENTORY_CLERK, username="iso_clerk_2")
    db.commit()
    headers = auth_headers(client, "iso_clerk_2", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/inventory/adjustments",
        headers=headers,
        json={
            "product_id": other_product.id,
            "quantity_delta": "-5",
            "reason_code": "THEFT",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"
    db.refresh(other_product)
    assert other_product.current_qty_on_hand == Decimal("10")


def test_cashier_cannot_sell_against_another_store(client: TestClient, db: Session) -> None:
    """The highest-stakes case: a store_id in the sale request body must
    never let a scoped cashier transact against another store's
    inventory, even if they somehow know a valid product_id there."""
    store_a = make_store(db)
    store_b = make_store(db)
    other_product = make_product(
        db, store_b, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_5")
    db.commit()
    headers = auth_headers(client, "iso_cashier_5", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store_b.id,
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [{"product_id": other_product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "10.00"}],
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"
    db.refresh(other_product)
    assert other_product.current_qty_on_hand == Decimal("10")


def test_cashier_cannot_read_another_stores_sale(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    product_b = make_product(
        db, store_b, current_price=Decimal("5.00"), current_qty_on_hand=Decimal("5")
    )
    make_user_with_role(db, store_b, CASHIER, username="iso_cashier_owner")
    make_user_with_role(db, store_a, CASHIER, username="iso_cashier_intruder")
    db.commit()

    owner_headers = auth_headers(client, "iso_cashier_owner", DEFAULT_TEST_PASSWORD)
    sale_response = client.post(
        "/api/v1/sales",
        headers=owner_headers,
        json={
            "store_id": store_b.id,
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [{"product_id": product_b.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "5.00"}],
        },
    )
    assert sale_response.status_code == 201
    sale_id = sale_response.json()["id"]

    intruder_headers = auth_headers(client, "iso_cashier_intruder", DEFAULT_TEST_PASSWORD)
    read_response = client.get(f"/api/v1/sales/{sale_id}", headers=intruder_headers)
    assert read_response.status_code == 404


def test_cross_store_admin_can_operate_across_stores(client: TestClient, db: Session) -> None:
    """Positive control: an Admin/Manager with no single store_id
    assignment (store_id is NULL) is a deliberate cross-store role, not a
    gap — they must still be able to read/write across stores."""
    store_a = make_store(db)
    store_b = make_store(db)
    product_a = make_product(db, store_a, name="Cross A")
    product_b = make_product(db, store_b, name="Cross B")
    make_user_with_role(db, None, ADMIN, username="iso_admin")
    db.commit()
    headers = auth_headers(client, "iso_admin", DEFAULT_TEST_PASSWORD)

    resp_a = client.get(f"/api/v1/products/{product_a.id}", headers=headers)
    resp_b = client.get(f"/api/v1/products/{product_b.id}", headers=headers)
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    list_response = client.get(f"/api/v1/products?store_id={store_b.id}", headers=headers)
    assert list_response.status_code == 200
