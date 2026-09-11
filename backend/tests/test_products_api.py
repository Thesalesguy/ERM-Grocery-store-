"""Product catalog API: CRUD, validation, barcode lookup, and the
price-change-does-not-touch-history regression test."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
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


def test_create_and_get_product(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="admin_p1")
    db.commit()
    headers = auth_headers(client, "admin_p1", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/products",
        headers=headers,
        json={
            "store_id": store.id,
            "sku": "API-SKU-1",
            "name": "API Product",
            "current_price": "12.50",
        },
    )
    assert create_response.status_code == 201
    product_id = create_response.json()["id"]

    get_response = client.get(f"/api/v1/products/{product_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["sku"] == "API-SKU-1"
    assert get_response.json()["current_cost"] == "0.000000"


def test_duplicate_sku_returns_409(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="admin_p2")
    db.commit()
    headers = auth_headers(client, "admin_p2", DEFAULT_TEST_PASSWORD)
    payload = {"store_id": store.id, "sku": "DUP", "name": "A", "current_price": "1.00"}

    assert client.post("/api/v1/products", headers=headers, json=payload).status_code == 201
    response = client.post("/api/v1/products", headers=headers, json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_SKU"


def test_invalid_category_reference_returns_422(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="admin_p3")
    db.commit()
    headers = auth_headers(client, "admin_p3", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/products",
        headers=headers,
        json={
            "store_id": store.id,
            "sku": "BADCAT",
            "name": "A",
            "current_price": "1.00",
            "category_id": 999999,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CATEGORY"


def test_negative_price_rejected_by_schema(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="admin_p4")
    db.commit()
    headers = auth_headers(client, "admin_p4", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/products",
        headers=headers,
        json={"store_id": store.id, "sku": "NEG", "name": "A", "current_price": "-5.00"},
    )
    assert response.status_code == 422


def test_search_products_by_name(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="mgr_p1")
    make_product(db, store, sku="FIND-1", name="Findable Widget")
    make_product(db, store, sku="FIND-2", name="Unrelated Gadget")
    db.commit()
    headers = auth_headers(client, "mgr_p1", DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/products?store_id={store.id}&search=Findable", headers=headers)
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert names == ["Findable Widget"]


def test_barcode_lookup(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, sku="BC-1", name="Barcoded")
    make_user_with_role(db, store, MANAGER, username="mgr_p2")
    db.commit()
    headers = auth_headers(client, "mgr_p2", DEFAULT_TEST_PASSWORD)

    add_response = client.post(
        f"/api/v1/products/{product.id}/barcodes",
        headers=headers,
        json={"barcode": "9999999999999", "is_primary": True},
    )
    assert add_response.status_code == 201

    lookup_response = client.get("/api/v1/products/barcode/9999999999999", headers=headers)
    assert lookup_response.status_code == 200
    assert lookup_response.json()["id"] == product.id


def test_unknown_barcode_returns_404(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="mgr_p3")
    db.commit()
    headers = auth_headers(client, "mgr_p3", DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/products/barcode/0000000000000", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "UNKNOWN_BARCODE"


def test_duplicate_barcode_returns_409(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product_a = make_product(db, store)
    product_b = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="mgr_p4")
    db.commit()
    headers = auth_headers(client, "mgr_p4", DEFAULT_TEST_PASSWORD)

    client.post(
        f"/api/v1/products/{product_a.id}/barcodes",
        headers=headers,
        json={"barcode": "1112223334445"},
    )
    response = client.post(
        f"/api/v1/products/{product_b.id}/barcodes",
        headers=headers,
        json={"barcode": "1112223334445"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_BARCODE"


def test_deactivate_then_reactivate_product(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    make_user_with_role(db, store, MANAGER, username="mgr_p5")
    db.commit()
    headers = auth_headers(client, "mgr_p5", DEFAULT_TEST_PASSWORD)

    deactivate = client.post(f"/api/v1/products/{product.id}/deactivate", headers=headers)
    assert deactivate.status_code == 200
    assert deactivate.json()["is_active"] is False

    listing = client.get(f"/api/v1/products?store_id={store.id}&is_active=false", headers=headers)
    assert product.id in [p["id"] for p in listing.json()]

    reactivate = client.post(f"/api/v1/products/{product.id}/activate", headers=headers)
    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True


def test_price_change_does_not_affect_historical_sale(client: TestClient, db: Session) -> None:
    """M2 task Section 5: a later product edit must not alter historical
    sales — proven end to end through the real update endpoint and the
    real sale-finalization service."""
    store = make_store(db)
    supplier = make_supplier(db)
    cashier = make_user_with_role(db, store, CASHIER, username="cashier_hist")
    make_user_with_role(db, store, MANAGER, username="mgr_hist")
    product = make_product(db, store, current_price=Decimal("10.00"))
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date.today(),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("4.00"))],
    )
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    original_unit_price = sale.items[0].unit_price_at_sale
    assert original_unit_price == Decimal("10.00")

    headers = auth_headers(client, "mgr_hist", DEFAULT_TEST_PASSWORD)
    update_response = client.put(
        f"/api/v1/products/{product.id}", headers=headers, json={"current_price": "99.99"}
    )
    assert update_response.status_code == 200
    assert update_response.json()["current_price"] == "99.99"

    db.refresh(sale)
    db.refresh(sale.items[0])
    assert sale.items[0].unit_price_at_sale == original_unit_price == Decimal("10.00")
