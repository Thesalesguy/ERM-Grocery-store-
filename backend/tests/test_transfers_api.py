"""M8 Section 4/5 API layer: transfer RBAC tiers (write/ship/receive) and
store isolation."""

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


def _create_transfer_via_api(
    client: TestClient, headers: dict, from_store_id: int, to_store_id: int, source_product_id: int
) -> dict:
    response = client.post(
        "/api/v1/transfers",
        json={
            "from_store_id": from_store_id,
            "to_store_id": to_store_id,
            "requested_date": "2024-01-01",
            "lines": [{"source_product_id": source_product_id, "requested_quantity": "10"}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cashier_cannot_create_transfer(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku=f"SKU-{unique_suffix()}")
    make_product(db, store_b, sku=source.sku)
    username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store_a, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/transfers",
        json={
            "from_store_id": store_a.id,
            "to_store_id": store_b.id,
            "requested_date": "2024-01-01",
            "lines": [{"source_product_id": source.id, "requested_quantity": "1"}],
        },
        headers=headers,
    )
    assert response.status_code == 403


def test_inventory_clerk_can_create_ship_and_receive(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("50")
    )
    make_product(db, store_b, sku=source.sku)
    username_a = f"clerk_a_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username_a)
    db.commit()
    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)

    transfer = _create_transfer_via_api(client, headers_a, store_a.id, store_b.id, source.id)
    line_id = transfer["lines"][0]["id"]

    response = client.post(
        f"/api/v1/transfers/{transfer['id']}/ship",
        json={
            "client_transaction_id": f"ship-{unique_suffix()}",
            "lines": [{"transfer_line_id": line_id, "quantity_to_ship": "10"}],
        },
        headers=headers_a,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SHIPPED"

    username_b = f"clerk_b_{unique_suffix()}"
    make_user_with_role(db, store_b, INVENTORY_CLERK, username=username_b)
    db.commit()
    headers_b = auth_headers(client, username_b, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/transfers/{transfer['id']}/receipts",
        json={
            "received_date": "2024-01-02",
            "client_transaction_id": f"recv-{unique_suffix()}",
            "lines": [{"transfer_line_id": line_id, "quantity_received": "10"}],
        },
        headers=headers_b,
    )
    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["quantity_received"] == "10.000"


def test_store_scoped_source_user_cannot_receive(client: TestClient, db: Session) -> None:
    """A user scoped to the SOURCE store must not be able to receive at
    the destination store just by naming the transfer id."""
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("10")
    )
    make_product(db, store_b, sku=source.sku)
    username_a = f"clerk_a_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username_a)
    db.commit()
    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)

    transfer = _create_transfer_via_api(client, headers_a, store_a.id, store_b.id, source.id)
    line_id = transfer["lines"][0]["id"]
    client.post(
        f"/api/v1/transfers/{transfer['id']}/ship",
        json={
            "client_transaction_id": f"ship-{unique_suffix()}",
            "lines": [{"transfer_line_id": line_id, "quantity_to_ship": "10"}],
        },
        headers=headers_a,
    )

    response = client.post(
        f"/api/v1/transfers/{transfer['id']}/receipts",
        json={
            "received_date": "2024-01-02",
            "client_transaction_id": f"recv-{unique_suffix()}",
            "lines": [{"transfer_line_id": line_id, "quantity_received": "10"}],
        },
        headers=headers_a,
    )
    assert response.status_code == 403


def test_store_scoped_unrelated_user_cannot_see_transfer(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    store_c = make_store(db)
    source = make_product(db, store_a, sku=f"SKU-{unique_suffix()}")
    make_product(db, store_b, sku=source.sku)
    username_admin = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username_admin)
    db.commit()
    admin_headers = auth_headers(client, username_admin, DEFAULT_TEST_PASSWORD)
    transfer = _create_transfer_via_api(client, admin_headers, store_a.id, store_b.id, source.id)

    username_c = f"clerk_c_{unique_suffix()}"
    make_user_with_role(db, store_c, INVENTORY_CLERK, username=username_c)
    db.commit()
    headers_c = auth_headers(client, username_c, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/transfers/{transfer['id']}", headers=headers_c)
    assert response.status_code == 404
