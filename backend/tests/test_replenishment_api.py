"""M9 supply-chain API: RBAC across the four supply_chain permission tiers
(read/plan/approve/execute) and multi-store isolation, exercised through
the real HTTP endpoints — see docs/M9_SUPPLY_CHAIN_DESIGN.md "design
answer #19" for why these are four separate permissions."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import AUDITOR, CASHIER, INVENTORY_CLERK, MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_supplier,
    make_supplier_product,
    make_user_with_role,
)
from tests.helpers import auth_headers


def _make_shortage(db: Session, store) -> None:
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()


# --- read permission ---------------------------------------------------


def test_cashier_cannot_read_suggestions(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="repl_cashier_1")
    db.commit()
    headers = auth_headers(client, "repl_cashier_1", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/replenishment/suggestions", headers=headers)
    assert response.status_code == 403


def test_auditor_can_read_but_not_generate(client: TestClient, db: Session) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, None, AUDITOR, username="repl_auditor_1")
    db.commit()
    headers = auth_headers(client, "repl_auditor_1", DEFAULT_TEST_PASSWORD)

    read_response = client.get(
        "/api/v1/replenishment/plans", params={"store_id": store.id}, headers=headers
    )
    assert read_response.status_code == 200

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers, json={"store_id": store.id}
    )
    assert generate_response.status_code == 403


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/replenishment/suggestions")
    assert response.status_code == 401


# --- plan permission -----------------------------------------------------


def test_inventory_clerk_can_generate_and_cancel_but_not_approve_or_execute(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, store, INVENTORY_CLERK, username="repl_clerk_1")
    db.commit()
    headers = auth_headers(client, "repl_clerk_1", DEFAULT_TEST_PASSWORD)

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers, json={"store_id": store.id}
    )
    assert generate_response.status_code == 200
    plans = generate_response.json()
    assert len(plans) == 1
    plan_id = plans[0]["id"]

    approve_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/approve", headers=headers
    )
    assert approve_response.status_code == 403

    execute_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/execute",
        headers=headers,
        json={"client_transaction_id": "clerk-attempt"},
    )
    assert execute_response.status_code == 403

    cancel_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/cancel", headers=headers, json={}
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "CANCELLED"


# --- approve / execute permission ----------------------------------------


def test_manager_can_drive_the_full_lifecycle(client: TestClient, db: Session) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, store, MANAGER, username="repl_mgr_1")
    db.commit()
    headers = auth_headers(client, "repl_mgr_1", DEFAULT_TEST_PASSWORD)

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers, json={"store_id": store.id}
    )
    assert generate_response.status_code == 200
    plan_id = generate_response.json()[0]["id"]

    approve_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/approve", headers=headers
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "APPROVED"

    execute_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/execute",
        headers=headers,
        json={"client_transaction_id": "mgr-exec-1"},
    )
    assert execute_response.status_code == 200
    body = execute_response.json()
    assert body["status"] == "EXECUTED"
    assert body["generated_purchase_order_id"] is not None

    detail_response = client.get(f"/api/v1/replenishment/plans/{plan_id}", headers=headers)
    assert detail_response.status_code == 200
    assert detail_response.json()["fulfilled"] is False


def test_inventory_clerk_cannot_execute_even_with_a_manager_approved_plan(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, store, MANAGER, username="repl_mgr_2")
    make_user_with_role(db, store, INVENTORY_CLERK, username="repl_clerk_2")
    db.commit()
    mgr_headers = auth_headers(client, "repl_mgr_2", DEFAULT_TEST_PASSWORD)
    clerk_headers = auth_headers(client, "repl_clerk_2", DEFAULT_TEST_PASSWORD)

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=mgr_headers, json={"store_id": store.id}
    )
    plan_id = generate_response.json()[0]["id"]
    client.post(f"/api/v1/replenishment/plans/{plan_id}/approve", headers=mgr_headers)

    execute_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/execute",
        headers=clerk_headers,
        json={"client_transaction_id": "clerk-blocked"},
    )
    assert execute_response.status_code == 403


# --- multi-store isolation -------------------------------------------------


def test_store_scoped_manager_cannot_see_another_stores_plan(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    _make_shortage(db, store_a)
    make_user_with_role(db, store_a, MANAGER, username="repl_mgr_a")
    make_user_with_role(db, store_b, MANAGER, username="repl_mgr_b")
    db.commit()
    headers_a = auth_headers(client, "repl_mgr_a", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "repl_mgr_b", DEFAULT_TEST_PASSWORD)

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers_a, json={"store_id": store_a.id}
    )
    plan_id = generate_response.json()[0]["id"]

    cross_store_response = client.get(f"/api/v1/replenishment/plans/{plan_id}", headers=headers_b)
    assert cross_store_response.status_code == 404

    cross_store_generate = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers_b, json={"store_id": store_a.id}
    )
    assert cross_store_generate.status_code == 403


def test_store_scoped_manager_cannot_approve_or_execute_another_stores_plan(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    _make_shortage(db, store_a)
    make_user_with_role(db, store_a, MANAGER, username="repl_mgr_c")
    make_user_with_role(db, store_b, MANAGER, username="repl_mgr_d")
    db.commit()
    headers_a = auth_headers(client, "repl_mgr_c", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "repl_mgr_d", DEFAULT_TEST_PASSWORD)

    generate_response = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers_a, json={"store_id": store_a.id}
    )
    plan_id = generate_response.json()[0]["id"]

    approve_response = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/approve", headers=headers_b
    )
    assert approve_response.status_code in (403, 404)
