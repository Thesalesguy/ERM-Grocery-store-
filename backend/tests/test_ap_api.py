"""M6 Session J: AP API/security tests — RBAC, multi-store isolation
(including a mutation test proving the store check is real), idempotency
at the HTTP boundary, and adversarial input handling.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.v1.endpoints.ap as ap_endpoint
from app.modules.ap import service as ap_service
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.auth.permissions import AUDITOR, CASHIER, INVENTORY_CLERK, MANAGER
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _receive(db: Session, store, supplier, product, *, qty="10", cost="5.00"):
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal(qty),
        unit_cost=Decimal(cost),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal(qty), Decimal(cost))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    return po, item


def _invoice_payload(store_id, supplier_id, po_id, item_id, *, qty="10", price="5.00", number=None):
    return {
        "store_id": store_id,
        "supplier_id": supplier_id,
        "purchase_order_id": po_id,
        "invoice_number": number or f"INV-{unique_suffix()}",
        "invoice_date": "2024-01-05",
        "client_transaction_id": f"itxn-{unique_suffix()}",
        "lines": [
            {"purchase_order_item_id": item_id, "quantity_invoiced": qty, "unit_price": price}
        ],
    }


# --- RBAC ---------------------------------------------------------------


def test_cashier_has_no_ap_access(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    assert client.get("/api/v1/ap/invoices", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/v1/ap/invoices",
            json=_invoice_payload(store.id, 1, 1, 1),
            headers=headers,
        ).status_code
        == 403
    )


def test_inventory_clerk_can_write_but_not_post_or_pay(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"clerk_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/ap/invoices",
        json=_invoice_payload(store.id, supplier.id, po.id, item.id),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    invoice_id = response.json()["id"]

    assert client.post(f"/api/v1/ap/invoices/{invoice_id}/post", headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/v1/ap/invoices/{invoice_id}/payments",
            json={
                "store_id": store.id,
                "payment_date": "2024-01-10",
                "payment_method": "CASH",
                "amount": "10.00",
                "client_transaction_id": f"ptxn-{unique_suffix()}",
            },
            headers=headers,
        ).status_code
        == 403
    )


def test_manager_can_create_post_and_pay(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/ap/invoices",
        json=_invoice_payload(store.id, supplier.id, po.id, item.id),
        headers=headers,
    )
    assert response.status_code == 201
    invoice_id = response.json()["id"]

    response = client.post(f"/api/v1/ap/invoices/{invoice_id}/post", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "POSTED"

    response = client.post(
        f"/api/v1/ap/invoices/{invoice_id}/payments",
        json={
            "store_id": store.id,
            "payment_date": "2024-01-10",
            "payment_method": "CASH",
            "amount": "50.00",
            "client_transaction_id": f"ptxn-{unique_suffix()}",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_auditor_can_read_but_not_write(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"auditor_{unique_suffix()}"
    make_user_with_role(db, store, AUDITOR, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    assert client.get("/api/v1/ap/invoices", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/v1/ap/invoices", json=_invoice_payload(store.id, 1, 1, 1), headers=headers
        ).status_code
        == 403
    )


# --- Store isolation ------------------------------------------------------


def test_cross_store_invoice_creation_is_rejected(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store_b)
    username = f"manager_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store_b, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/ap/invoices",
        json=_invoice_payload(store_b.id, supplier.id, po.id, item.id),
        headers=headers,
    )
    assert response.status_code == 403


def test_cross_store_invoice_lookup_returns_404_not_leaked(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store_b)
    username_b = f"manager_b_{unique_suffix()}"
    make_user_with_role(db, store_b, MANAGER, username=username_b)
    po, item = _receive(db, store_b, supplier, product)
    db.commit()
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store_b.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    username_a = f"manager_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username_a)
    db.commit()
    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/invoices/{invoice.id}", headers=headers_a)
    assert response.status_code == 404


def test_store_isolation_mutation_test_removing_enforce_store_access(
    client: TestClient, db: Session, monkeypatch
) -> None:
    """Mutation test: disable the ROUTE-layer enforce_store_access call
    and confirm the request STILL gets 403 — the SERVICE-layer check
    inside create_purchase_invoice is an independent second line of
    defense, matching the M5 precedent
    (test_sales_returns_api.py::test_store_isolation_mutation_test_removing_enforce_store_access).
    """
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store_b)
    username = f"manager_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store_b, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(ap_endpoint, "enforce_store_access", lambda *a, **kw: None)

    response = client.post(
        "/api/v1/ap/invoices",
        json=_invoice_payload(store_b.id, supplier.id, po.id, item.id),
        headers=headers,
    )
    assert response.status_code == 403


# --- Idempotency at the HTTP boundary --------------------------------------


def test_identical_retry_returns_the_same_invoice(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = _invoice_payload(store.id, supplier.id, po.id, item.id)
    first = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    second = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_conflicting_payload_with_same_idempotency_key_is_rejected(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = _invoice_payload(store.id, supplier.id, po.id, item.id)
    client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    payload2 = dict(payload)
    payload2["lines"] = [
        {"purchase_order_item_id": item.id, "quantity_invoiced": "9", "unit_price": "5.00"}
    ]
    payload2["client_transaction_id"] = payload["client_transaction_id"]
    response = client.post("/api/v1/ap/invoices", json=payload2, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


# --- Adversarial inputs -----------------------------------------------------


def test_over_invoicing_rejected_via_api(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty="10", cost="5.00")
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = _invoice_payload(store.id, supplier.id, po.id, item.id, qty="20", price="5.00")
    response = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    invoice_id = response.json()["id"]
    post_response = client.post(f"/api/v1/ap/invoices/{invoice_id}/post", headers=headers)
    assert post_response.status_code == 409
    assert post_response.json()["error"]["code"] == "OVER_INVOICING"


def test_tampered_purchase_order_item_id_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = _invoice_payload(store.id, supplier.id, po.id, 999999999)
    response = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    assert response.status_code == 404


def test_negative_and_zero_quantity_rejected_by_schema(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    for bad_qty in ("-1", "0"):
        payload = _invoice_payload(store.id, supplier.id, po.id, item.id, qty=bad_qty)
        response = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
        assert response.status_code == 422


def test_malformed_invoice_id_is_handled_cleanly(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    response = client.get("/api/v1/ap/invoices/not-a-number", headers=headers)
    assert response.status_code == 422


def test_sql_injection_attempt_in_query_param_is_inert(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    response = client.get("/api/v1/ap/invoices?status=' OR '1'='1", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_oversized_invoice_line_payload_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    po, item = _receive(db, store, supplier, product)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = _invoice_payload(store.id, supplier.id, po.id, item.id)
    payload["lines"] = [
        {"purchase_order_item_id": item.id, "quantity_invoiced": "1", "unit_price": "1.00"}
        for _ in range(501)
    ]
    response = client.post("/api/v1/ap/invoices", json=payload, headers=headers)
    assert response.status_code == 422
