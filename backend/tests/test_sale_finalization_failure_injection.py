"""M12 Phase 12: error handling and incident safety.

finalize_sale is the single highest-volume, money-and-inventory-moving
mutation in the whole system, and -- unlike returns/voids
(test_sales_returns_failure_injection.py), stock counts/transfers
(test_m8_failure_injection.py), AP (test_ap_failure_injection.py), and
payroll posting (test_payroll_hardening.py) -- it had never been
failure-injection tested. This closes that gap: a forced failure late
in the transaction (accounting posting, after the sale/items/payments/
movements/audit row are all flushed but not committed) must roll back
EVERYTHING, the API must surface a safe generic error with no internal
leak, and a client retry with the same idempotency key afterward must
succeed cleanly rather than being blocked by any partial state left
behind.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import CASHIER
from app.modules.inventory.models import InventoryMovement
from app.modules.sales import service as sales_service
from app.modules.sales.models import Payment, Sale, SaleItem
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import DEFAULT_TEST_PASSWORD, make_product, make_store, make_user_with_role
from tests.helpers import auth_headers


def _no_raise_client(client: TestClient) -> TestClient:
    """See tests/test_reports_failure_injection.py's identical helper: a
    second TestClient wrapping the same `app` (and therefore the same
    `get_db` override the `client` fixture already registered), with
    raise_server_exceptions=False so the global exception handler's
    response can actually be observed."""
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_failure_during_sale_accounting_posting_leaves_no_partial_state(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username="finalize_fail_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()

    # erp_dev is a long-lived, shared dev database with committed rows
    # from many prior test runs -- so "the table is empty" is never a
    # safe assertion here. Scoping by this test's own unique product_id
    # (SaleItem, InventoryMovement) is airtight; for tables with no
    # product_id (Payment, AuditLog), a before/after row-count comparison
    # is used instead, which is equally airtight regardless of what else
    # already exists in the table.
    sale_item_count_before = db.execute(select(func.count()).select_from(SaleItem)).scalar_one()
    payment_count_before = db.execute(select(func.count()).select_from(Payment)).scalar_one()
    audit_count_before = db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "SALE_COMPLETED")
    ).scalar_one()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure inside sale accounting posting, before commit")

    monkeypatch.setattr(accounting_service, "post_sale_journal", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id="finalize-fail-1",
            caller_store_id=store.id,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
    db.rollback()

    assert (
        db.execute(select(Sale).where(Sale.client_transaction_id == "finalize-fail-1")).first()
        is None
    )
    assert db.execute(select(SaleItem).where(SaleItem.product_id == product.id)).first() is None
    assert (
        db.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == product.id)
        ).first()
        is None
    )
    assert (
        db.execute(select(func.count()).select_from(SaleItem)).scalar_one()
        == sale_item_count_before
    )
    assert (
        db.execute(select(func.count()).select_from(Payment)).scalar_one() == payment_count_before
    )
    assert (
        db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "SALE_COMPLETED")
        ).scalar_one()
        == audit_count_before
    )

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("5")  # never touched


def test_retry_with_the_same_idempotency_key_after_a_failed_attempt_succeeds_cleanly(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failed attempt above must not leave any state that blocks a
    clean, normal retry with the SAME client_transaction_id -- exactly
    what a POS terminal does after a request that never got a response
    (network error, timeout, or a real transient failure)."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username="finalize_retry_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(accounting_service, "post_sale_journal", _boom)
    with pytest.raises(RuntimeError):
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id="finalize-retry-1",
            caller_store_id=store.id,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
    db.rollback()
    db.refresh(product)

    monkeypatch.undo()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id="finalize-retry-1",
        caller_store_id=store.id,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()

    assert sale.client_transaction_id == "finalize-retry-1"
    matching_sales = (
        db.execute(select(Sale).where(Sale.client_transaction_id == "finalize-retry-1"))
        .scalars()
        .all()
    )
    assert len(matching_sales) == 1  # exactly one sale, not zero, not two
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("4")  # exactly one unit sold


def test_forced_sale_failure_returns_a_safe_generic_error_via_the_api(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="finalize_api_fail_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()
    headers = auth_headers(client, "finalize_api_fail_cashier", DEFAULT_TEST_PASSWORD)
    raw_client = _no_raise_client(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure: connection reset by peer at db.example.internal")

    monkeypatch.setattr(accounting_service, "post_sale_journal", _boom)

    response = raw_client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": "finalize-api-fail-1",
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "10.00"}],
        },
    )
    assert response.status_code == 500
    body = response.json()
    assert body == {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}}
    assert "db.example.internal" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_sale_failure_does_not_poison_a_later_unrelated_request(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="finalize_poison_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()
    headers = auth_headers(client, "finalize_poison_cashier", DEFAULT_TEST_PASSWORD)
    raw_client = _no_raise_client(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(accounting_service, "post_sale_journal", _boom)
    failing = raw_client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": "finalize-poison-1",
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "10.00"}],
        },
    )
    assert failing.status_code == 500

    monkeypatch.undo()
    healthy = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": "finalize-poison-2",
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": "10.00"}],
        },
    )
    assert healthy.status_code == 201
