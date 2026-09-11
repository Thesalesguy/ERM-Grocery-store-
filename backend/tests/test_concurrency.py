"""The concurrency proof: two simultaneous sale-finalization attempts
against a single unit of stock must never both succeed, and must never
leave the ledger inconsistent.

This deliberately does NOT use the `db` fixture (tests/conftest.py) — that
fixture wraps each test in one outer transaction on ONE connection with a
SAVEPOINT-based inner commit, so a second, independent connection would
never see its setup data at all (nothing is really committed until the
fixture's rollback, which never lets it become visible). Proving real
concurrency requires two genuinely independent database connections/
transactions, each doing what a real concurrent HTTP request would do:
open its own session, lock, check, write, commit. So this file talks to
`app.db.session.SessionLocal` directly and leaves its (small, uniquely
named) committed rows in place rather than rolling them back — the
committed state IS the evidence.
"""

import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.inventory.models import InventoryMovement
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_purchase_order, make_store, make_supplier, make_user


@dataclass
class _AttemptResult:
    succeeded: bool = False
    error_code: str | None = None
    sale_id: int | None = None


def _attempt_one_unit_sale(
    *,
    store_id: int,
    product_id: int,
    cashier_id: int,
    barrier: threading.Barrier,
    result: _AttemptResult,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)  # both threads start their DB work together
        sale = sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            lines=[SaleLineInput(product_id=product_id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("100.00"))],
        )
        session.commit()
        result.succeeded = True
        result.sale_id = sale.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    finally:
        session.close()


def test_two_concurrent_sales_against_one_unit_of_stock_cannot_both_succeed() -> None:
    # --- Setup: a product with exactly 1 unit in stock, via a real,
    # committed session (not the rollback-isolated `db` fixture) so the
    # two worker threads' own independent connections can see it.
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(setup_session, store, current_price=Decimal("100.00"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        from app.modules.purchasing.models import PurchaseOrderItem

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("1"),
            unit_cost=Decimal("40.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        purchasing_service.receive_goods(
            setup_session,
            purchase_order_id=po.id,
            received_date=date.today(),
            lines=[GoodsReceiptLineInput(item.id, Decimal("1"), Decimal("40.00"))],
        )
        setup_session.commit()
        setup_session.refresh(product)
        assert product.current_qty_on_hand == Decimal("1")

        store_id, product_id, cashier_id = store.id, product.id, cashier.id
    finally:
        setup_session.close()

    # --- Two threads race to sell the same single unit.
    barrier = threading.Barrier(2)
    result_a = _AttemptResult()
    result_b = _AttemptResult()
    thread_a = threading.Thread(
        target=_attempt_one_unit_sale,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            cashier_id=cashier_id,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_one_unit_sale,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            cashier_id=cashier_id,
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    outcomes = [result_a, result_b]
    successes = [r for r in outcomes if r.succeeded]
    failures = [r for r in outcomes if not r.succeeded]

    # --- The core invariant: exactly one wins, exactly one is rejected,
    # and the rejection is specifically INSUFFICIENT_STOCK — not a
    # deadlock error, not a generic 500, not a lost update.
    assert len(successes) == 1, f"expected exactly one success, got {outcomes}"
    assert len(failures) == 1, f"expected exactly one failure, got {outcomes}"
    assert failures[0].error_code == "INSUFFICIENT_STOCK"

    # --- No oversell: stock lands at exactly 0, never negative.
    verify_session = SessionLocal()
    try:
        verify_session.expire_all()
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        assert product is not None
        assert product.current_qty_on_hand == Decimal("0")

        # --- No orphans: exactly one SALE movement, one completed sale,
        # one sale_item, one payment — not two, not zero.
        movements = (
            verify_session.query(InventoryMovement)
            .filter_by(product_id=product_id, movement_type="SALE")
            .all()
        )
        assert len(movements) == 1, f"expected exactly one SALE movement, got {len(movements)}"

        sale = verify_session.get(Sale, successes[0].sale_id)
        assert sale is not None
        assert sale.status == "COMPLETED"
        assert len(sale.items) == 1
        assert len(sale.payments) == 1
        assert sale.items[0].quantity == Decimal("1")
    finally:
        verify_session.close()


def test_five_concurrent_sales_against_one_unit_only_one_succeeds() -> None:
    """Same invariant, higher contention: 5 simultaneous attempts at 1
    unit each against a single unit of stock — still exactly one winner,
    stock still lands at exactly 0, never negative."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(setup_session, store, current_price=Decimal("50.00"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        from app.modules.purchasing.models import PurchaseOrderItem

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("1"),
            unit_cost=Decimal("20.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        purchasing_service.receive_goods(
            setup_session,
            purchase_order_id=po.id,
            received_date=date.today(),
            lines=[GoodsReceiptLineInput(item.id, Decimal("1"), Decimal("20.00"))],
        )
        setup_session.commit()
        store_id, product_id, cashier_id = store.id, product.id, cashier.id
    finally:
        setup_session.close()

    thread_count = 5
    barrier = threading.Barrier(thread_count)
    results = [_AttemptResult() for _ in range(thread_count)]
    threads = [
        threading.Thread(
            target=_attempt_one_unit_sale,
            kwargs=dict(
                store_id=store_id,
                product_id=product_id,
                cashier_id=cashier_id,
                barrier=barrier,
                result=results[i],
            ),
        )
        for i in range(thread_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    successes = [r for r in results if r.succeeded]
    failures = [r for r in results if not r.succeeded]
    assert len(successes) == 1, f"expected exactly one success among {thread_count}, got {results}"
    assert len(failures) == thread_count - 1
    assert all(r.error_code == "INSUFFICIENT_STOCK" for r in failures)

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        assert product is not None
        assert product.current_qty_on_hand == Decimal("0")
        movements = (
            verify_session.query(InventoryMovement)
            .filter_by(product_id=product_id, movement_type="SALE")
            .all()
        )
        assert len(movements) == 1
    finally:
        verify_session.close()
