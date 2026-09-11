"""Supplier, purchase-order, goods-receiving, and purchase-return API:
lifecycle, validation, RBAC, and multi-store isolation, all exercised
through the real HTTP endpoints (not the service layer directly — that's
tests/test_purchasing.py and the WAC-specific tests there)."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import AUDITOR, CASHIER, INVENTORY_CLERK, MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _po_payload(store, supplier, product, *, quantity="10", unit_cost="5.00"):
    return {
        "store_id": store.id,
        "supplier_id": supplier.id,
        "order_date": "2024-01-01",
        "lines": [{"product_id": product.id, "quantity_ordered": quantity, "unit_cost": unit_cost}],
    }


# --- Suppliers --------------------------------------------------------------


def test_create_and_get_supplier(client: TestClient, db: Session) -> None:
    make_user_with_role(db, None, MANAGER, username="sup_mgr_1")
    db.commit()
    headers = auth_headers(client, "sup_mgr_1", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/purchasing/suppliers",
        headers=headers,
        json={
            "name": "Acme Distributors",
            "code": f"ACME-{unique_suffix()}",
            "phone": "+255700000000",
        },
    )
    assert response.status_code == 201
    supplier_id = response.json()["id"]
    assert response.json()["is_active"] is True

    get_response = client.get(f"/api/v1/purchasing/suppliers/{supplier_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Acme Distributors"


def test_duplicate_supplier_code_rejected(client: TestClient, db: Session) -> None:
    make_user_with_role(db, None, MANAGER, username="sup_mgr_2")
    db.commit()
    headers = auth_headers(client, "sup_mgr_2", DEFAULT_TEST_PASSWORD)
    code = f"DUP-{unique_suffix()}"

    first = client.post(
        "/api/v1/purchasing/suppliers", headers=headers, json={"name": "Supplier A", "code": code}
    )
    assert first.status_code == 201
    second = client.post(
        "/api/v1/purchasing/suppliers", headers=headers, json={"name": "Supplier B", "code": code}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_SUPPLIER_CODE"


def test_supplier_visible_across_stores(client: TestClient, db: Session) -> None:
    """M3 decision: suppliers are global/shared, not store-scoped — a
    user assigned to one store can still see a supplier regardless of
    which store first created it (there is no "which store" for a
    supplier at all)."""
    store_a = make_store(db)
    supplier = make_supplier(db, name="Shared Supplier")
    make_user_with_role(db, store_a, INVENTORY_CLERK, username="sup_clerk_1")
    db.commit()
    headers = auth_headers(client, "sup_clerk_1", DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/purchasing/suppliers/{supplier.id}", headers=headers)
    assert response.status_code == 200


def test_cashier_cannot_create_supplier(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="sup_cashier_1")
    db.commit()
    headers = auth_headers(client, "sup_cashier_1", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/purchasing/suppliers", headers=headers, json={"name": "Should Not Work"}
    )
    assert response.status_code == 403


def test_auditor_can_read_but_not_write_suppliers(client: TestClient, db: Session) -> None:
    supplier = make_supplier(db)
    make_user_with_role(db, None, AUDITOR, username="sup_auditor_1")
    db.commit()
    headers = auth_headers(client, "sup_auditor_1", DEFAULT_TEST_PASSWORD)

    read_response = client.get(f"/api/v1/purchasing/suppliers/{supplier.id}", headers=headers)
    assert read_response.status_code == 200

    write_response = client.put(
        f"/api/v1/purchasing/suppliers/{supplier.id}", headers=headers, json={"name": "Hacked"}
    )
    assert write_response.status_code == 403


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/purchasing/suppliers")
    assert response.status_code == 401


# --- Purchase order lifecycle ------------------------------------------------


def test_purchase_order_created_in_draft_and_touches_no_inventory(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="po_mgr_1")
    db.commit()
    headers = auth_headers(client, "po_mgr_1", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["items"][0]["quantity_remaining"] == "10.000"

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("0")


def test_submit_purchase_order_transitions_to_ordered(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="po_mgr_2")
    db.commit()
    headers = auth_headers(client, "po_mgr_2", DEFAULT_TEST_PASSWORD)

    po_id = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    ).json()["id"]

    response = client.post(f"/api/v1/purchasing/purchase-orders/{po_id}/submit", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ORDERED"


def test_cannot_submit_an_already_submitted_purchase_order(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="po_mgr_3")
    db.commit()
    headers = auth_headers(client, "po_mgr_3", DEFAULT_TEST_PASSWORD)

    po_id = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    ).json()["id"]
    client.post(f"/api/v1/purchasing/purchase-orders/{po_id}/submit", headers=headers)

    response = client.post(f"/api/v1/purchasing/purchase-orders/{po_id}/submit", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PO_STATE"


def test_cancel_purchase_order(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="po_mgr_4")
    db.commit()
    headers = auth_headers(client, "po_mgr_4", DEFAULT_TEST_PASSWORD)

    po_id = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    ).json()["id"]

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po_id}/cancel",
        headers=headers,
        json={"reason": "Supplier out of stock"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_cannot_cancel_a_fully_received_purchase_order(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="po_mgr_5")
    db.commit()
    headers = auth_headers(client, "po_mgr_5", DEFAULT_TEST_PASSWORD)

    po_id = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product, quantity="5"),
    ).json()["id"]
    client.post(f"/api/v1/purchasing/purchase-orders/{po_id}/submit", headers=headers)
    po = client.get(f"/api/v1/purchasing/purchase-orders/{po_id}", headers=headers).json()
    item_id = po["items"][0]["id"]

    client.post(
        f"/api/v1/purchasing/purchase-orders/{po_id}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "5", "unit_cost": "5.00"}
            ],
        },
    )

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po_id}/cancel", headers=headers, json={}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PO_STATE"


def test_cashier_cannot_create_or_receive_purchase_orders(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, CASHIER, username="po_cashier_1")
    db.commit()
    headers = auth_headers(client, "po_cashier_1", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    )
    assert create_response.status_code == 403


# --- Goods receiving ----------------------------------------------------


def _create_and_submit_po(
    client, headers, store, supplier, product, quantity="10", unit_cost="5.00"
):
    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product, quantity=quantity, unit_cost=unit_cost),
    ).json()
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/submit", headers=headers)
    detail = client.get(f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers).json()
    return detail


def test_full_receive_updates_stock_and_wac(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="recv_mgr_1")
    db.commit()
    headers = auth_headers(client, "recv_mgr_1", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(
        client, headers, store, supplier, product, quantity="10", unit_cost="4.00"
    )
    item_id = po["items"][0]["id"]

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "4.00"}
            ],
        },
    )
    assert response.status_code == 201
    assert response.json()["items"][0]["unit_cost"] == "4.000000"

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10")
    assert product.current_cost == Decimal("4.000000")

    final_po = client.get(f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers).json()
    assert final_po["status"] == "RECEIVED"


def test_partial_receive_leaves_po_partially_received(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_2")
    db.commit()
    headers = auth_headers(client, "recv_mgr_2", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product, quantity="10")
    item_id = po["items"][0]["id"]

    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "4", "unit_cost": "5.00"}
            ],
        },
    )
    final_po = client.get(f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers).json()
    assert final_po["status"] == "PARTIALLY_RECEIVED"
    assert final_po["items"][0]["quantity_received"] == "4.000"
    assert final_po["items"][0]["quantity_remaining"] == "6.000"


def test_over_receipt_allowed_and_flagged(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_3")
    db.commit()
    headers = auth_headers(client, "recv_mgr_3", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product, quantity="10")
    item_id = po["items"][0]["id"]

    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "15", "unit_cost": "5.00"}
            ],
        },
    )
    final_po = client.get(f"/api/v1/purchasing/purchase-orders/{po['id']}", headers=headers).json()
    assert final_po["items"][0]["quantity_received"] == "15.000"
    assert final_po["items"][0]["is_over_received"] is True
    assert final_po["status"] == "RECEIVED"


def test_cannot_receive_against_draft_purchase_order(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_4")
    db.commit()
    headers = auth_headers(client, "recv_mgr_4", DEFAULT_TEST_PASSWORD)

    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    ).json()
    item_id = po["items"][0]["id"]

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "5", "unit_cost": "5.00"}
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PO_STATE"


def test_cannot_receive_against_cancelled_purchase_order_via_api(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_5")
    db.commit()
    headers = auth_headers(client, "recv_mgr_5", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product)
    item_id = po["items"][0]["id"]
    client.post(f"/api/v1/purchasing/purchase-orders/{po['id']}/cancel", headers=headers, json={})

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "5", "unit_cost": "5.00"}
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PO_STATE"


def test_cannot_receive_against_already_fully_received_po(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_6")
    db.commit()
    headers = auth_headers(client, "recv_mgr_6", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product, quantity="5")
    item_id = po["items"][0]["id"]
    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "5", "unit_cost": "5.00"}
            ],
        },
    )

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-06",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "1", "unit_cost": "5.00"}
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PO_STATE"


def test_zero_and_negative_receive_quantities_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_7")
    db.commit()
    headers = auth_headers(client, "recv_mgr_7", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product)
    item_id = po["items"][0]["id"]

    for bad_qty in ("0", "-5"):
        response = client.post(
            f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
            headers=headers,
            json={
                "received_date": "2024-01-05",
                "client_transaction_id": f"txn-{unique_suffix()}",
                "lines": [
                    {
                        "purchase_order_item_id": item_id,
                        "quantity_received": bad_qty,
                        "unit_cost": "5.00",
                    }
                ],
            },
        )
        assert response.status_code == 422


def test_receiving_wrong_purchase_order_item_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product_a = make_product(db, store)
    product_b = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_8")
    db.commit()
    headers = auth_headers(client, "recv_mgr_8", DEFAULT_TEST_PASSWORD)

    po_a = _create_and_submit_po(client, headers, store, supplier, product_a)
    po_b = _create_and_submit_po(client, headers, store, supplier, product_b)
    item_from_po_b = po_b["items"][0]["id"]

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po_a['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {
                    "purchase_order_item_id": item_from_po_b,
                    "quantity_received": "1",
                    "unit_cost": "5.00",
                }
            ],
        },
    )
    assert response.status_code == 404

    # No orphan receipt or inventory movement from the rejected attempt —
    # rejecting one line must leave nothing behind, not a partial receipt.
    from app.modules.inventory.models import InventoryMovement
    from app.modules.purchasing.models import GoodsReceipt

    assert db.query(GoodsReceipt).filter_by(purchase_order_id=po_a["id"]).count() == 0
    assert (
        db.query(InventoryMovement)
        .filter_by(product_id=product_a.id, movement_type="PURCHASE_RECEIPT")
        .count()
        == 0
    )
    db.refresh(product_a)
    assert product_a.current_qty_on_hand == Decimal("0")


def test_zero_cost_receipt_is_allowed_for_free_or_promotional_stock(
    client: TestClient, db: Session
) -> None:
    """M3 Section 5: received unit cost = 0 is a legitimate case (a
    supplier bonus/free sample) — not blocked, and it correctly pulls the
    blended WAC down."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store, MANAGER, username="recv_mgr_zero_cost")
    db.commit()
    headers = auth_headers(client, "recv_mgr_zero_cost", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(
        client, headers, store, supplier, product, quantity="10", unit_cost="0.00"
    )
    item_id = po["items"][0]["id"]

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "0.00"}
            ],
        },
    )
    assert response.status_code == 201
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10")
    assert product.current_cost == Decimal("0.000000")


def test_inventory_clerk_can_receive_but_cashier_cannot(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="recv_mgr_setup")
    make_user_with_role(db, store, INVENTORY_CLERK, username="recv_clerk_1")
    make_user_with_role(db, store, CASHIER, username="recv_cashier_1")
    db.commit()
    setup_headers = auth_headers(client, "recv_mgr_setup", DEFAULT_TEST_PASSWORD)
    po = _create_and_submit_po(client, setup_headers, store, supplier, product)
    item_id = po["items"][0]["id"]

    cashier_headers = auth_headers(client, "recv_cashier_1", DEFAULT_TEST_PASSWORD)
    cashier_response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=cashier_headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "1", "unit_cost": "5.00"}
            ],
        },
    )
    assert cashier_response.status_code == 403

    clerk_headers = auth_headers(client, "recv_clerk_1", DEFAULT_TEST_PASSWORD)
    clerk_response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=clerk_headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "1", "unit_cost": "5.00"}
            ],
        },
    )
    assert clerk_response.status_code == 201


# --- Purchase returns ---------------------------------------------------


def test_purchase_return_reduces_stock_without_changing_wac(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="ret_mgr_1")
    db.commit()
    headers = auth_headers(client, "ret_mgr_1", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(
        client, headers, store, supplier, product, quantity="10", unit_cost="6.00"
    )
    item_id = po["items"][0]["id"]
    client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "10", "unit_cost": "6.00"}
            ],
        },
    )
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10")
    assert product.current_cost == Decimal("6.000000")

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/returns",
        headers=headers,
        json={
            "store_id": store.id,
            "return_date": "2024-01-06",
            "reason": "Damaged",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity": "3"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["items"][0]["unit_cost"] == "6.000000"

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("7")
    assert product.current_cost == Decimal("6.000000")  # unchanged by a return


def test_purchase_return_cannot_exceed_current_stock(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"))
    make_user_with_role(db, store, MANAGER, username="ret_mgr_2")
    db.commit()
    headers = auth_headers(client, "ret_mgr_2", DEFAULT_TEST_PASSWORD)

    po = _create_and_submit_po(client, headers, store, supplier, product, quantity="2")

    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/returns",
        headers=headers,
        json={
            "store_id": store.id,
            "return_date": "2024-01-06",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity": "5"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_STOCK"


# --- Multi-store isolation (M2 hardening audit Section 12, extended) -----


def test_store_scoped_user_cannot_create_po_for_another_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_b = make_product(db, store_b)
    make_user_with_role(db, store_a, MANAGER, username="iso_po_mgr_1")
    db.commit()
    headers = auth_headers(client, "iso_po_mgr_1", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store_b, supplier, product_b),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_store_scoped_user_cannot_read_another_stores_po(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_b = make_product(db, store_b)
    make_user_with_role(db, store_b, MANAGER, username="iso_po_owner")
    make_user_with_role(db, store_a, MANAGER, username="iso_po_intruder")
    db.commit()

    owner_headers = auth_headers(client, "iso_po_owner", DEFAULT_TEST_PASSWORD)
    po_id = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=owner_headers,
        json=_po_payload(store_b, supplier, product_b),
    ).json()["id"]

    intruder_headers = auth_headers(client, "iso_po_intruder", DEFAULT_TEST_PASSWORD)
    response = client.get(f"/api/v1/purchasing/purchase-orders/{po_id}", headers=intruder_headers)
    assert response.status_code == 404


def test_store_scoped_user_cannot_receive_against_another_stores_po(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_b = make_product(db, store_b, current_qty_on_hand=Decimal("0"))
    make_user_with_role(db, store_b, MANAGER, username="iso_recv_owner")
    make_user_with_role(db, store_a, MANAGER, username="iso_recv_intruder")
    db.commit()

    owner_headers = auth_headers(client, "iso_recv_owner", DEFAULT_TEST_PASSWORD)
    po = _create_and_submit_po(client, owner_headers, store_b, supplier, product_b)
    item_id = po["items"][0]["id"]

    intruder_headers = auth_headers(client, "iso_recv_intruder", DEFAULT_TEST_PASSWORD)
    response = client.post(
        f"/api/v1/purchasing/purchase-orders/{po['id']}/receive",
        headers=intruder_headers,
        json={
            "received_date": "2024-01-05",
            "client_transaction_id": f"txn-{unique_suffix()}",
            "lines": [
                {"purchase_order_item_id": item_id, "quantity_received": "1", "unit_cost": "5.00"}
            ],
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"
    db.refresh(product_b)
    assert product_b.current_qty_on_hand == Decimal("0")


def test_cross_store_manager_without_store_assignment_can_operate_anywhere(
    client: TestClient, db: Session
) -> None:
    """Positive control (M2 hardening audit's pattern, extended to M3): a
    Manager with no single store_id assignment is a deliberate
    cross-store role, not a gap."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    make_user_with_role(db, None, MANAGER, username="iso_cross_mgr")
    db.commit()
    headers = auth_headers(client, "iso_cross_mgr", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers,
        json=_po_payload(store, supplier, product),
    )
    assert response.status_code == 201


def test_store_scoped_user_only_lists_their_own_stores_purchase_orders(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_a = make_product(db, store_a)
    product_b = make_product(db, store_b)
    make_user_with_role(db, store_a, MANAGER, username="iso_list_a")
    make_user_with_role(db, store_b, MANAGER, username="iso_list_b")
    db.commit()

    headers_a = auth_headers(client, "iso_list_a", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "iso_list_b", DEFAULT_TEST_PASSWORD)
    client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers_a,
        json=_po_payload(store_a, supplier, product_a),
    )
    client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers_b,
        json=_po_payload(store_b, supplier, product_b),
    )

    listing = client.get("/api/v1/purchasing/purchase-orders", headers=headers_a).json()
    assert all(po["store_id"] == store_a.id for po in listing)
