"""Pre-M15 hardening (docs/M15_DESIGN.md "Pre-M15 hardening"): stock
adjustments must be safe against retries, mirroring the idempotency
coverage every other financial-posting flow already has (see
tests/test_purchasing_idempotency.py for the sequential-retry pattern this
file follows, and tests/test_purchasing_concurrency.py for the genuinely-
concurrent-duplicate pattern this file's last test follows).
"""

import threading
from dataclasses import dataclass
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.accounting.models import JournalEntry
from app.modules.auth.permissions import INVENTORY_CLERK
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import InventoryMovement, StockAdjustment
from app.modules.products.models import Product
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def test_exact_duplicate_adjustment_request_returns_the_same_adjustment(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    username = f"idem_adj_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    key = f"txn-{unique_suffix()}"
    payload = {
        "product_id": product.id,
        "quantity_delta": "-2",
        "reason_code": "DAMAGE",
        "notes": "broken in transit",
        "client_transaction_id": key,
    }

    first = client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    assert first.status_code == 201
    second = client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


def test_duplicate_key_does_not_double_post_movement_or_journal_entry(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_qty_on_hand=Decimal("10"), current_cost=Decimal("3.00")
    )
    username = f"idem_adj_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    key = f"txn-{unique_suffix()}"
    payload = {
        "product_id": product.id,
        "quantity_delta": "-2",
        "reason_code": "THEFT",
        "notes": None,
        "client_transaction_id": key,
    }

    client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)

    adjustment_count = (
        db.execute(select(StockAdjustment).where(StockAdjustment.client_transaction_id == key))
        .scalars()
        .all()
    )
    assert len(adjustment_count) == 1

    movement_count = (
        db.execute(
            select(InventoryMovement).where(
                InventoryMovement.reference_type == "stock_adjustment",
                InventoryMovement.reference_id == adjustment_count[0].id,
            )
        )
        .scalars()
        .all()
    )
    assert len(movement_count) == 1

    journal_count = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "STOCK_ADJUSTMENT",
                JournalEntry.source_id == adjustment_count[0].id,
            )
        )
        .scalars()
        .all()
    )
    assert len(journal_count) == 1

    db.refresh(product)
    # Only ONE adjustment's worth of quantity applied (10 - 2 = 8), not
    # three times over.
    assert product.current_qty_on_hand == Decimal("8.000")


def test_omitted_client_transaction_id_preserves_pre_m15_behavior(
    client: TestClient, db: Session
) -> None:
    """A caller that never supplies a key (every pre-M15 caller, and every
    internal call from post_stock_count) gets exactly the old behavior:
    each call creates its own adjustment, no idempotency check at all."""
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    username = f"idem_adj_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payload = {
        "product_id": product.id,
        "quantity_delta": "-1",
        "reason_code": "OTHER",
        "notes": None,
    }
    first = client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    second = client.post("/api/v1/inventory/adjustments", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("8.000")


def test_service_layer_retry_with_same_key_returns_original(db: Session) -> None:
    """Direct service-layer proof (not just via the HTTP endpoint) that a
    retried create_stock_adjustment call with the same key is a no-op."""
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("5"))
    username = f"idem_adj_{unique_suffix()}"
    user = make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()

    key = f"txn-{unique_suffix()}"
    first = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-1"),
        reason_code="EXPIRY",
        notes=None,
        created_by=user.id,
        client_transaction_id=key,
    )
    db.commit()
    second = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-1"),
        reason_code="EXPIRY",
        notes=None,
        created_by=user.id,
        client_transaction_id=key,
    )
    assert second.id == first.id

    count = (
        db.execute(select(StockAdjustment).where(StockAdjustment.client_transaction_id == key))
        .scalars()
        .all()
    )
    assert len(count) == 1


# --- Concurrency (genuinely independent connections, real PostgreSQL) -----
#
# Mirrors tests/test_purchasing_concurrency.py's
# test_f_concurrent_duplicate_receipt_requests_create_only_one_receipt
# exactly — the `db` fixture's savepoint isolation would make one thread's
# setup invisible to another thread's connection, so this uses SessionLocal
# directly like that file does.


@dataclass
class _AdjustmentOutcome:
    succeeded: bool = False
    error: str | None = None
    adjustment_id: int | None = None


def _attempt_adjustment(
    *,
    store_id: int,
    product_id: int,
    created_by: int,
    client_transaction_id: str,
    barrier: threading.Barrier,
    result: _AdjustmentOutcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        adjustment = inventory_service.create_stock_adjustment(
            session,
            store_id=store_id,
            product_id=product_id,
            quantity_delta=Decimal("-1"),
            reason_code="DAMAGE",
            notes=None,
            created_by=created_by,
            client_transaction_id=client_transaction_id,
        )
        session.commit()
        result.succeeded = True
        result.adjustment_id = adjustment.id
    except Exception as exc:  # noqa: BLE001 - captured for the assertion below
        session.rollback()
        result.error = repr(exc)
    finally:
        session.close()


def test_concurrent_duplicate_adjustment_requests_create_only_one_adjustment() -> None:
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("20"))
        username = f"idem_adj_{unique_suffix()}"
        user = make_user_with_role(setup_session, store, INVENTORY_CLERK, username=username)
        setup_session.commit()
        store_id, product_id, created_by = store.id, product.id, user.id
    finally:
        setup_session.close()

    shared_key = f"txn-{unique_suffix()}"
    barrier = threading.Barrier(2)
    result_a, result_b = _AdjustmentOutcome(), _AdjustmentOutcome()
    thread_a = threading.Thread(
        target=_attempt_adjustment,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            created_by=created_by,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_adjustment,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            created_by=created_by,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.error is None, result_a
    assert result_b.error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.adjustment_id == result_b.adjustment_id

    verify_session = SessionLocal()
    try:
        count = (
            verify_session.execute(
                select(StockAdjustment).where(StockAdjustment.client_transaction_id == shared_key)
            )
            .scalars()
            .all()
        )
        assert len(count) == 1

        product = verify_session.get(Product, product_id)
        # Only ONE adjustment's worth of quantity (-1), not -2.
        assert product.current_qty_on_hand == Decimal("19.000")
    finally:
        verify_session.close()
