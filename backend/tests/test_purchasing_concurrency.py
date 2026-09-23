"""M3 Section 7's concurrency scenarios for goods receiving, proven against
real PostgreSQL with genuinely independent connections (same discipline as
tests/test_concurrency.py — see that file's module docstring for why the
`db` fixture can't be used here: its savepoint isolation would make one
thread's setup invisible to another thread's connection).

tests/test_concurrency.py::test_concurrent_sale_and_goods_receipt_serialize_correctly
already covers "sale + receipt race" (M3 Section 7.C). This file covers
the receiving-specific scenarios: two receipts against the same product
(A), two receipts against the same PO (B — the exact race the M1 lost-
update bug this milestone fixed would have failed), receipt + stock
adjustment (D), and a multi-item receipt with reversed line order to
prove the PO-lock-then-product-lock ordering can't deadlock (E).
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
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseReturnLineInput
from tests.factories import make_product, make_purchase_order, make_store, make_supplier


@dataclass
class _ReceiptOutcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    receipt_id: int | None = None


def _attempt_receive(
    *,
    purchase_order_id: int,
    lines: list[tuple[int, Decimal, Decimal]],
    barrier: threading.Barrier,
    result: _ReceiptOutcome,
    client_transaction_id: str | None = None,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        receipt = purchasing_service.receive_goods(
            session,
            purchase_order_id=purchase_order_id,
            received_date=date.today(),
            client_transaction_id=client_transaction_id or f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[GoodsReceiptLineInput(po_item_id, qty, cost) for po_item_id, qty, cost in lines],
        )
        session.commit()
        result.succeeded = True
        result.receipt_id = receipt.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock to surface, not vanish
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_a_two_concurrent_receipts_against_the_same_product_serialize() -> None:
    """A: two users receiving the SAME product at the same instant (via
    two different purchase orders) must never corrupt WAC or on-hand
    quantity — both product-row locks must serialize, not race."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(
            setup_session, store, current_qty_on_hand=Decimal("0"), current_cost=Decimal("0")
        )
        po_a = make_purchase_order(setup_session, store, supplier)
        po_b = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        item_a = PurchaseOrderItem(
            purchase_order_id=po_a.id,
            product_id=product.id,
            quantity_ordered=Decimal("100"),
            unit_cost=Decimal("10.00"),
        )
        item_b = PurchaseOrderItem(
            purchase_order_id=po_b.id,
            product_id=product.id,
            quantity_ordered=Decimal("100"),
            unit_cost=Decimal("20.00"),
        )
        setup_session.add_all([item_a, item_b])
        setup_session.commit()
        po_a_id, po_b_id, item_a_id, item_b_id, product_id = (
            po_a.id,
            po_b.id,
            item_a.id,
            item_b.id,
            product.id,
        )
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _ReceiptOutcome(), _ReceiptOutcome()
    thread_a = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_a_id,
            lines=[(item_a_id, Decimal("100"), Decimal("10.00"))],
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_b_id,
            lines=[(item_b_id, Decimal("100"), Decimal("20.00"))],
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded, (result_a, result_b)

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        # Both receipts applied, regardless of order: 100 + 100 = 200 on
        # hand. WAC is order-independent here since both lines are the
        # same qty at different costs feeding one running average — but
        # what matters is NEITHER receipt's effect was lost.
        assert product.current_qty_on_hand == Decimal("200")
        expected_wac_either_order = inventory_service.compute_new_wac(
            existing_qty=Decimal("100"),
            existing_wac=Decimal("10.00"),
            received_qty=Decimal("100"),
            received_unit_cost=Decimal("20.00"),
        )
        assert product.current_cost == expected_wac_either_order
        movements = (
            verify_session.query(InventoryMovement)
            .filter_by(product_id=product_id, movement_type="PURCHASE_RECEIPT")
            .all()
        )
        assert len(movements) == 2
    finally:
        verify_session.close()


def test_b_two_concurrent_receipts_against_the_same_purchase_order() -> None:
    """B: two users receiving against the SAME PO's SAME line at the same
    instant — this is exactly the race the M1 lost-update bug (fixed in
    M3 by locking the PurchaseOrder row before touching quantity_received)
    would have silently dropped one of the two increments on."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("100"),
            unit_cost=Decimal("5.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        po_id, item_id, product_id = po.id, item.id, product.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _ReceiptOutcome(), _ReceiptOutcome()
    thread_a = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_id,
            lines=[(item_id, Decimal("30"), Decimal("5.00"))],
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_id,
            lines=[(item_id, Decimal("20"), Decimal("5.00"))],
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded, (result_a, result_b)

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        item = verify_session.get(PurchaseOrderItem, item_id)
        # 30 + 20 = 50 — if the lost-update bug were still present, this
        # would land at 30 OR 20, never both.
        assert product.current_qty_on_hand == Decimal("50")
        assert item.quantity_received == Decimal("50")
    finally:
        verify_session.close()


def test_d_concurrent_receipt_and_stock_adjustment_serialize_correctly() -> None:
    """D: a goods receipt and a manual stock adjustment race against the
    same product. Both go through inventory_service.lock_product_for_update,
    so the final quantity must reflect both changes exactly."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("10"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("50"),
            unit_cost=Decimal("3.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        po_id, item_id, product_id, store_id = po.id, item.id, product.id, store.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    receipt_result = _ReceiptOutcome()
    adjustment_error: list[str] = []

    def _do_receipt() -> None:
        _attempt_receive(
            purchase_order_id=po_id,
            lines=[(item_id, Decimal("20"), Decimal("3.00"))],
            barrier=barrier,
            result=receipt_result,
        )

    def _do_adjustment() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            inventory_service.create_stock_adjustment(
                session,
                store_id=store_id,
                product_id=product_id,
                quantity_delta=Decimal("-4"),
                reason_code="STOCKTAKE_CORRECTION",
                notes="concurrency test",
                created_by=None,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            adjustment_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    thread_receipt = threading.Thread(target=_do_receipt)
    thread_adjustment = threading.Thread(target=_do_adjustment)
    thread_receipt.start()
    thread_adjustment.start()
    thread_receipt.join(timeout=15)
    thread_adjustment.join(timeout=15)

    assert receipt_result.unexpected_error is None, receipt_result
    assert receipt_result.succeeded, receipt_result
    assert adjustment_error == [], adjustment_error

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        # 10 + 20 (receipt) - 4 (adjustment) = 26, regardless of order.
        assert product.current_qty_on_hand == Decimal("26")
    finally:
        verify_session.close()


def test_e_multi_item_receipt_with_reversed_line_order_does_not_deadlock() -> None:
    """E: two receipts, each against a DIFFERENT PO, but both referencing
    the SAME two products in opposite line order — the naive "lock in
    line order" approach is the classic deadlock shape. receive_goods
    sorts distinct product IDs before locking regardless of line order
    (matching finalize_sale's pattern), so this must complete cleanly
    every time. Run several iterations since a real deadlock is
    timing-sensitive."""
    for _ in range(5):
        setup_session = SessionLocal()
        try:
            store = make_store(setup_session)
            supplier = make_supplier(setup_session)
            product_low = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
            product_high = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
            po_a = make_purchase_order(setup_session, store, supplier)
            po_b = make_purchase_order(setup_session, store, supplier)
            setup_session.commit()

            item_a_low = PurchaseOrderItem(
                purchase_order_id=po_a.id,
                product_id=product_low.id,
                quantity_ordered=Decimal("10"),
                unit_cost=Decimal("1.00"),
            )
            item_a_high = PurchaseOrderItem(
                purchase_order_id=po_a.id,
                product_id=product_high.id,
                quantity_ordered=Decimal("10"),
                unit_cost=Decimal("1.00"),
            )
            item_b_low = PurchaseOrderItem(
                purchase_order_id=po_b.id,
                product_id=product_low.id,
                quantity_ordered=Decimal("10"),
                unit_cost=Decimal("1.00"),
            )
            item_b_high = PurchaseOrderItem(
                purchase_order_id=po_b.id,
                product_id=product_high.id,
                quantity_ordered=Decimal("10"),
                unit_cost=Decimal("1.00"),
            )
            setup_session.add_all([item_a_low, item_a_high, item_b_low, item_b_high])
            setup_session.commit()
            po_a_id, po_b_id = po_a.id, po_b.id
            item_a_low_id, item_a_high_id = item_a_low.id, item_a_high.id
            item_b_low_id, item_b_high_id = item_b_low.id, item_b_high.id
        finally:
            setup_session.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _ReceiptOutcome(), _ReceiptOutcome()
        thread_a = threading.Thread(
            target=_attempt_receive,
            kwargs=dict(
                purchase_order_id=po_a_id,
                # Ascending order in receipt A's own line list...
                lines=[
                    (item_a_low_id, Decimal("1"), Decimal("1.00")),
                    (item_a_high_id, Decimal("1"), Decimal("1.00")),
                ],
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_receive,
            kwargs=dict(
                purchase_order_id=po_b_id,
                # ...but DESCENDING order in receipt B's own line list.
                lines=[
                    (item_b_high_id, Decimal("1"), Decimal("1.00")),
                    (item_b_low_id, Decimal("1"), Decimal("1.00")),
                ],
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
        assert all(r.succeeded for r in outcomes), outcomes


def test_f_concurrent_duplicate_receipt_requests_create_only_one_receipt() -> None:
    """F: two threads submit a receipt against the SAME PO with the SAME
    client_transaction_id at (as close as threading allows) the same
    instant. Exactly one GoodsReceipt must result — see
    tests/test_purchasing_idempotency.py for the sequential-retry cases."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("50"),
            unit_cost=Decimal("2.00"),
        )
        setup_session.add(item)
        setup_session.commit()
        po_id, item_id, product_id = po.id, item.id, product.id
    finally:
        setup_session.close()

    shared_key = f"txn-{uuid.uuid4().hex}"
    barrier = threading.Barrier(2)
    result_a, result_b = _ReceiptOutcome(), _ReceiptOutcome()
    thread_a = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_id,
            lines=[(item_id, Decimal("10"), Decimal("2.00"))],
            barrier=barrier,
            result=result_a,
            client_transaction_id=shared_key,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            purchase_order_id=po_id,
            lines=[(item_id, Decimal("10"), Decimal("2.00"))],
            barrier=barrier,
            result=result_b,
            client_transaction_id=shared_key,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.receipt_id == result_b.receipt_id

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product
        from app.modules.purchasing.models import GoodsReceipt

        count = (
            verify_session.query(GoodsReceipt).filter_by(client_transaction_id=shared_key).count()
        )
        assert count == 1

        product = verify_session.get(Product, product_id)
        # Only ONE receipt's worth of quantity (10), not 20 (double-applied).
        assert product.current_qty_on_hand == Decimal("10")
    finally:
        verify_session.close()


@dataclass
class _PoOutcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    po_id: int | None = None


def _attempt_create_po(
    *,
    store_id: int,
    supplier_id: int,
    product_id: int,
    client_transaction_id: str,
    barrier: threading.Barrier,
    result: _PoOutcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        po = purchasing_service.create_purchase_order(
            session,
            store_id=store_id,
            supplier_id=supplier_id,
            order_date=date.today(),
            client_transaction_id=client_transaction_id,
            lines=[
                purchasing_service.PurchaseOrderItemInput(
                    product_id=product_id, quantity_ordered=Decimal("10"), unit_cost=Decimal("1.00")
                )
            ],
        )
        result.succeeded = True
        result.po_id = po.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock to surface, not vanish
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_g_concurrent_duplicate_purchase_order_creation_creates_only_one_po() -> None:
    """G: M19's PO-creation idempotency key, proven against real threads
    (not the sequential-retry cases tests/test_purchasing_idempotency.py
    covers) — two threads submit a purchase order with the SAME
    client_transaction_id at (as close as threading allows) the same
    instant. Exactly one PurchaseOrder must result, the same discipline
    test_f above already proves for goods receipts."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
        setup_session.commit()
        store_id, supplier_id, product_id = store.id, supplier.id, product.id
    finally:
        setup_session.close()

    shared_key = f"po-{uuid.uuid4().hex}"
    barrier = threading.Barrier(2)
    result_a, result_b = _PoOutcome(), _PoOutcome()
    thread_a = threading.Thread(
        target=_attempt_create_po,
        kwargs=dict(
            store_id=store_id,
            supplier_id=supplier_id,
            product_id=product_id,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_create_po,
        kwargs=dict(
            store_id=store_id,
            supplier_id=supplier_id,
            product_id=product_id,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.po_id == result_b.po_id

    verify_session = SessionLocal()
    try:
        from app.modules.purchasing.models import PurchaseOrder

        count = (
            verify_session.query(PurchaseOrder).filter_by(client_transaction_id=shared_key).count()
        )
        assert count == 1
    finally:
        verify_session.close()


@dataclass
class _ReturnOutcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    return_id: int | None = None


def _attempt_return(
    *,
    purchase_order_id: int,
    store_id: int,
    product_id: int,
    quantity: Decimal,
    barrier: threading.Barrier,
    result: _ReturnOutcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        purchase_return = purchasing_service.create_purchase_return(
            session,
            purchase_order_id=purchase_order_id,
            store_id=store_id,
            return_date=date.today(),
            lines=[PurchaseReturnLineInput(product_id=product_id, quantity=quantity)],
            client_transaction_id=f"ret-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.return_id = purchase_return.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock to surface, not vanish
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_h_concurrent_returns_against_the_same_po_cannot_jointly_over_return() -> None:
    """H: M19's received-quantity ceiling on purchase returns
    (RETURN_EXCEEDS_RECEIVED_QUANTITY), proven against two real threads
    that EACH individually request a quantity within what was received,
    but which together would exceed it. The PurchaseOrderItem row lock
    the validation takes must serialize the two requests so exactly one
    succeeds -- never both, which would silently over-return."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store, current_qty_on_hand=Decimal("0"))
        po = make_purchase_order(setup_session, store, supplier)
        setup_session.commit()

        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("10"),
            quantity_received=Decimal("10"),
            unit_cost=Decimal("2.00"),
        )
        setup_session.add(item)
        setup_session.commit()

        product.current_qty_on_hand = Decimal("10")
        setup_session.add(product)
        setup_session.commit()
        store_id, po_id, product_id = store.id, po.id, product.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _ReturnOutcome(), _ReturnOutcome()
    thread_a = threading.Thread(
        target=_attempt_return,
        kwargs=dict(
            purchase_order_id=po_id,
            store_id=store_id,
            product_id=product_id,
            quantity=Decimal("7"),
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_return,
        kwargs=dict(
            purchase_order_id=po_id,
            store_id=store_id,
            product_id=product_id,
            quantity=Decimal("7"),
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b

    outcomes = [result_a, result_b]
    succeeded = [r for r in outcomes if r.succeeded]
    failed = [r for r in outcomes if not r.succeeded]
    assert len(succeeded) == 1, outcomes
    assert len(failed) == 1, outcomes
    assert failed[0].error_code == "RETURN_EXCEEDS_RECEIVED_QUANTITY"

    verify_session = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify_session.get(Product, product_id)
        # Only ONE return's worth (7), not both (14) -- the loser never
        # applied its inventory effect.
        assert product.current_qty_on_hand == Decimal("3")
    finally:
        verify_session.close()
