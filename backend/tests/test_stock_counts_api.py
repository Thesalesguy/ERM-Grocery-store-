"""M8 Section 1/2 API layer: stock count RBAC tiers (write/review/post are
genuinely separate permissions, not just labels) and store isolation."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints import inventory as inventory_endpoint
from app.modules.auth.permissions import ADMIN, INVENTORY_CLERK, MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _create_and_open_via_api(
    client: TestClient, headers: dict, store_id: int, product_id: int
) -> dict:
    response = client.post(
        "/api/v1/inventory/stock-counts",
        json={"store_id": store_id, "product_ids": [product_id]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    count = response.json()
    response = client.post(f"/api/v1/inventory/stock-counts/{count['id']}/open", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_inventory_clerk_can_write_but_not_review_or_post(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    username = f"clerk_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    count = _create_and_open_via_api(client, headers, store.id, product.id)

    response = client.post(
        f"/api/v1/inventory/stock-counts/{count['id']}/entries",
        json={"product_id": product.id, "counted_quantity": "9"},
        headers=headers,
    )
    assert response.status_code == 201, response.text

    response = client.post(
        f"/api/v1/inventory/stock-counts/{count['id']}/mark-counted", headers=headers
    )
    assert response.status_code == 200

    response = client.post(f"/api/v1/inventory/stock-counts/{count['id']}/review", headers=headers)
    assert response.status_code == 403

    response = client.post(f"/api/v1/inventory/stock-counts/{count['id']}/post", headers=headers)
    assert response.status_code == 403


def test_manager_can_review_and_post(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    count = _create_and_open_via_api(client, headers, store.id, product.id)
    client.post(
        f"/api/v1/inventory/stock-counts/{count['id']}/entries",
        json={"product_id": product.id, "counted_quantity": "10"},
        headers=headers,
    )
    client.post(f"/api/v1/inventory/stock-counts/{count['id']}/mark-counted", headers=headers)

    response = client.post(f"/api/v1/inventory/stock-counts/{count['id']}/review", headers=headers)
    assert response.status_code == 200

    response = client.post(f"/api/v1/inventory/stock-counts/{count['id']}/post", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "POSTED"


def test_store_scoped_user_cannot_see_other_store_count(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    product_b = make_product(db, store_b)
    username_admin = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username_admin)
    db.commit()
    admin_headers = auth_headers(client, username_admin, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/inventory/stock-counts",
        json={"store_id": store_b.id, "product_ids": [product_b.id]},
        headers=admin_headers,
    )
    assert response.status_code == 201
    count_id = response.json()["id"]

    username_a = f"clerk_a_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username_a)
    db.commit()
    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/inventory/stock-counts/{count_id}", headers=headers_a)
    assert response.status_code in (403, 404)


def test_store_isolation_mutation_test_removing_enforce_store_access(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """Mutation test: the /open route has no route-layer
    enforce_store_access call at all (unlike create_stock_count/
    get_stock_count) — isolation for it depends ENTIRELY on
    open_stock_count's own caller_store_id check. Neutering
    enforce_store_access module-wide (as if the route DID call it and
    that call were disabled) must still leave this route protected,
    proving the service layer is a real, independent line of defense
    and not merely decorative."""
    store_a = make_store(db)
    store_b = make_store(db)
    product_b = make_product(db, store_b)
    username_admin = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username_admin)
    db.commit()
    admin_headers = auth_headers(client, username_admin, DEFAULT_TEST_PASSWORD)
    response = client.post(
        "/api/v1/inventory/stock-counts",
        json={"store_id": store_b.id, "product_ids": [product_b.id]},
        headers=admin_headers,
    )
    count_id = response.json()["id"]

    username_a = f"clerk_a_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username_a)
    db.commit()
    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(inventory_endpoint, "enforce_store_access", lambda *a, **kw: None)

    response = client.post(f"/api/v1/inventory/stock-counts/{count_id}/open", headers=headers_a)
    assert response.status_code == 403
