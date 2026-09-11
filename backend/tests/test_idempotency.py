"""M2 hardening audit Section 7: a retried checkout request (double-click,
network retry after a dropped response) must produce ONE committed sale,
never two — and a failed attempt must not "poison" its idempotency key
against a later, corrected retry.

See app.modules.sales.service.finalize_sale's docstring for the mechanism:
an early SELECT-by-client_transaction_id fast path for the common
sequential-retry case, backed by a UNIQUE database constraint (enforced
even under genuine concurrency) as the real guarantee.
"""

import threading
from dataclasses import dataclass
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.auth.permissions import CASHIER
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def test_exact_duplicate_http_request_returns_the_same_sale(
    client: TestClient, db: Session
) -> None:
    """The common real-world case: a cashier's browser sends the same
    checkout request twice (double-click, or a retry after the first
    response was lost) — both requests carry the same client-generated
    client_transaction_id. The second must return the SAME sale, not
    create a second one."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("50")
    )
    make_user_with_role(db, store, CASHIER, username="dup_cashier")
    db.commit()
    headers = auth_headers(client, "dup_cashier", DEFAULT_TEST_PASSWORD)

    key = f"txn-{unique_suffix()}"
    payload = {
        "store_id": store.id,
        "client_transaction_id": key,
        "lines": [{"product_id": product.id, "quantity": "1"}],
        "payments": [{"payment_method": "CASH", "amount": "10.00"}],
    }

    first = client.post("/api/v1/sales", headers=headers, json=payload)
    assert first.status_code == 201
    first_body = first.json()

    second = client.post("/api/v1/sales", headers=headers, json=payload)
    assert second.status_code == 201
    second_body = second.json()

    assert second_body["id"] == first_body["id"]
    assert second_body["sale_number"] == first_body["sale_number"]

    # Only one row actually exists — the second request did not insert
    # anything.
    count = db.execute(
        select(func.count()).select_from(Sale).where(Sale.client_transaction_id == key)
    ).scalar_one()
    assert count == 1

    # Stock was only decremented once (10 -> ... -> would be 48 if double-
    # charged; must still be 49).
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("49")


def test_retry_after_business_failure_is_a_fresh_attempt(client: TestClient, db: Session) -> None:
    """A failed attempt (e.g. insufficient stock) creates nothing, so its
    client_transaction_id must NOT block a later, corrected retry that
    reuses the same key — that is still "one intended sale", just fixed
    up before it succeeds."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("1")
    )
    make_user_with_role(db, store, CASHIER, username="retry_cashier")
    db.commit()
    headers = auth_headers(client, "retry_cashier", DEFAULT_TEST_PASSWORD)

    key = f"txn-{unique_suffix()}"
    failing_payload = {
        "store_id": store.id,
        "client_transaction_id": key,
        "lines": [{"product_id": product.id, "quantity": "5"}],  # more than the 1 in stock
        "payments": [{"payment_method": "CASH", "amount": "50.00"}],
    }
    first = client.post("/api/v1/sales", headers=headers, json=failing_payload)
    assert first.status_code == 409
    assert first.json()["error"]["code"] == "INSUFFICIENT_STOCK"

    corrected_payload = {
        "store_id": store.id,
        "client_transaction_id": key,  # SAME key as the failed attempt
        "lines": [{"product_id": product.id, "quantity": "1"}],  # corrected to what's in stock
        "payments": [{"payment_method": "CASH", "amount": "10.00"}],
    }
    second = client.post("/api/v1/sales", headers=headers, json=corrected_payload)
    assert second.status_code == 201
    assert second.json()["items"][0]["quantity"] == "1.000"

    count = db.execute(
        select(func.count()).select_from(Sale).where(Sale.client_transaction_id == key)
    ).scalar_one()
    assert count == 1


@dataclass
class _Outcome:
    sale_id: int | None = None
    error: str | None = None


def test_concurrent_duplicate_submission_creates_only_one_sale() -> None:
    """The narrow race the early SELECT fast path can't catch on its own:
    two threads submit with the SAME client_transaction_id at (as close
    as threading allows to) the same instant. Plenty of stock for both,
    so this isolates idempotency from stock contention (that's
    tests/test_concurrency.py's job) — the only thing under test here is
    whether two Sale rows can ever result from one logical attempt.
    """
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("50")
        )
        setup_session.commit()
        store_id, cashier_id, product_id = store.id, cashier.id, product.id
    finally:
        setup_session.close()

    shared_key = f"txn-{unique_suffix()}"
    barrier = threading.Barrier(2)
    outcome_a, outcome_b = _Outcome(), _Outcome()

    def _attempt(outcome: _Outcome) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            sale = sales_service.finalize_sale(
                session,
                store_id=store_id,
                cashier_id=cashier_id,
                client_transaction_id=shared_key,
                caller_store_id=None,
                lines=[SaleLineInput(product_id=product_id, quantity=Decimal("1"))],
                payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
            )
            session.commit()
            outcome.sale_id = sale.id
        except Exception as exc:  # noqa: BLE001 - want to see anything unexpected
            session.rollback()
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            session.close()

    thread_a = threading.Thread(target=_attempt, args=(outcome_a,))
    thread_b = threading.Thread(target=_attempt, args=(outcome_b,))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert outcome_a.error is None, outcome_a
    assert outcome_b.error is None, outcome_b
    # Both calls "succeed" from the caller's point of view — one by
    # actually creating the sale, the other by idempotent replay — and
    # they must agree on which sale that was.
    assert outcome_a.sale_id is not None
    assert outcome_a.sale_id == outcome_b.sale_id

    verify_session = SessionLocal()
    try:
        from app.modules.inventory.models import InventoryMovement
        from app.modules.products.models import Product
        from app.modules.sales.models import Payment, SaleItem

        count = verify_session.execute(
            select(func.count()).select_from(Sale).where(Sale.client_transaction_id == shared_key)
        ).scalar_one()
        assert count == 1, "two Sale rows were created for one client_transaction_id"

        product = verify_session.get(Product, product_id)
        # Exactly one unit sold, not two — a lost-idempotency bug would
        # double-charge inventory even if it somehow avoided a duplicate
        # Sale row.
        assert product.current_qty_on_hand == Decimal("49")

        items = (
            verify_session.execute(select(SaleItem).where(SaleItem.sale_id == outcome_a.sale_id))
            .scalars()
            .all()
        )
        assert len(items) == 1
        payments = (
            verify_session.execute(select(Payment).where(Payment.sale_id == outcome_a.sale_id))
            .scalars()
            .all()
        )
        assert len(payments) == 1
        movements = (
            verify_session.execute(
                select(InventoryMovement).where(
                    InventoryMovement.reference_type == "sale",
                    InventoryMovement.reference_id == outcome_a.sale_id,
                )
            )
            .scalars()
            .all()
        )
        assert len(movements) == 1
    finally:
        verify_session.close()
