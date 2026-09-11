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
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.inventory import service as inventory_service
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
    unexpected_error: str | None = None


def _attempt_one_unit_sale(
    *,
    store_id: int,
    product_id: int,
    cashier_id: int,
    barrier: threading.Barrier,
    result: _AttemptResult,
    client_transaction_id: str | None = None,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)  # both threads start their DB work together
        sale = sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=client_transaction_id or f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
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


def _attempt_sale(
    *,
    store_id: int,
    cashier_id: int,
    lines: list[tuple[int, Decimal]],
    payment_amount: Decimal,
    barrier: threading.Barrier,
    result: _AttemptResult,
    client_transaction_id: str | None = None,
) -> None:
    """General multi-line version of _attempt_one_unit_sale, for tests
    where a sale touches more than one product (B/C/G below). Catches any
    exception, not just ConflictError, so a real deadlock (which
    PostgreSQL surfaces as a DBAPI/OperationalError, not our ConflictError)
    shows up as `unexpected_error` instead of crashing the thread
    silently — the whole point of test C is to prove that never happens.
    """
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        sale = sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=client_transaction_id or f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=pid, quantity=qty) for pid, qty in lines],
            payments=[PaymentInput(payment_method="CASH", amount=payment_amount)],
        )
        session.commit()
        result.succeeded = True
        result.sale_id = sale.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
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


def _setup_two_products(
    *, shared_qty: Decimal, other_qty: Decimal = Decimal("50")
) -> tuple[int, int, int, int]:
    """Returns (store_id, cashier_id, shared_product_id, other_a_id).
    Also returns a third product via a second call is not needed — see
    _setup_three_products for the deadlock-order test."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        shared = make_product(
            setup_session, store, current_price=Decimal("10.00"), current_qty_on_hand=shared_qty
        )
        other = make_product(
            setup_session, store, current_price=Decimal("5.00"), current_qty_on_hand=other_qty
        )
        setup_session.commit()
        return store.id, cashier.id, shared.id, other.id
    finally:
        setup_session.close()


def test_two_concurrent_multi_item_sales_sharing_one_product() -> None:
    """B: two multi-item sales, each with a distinct second product, but
    both wanting the last unit of a SHARED first product. Exactly one
    whole sale wins; the loser's sale is rejected in its entirety (not
    just the shared line) — its own distinct product must be untouched."""
    store_id, cashier_id, shared_id, product_a_id = _setup_two_products(shared_qty=Decimal("1"))
    # A second, independent "only" product for thread B, so a bug that
    # partially applies a multi-line sale would show up as this product's
    # stock moving even though thread B's overall sale should fail.
    setup_session = SessionLocal()
    try:
        from app.modules.auth.models import Store

        store = setup_session.get(Store, store_id)
        product_b = make_product(
            setup_session, store, current_price=Decimal("5.00"), current_qty_on_hand=Decimal("50")
        )
        setup_session.commit()
        product_b_id = product_b.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _AttemptResult(), _AttemptResult()
    thread_a = threading.Thread(
        target=_attempt_sale,
        kwargs=dict(
            store_id=store_id,
            cashier_id=cashier_id,
            lines=[(shared_id, Decimal("1")), (product_a_id, Decimal("1"))],
            payment_amount=Decimal("100.00"),
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_sale,
        kwargs=dict(
            store_id=store_id,
            cashier_id=cashier_id,
            lines=[(shared_id, Decimal("1")), (product_b_id, Decimal("1"))],
            payment_amount=Decimal("100.00"),
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    outcomes = [result_a, result_b]
    assert all(r.unexpected_error is None for r in outcomes), outcomes
    successes = [r for r in outcomes if r.succeeded]
    failures = [r for r in outcomes if not r.succeeded]
    assert len(successes) == 1, f"expected exactly one success, got {outcomes}"
    assert failures[0].error_code == "INSUFFICIENT_STOCK"

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        shared = verify_session.get(Product, shared_id)
        assert shared.current_qty_on_hand == Decimal("0")

        # Whichever thread lost must have left ITS OWN distinct product
        # completely untouched — proving the whole multi-line sale was
        # rejected atomically, not partially applied.
        if result_a.succeeded:
            assert verify_session.get(Product, product_a_id).current_qty_on_hand == Decimal("49")
            assert verify_session.get(Product, product_b_id).current_qty_on_hand == Decimal("50")
        else:
            assert verify_session.get(Product, product_a_id).current_qty_on_hand == Decimal("50")
            assert verify_session.get(Product, product_b_id).current_qty_on_hand == Decimal("49")
    finally:
        verify_session.close()


def test_two_concurrent_multi_item_sales_with_lines_in_different_order_do_not_deadlock() -> None:
    """C: the two sales reference the SAME two products but list them in
    OPPOSITE order in their own cart — the naive "lock in cart order"
    approach would have thread A lock (shared, then A-only) while thread B
    locks (A-only, then shared) simultaneously, the classic deadlock
    shape. finalize_sale sorts by product_id before locking regardless of
    input order (see its module docstring), so this must complete cleanly
    — no deadlock, no unexpected exception — every time."""
    store_id, cashier_id, product_low_id, product_high_id = _setup_two_products(
        shared_qty=Decimal("2"), other_qty=Decimal("2")
    )
    # Run several iterations: a real deadlock is timing-sensitive, so one
    # clean pass is weak evidence but several back-to-back passes with
    # reversed lock-acquisition attempts is a meaningful stress test.
    for _ in range(5):
        setup_session = SessionLocal()
        try:
            from app.modules.auth.models import Store

            store = setup_session.get(Store, store_id)
            p_low = make_product(
                setup_session,
                store,
                current_price=Decimal("1.00"),
                current_qty_on_hand=Decimal("5"),
            )
            p_high = make_product(
                setup_session,
                store,
                current_price=Decimal("1.00"),
                current_qty_on_hand=Decimal("5"),
            )
            setup_session.commit()
            p_low_id, p_high_id = p_low.id, p_high.id
        finally:
            setup_session.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _AttemptResult(), _AttemptResult()
        thread_a = threading.Thread(
            target=_attempt_sale,
            kwargs=dict(
                store_id=store_id,
                cashier_id=cashier_id,
                # Ascending order in thread A's own cart...
                lines=[(p_low_id, Decimal("1")), (p_high_id, Decimal("1"))],
                payment_amount=Decimal("100.00"),
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_sale,
            kwargs=dict(
                store_id=store_id,
                cashier_id=cashier_id,
                # ...but DESCENDING order in thread B's own cart.
                lines=[(p_high_id, Decimal("1")), (p_low_id, Decimal("1"))],
                payment_amount=Decimal("100.00"),
                barrier=barrier,
                result=result_b,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        outcomes = [result_a, result_b]
        assert all(
            r.unexpected_error is None for r in outcomes
        ), f"a deadlock or other unexpected error occurred: {outcomes}"
        # Plenty of stock (5 each, only 1 requested each) — both sales
        # should succeed; the point of this test is deadlock-freedom, not
        # stock contention (that's tests A/B/D above).
        assert all(r.succeeded for r in outcomes), outcomes


def test_concurrent_sale_and_stock_adjustment_serialize_correctly() -> None:
    """E: a sale and a manual stock adjustment race against the same
    product. Both go through inventory_service.lock_product_for_update,
    so they must serialize (one waits for the other's row lock) rather
    than racing — the final quantity must reflect BOTH changes exactly,
    regardless of which happened first, never a lost update."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
        )
        setup_session.commit()
        store_id, cashier_id, product_id = store.id, cashier.id, product.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    sale_result = _AttemptResult()
    adjustment_error: list[str] = []

    def _do_sale() -> None:
        _attempt_sale(
            store_id=store_id,
            cashier_id=cashier_id,
            lines=[(product_id, Decimal("3"))],
            payment_amount=Decimal("100.00"),
            barrier=barrier,
            result=sale_result,
        )

    def _do_adjustment() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            inventory_service.create_stock_adjustment(
                session,
                store_id=store_id,
                product_id=product_id,
                quantity_delta=Decimal("-2"),
                reason_code="STOCKTAKE_CORRECTION",
                notes="concurrency test",
                created_by=cashier_id,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            adjustment_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    thread_sale = threading.Thread(target=_do_sale)
    thread_adjustment = threading.Thread(target=_do_adjustment)
    thread_sale.start()
    thread_adjustment.start()
    thread_sale.join(timeout=15)
    thread_adjustment.join(timeout=15)

    assert sale_result.unexpected_error is None, sale_result
    assert sale_result.succeeded, sale_result
    assert adjustment_error == [], adjustment_error

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        # 10 - 3 (sale) - 2 (adjustment) = 5, regardless of which
        # transaction's row lock was granted first.
        assert product.current_qty_on_hand == Decimal("5")
        movements = verify_session.query(InventoryMovement).filter_by(product_id=product_id).all()
        assert len(movements) == 2
        assert {m.movement_type for m in movements} == {"SALE", "STOCK_ADJUSTMENT_OUT"}
    finally:
        verify_session.close()


def test_concurrent_sale_and_goods_receipt_serialize_correctly() -> None:
    """F: a sale and a purchasing goods receipt race against the same
    product. Both lock the product row before touching quantity/WAC, so
    the final quantity must be exactly right regardless of interleaving —
    a lost update here would mean either the sale's decrement or the
    receipt's increment silently disappears."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
        )
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        from app.modules.purchasing.models import PurchaseOrderItem

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("10"),
            unit_cost=Decimal("4.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        store_id, cashier_id, product_id, po_id, item_id = (
            store.id,
            cashier.id,
            product.id,
            po.id,
            item.id,
        )
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    sale_result = _AttemptResult()
    receipt_error: list[str] = []

    def _do_sale() -> None:
        _attempt_sale(
            store_id=store_id,
            cashier_id=cashier_id,
            lines=[(product_id, Decimal("2"))],
            payment_amount=Decimal("100.00"),
            barrier=barrier,
            result=sale_result,
        )

    def _do_receipt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            purchasing_service.receive_goods(
                session,
                purchase_order_id=po_id,
                received_date=date.today(),
                lines=[GoodsReceiptLineInput(item_id, Decimal("10"), Decimal("4.00"))],
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            receipt_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    thread_sale = threading.Thread(target=_do_sale)
    thread_receipt = threading.Thread(target=_do_receipt)
    thread_sale.start()
    thread_receipt.start()
    thread_sale.join(timeout=15)
    thread_receipt.join(timeout=15)

    assert sale_result.unexpected_error is None, sale_result
    assert sale_result.succeeded, sale_result
    assert receipt_error == [], receipt_error

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        # 5 - 2 (sale) + 10 (receipt) = 13, regardless of ordering.
        assert product.current_qty_on_hand == Decimal("13")
    finally:
        verify_session.close()
