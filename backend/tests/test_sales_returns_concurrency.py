"""M5 Session D: return/void concurrency, proven against real PostgreSQL
with genuinely independent connections (same discipline as
tests/test_purchasing_concurrency.py and tests/test_concurrency.py — see
those files' module docstrings for why the `db` fixture's savepoint
isolation can't be used here).

Three scenarios, each repeated 5x since a real race is timing-sensitive:

A: two concurrent returns racing to return the SAME single remaining
   unit of the same sale line. Exactly one must win; the loser must be
   rejected with EXCESSIVE_RETURN_QUANTITY, never silently oversell the
   line past its own `quantity`.
B: two concurrent PARTIAL returns of the same line whose combined
   quantity exactly equals what remains. Both must succeed (the lock on
   the parent Sale row serializes them so the second sees the first's
   already-applied quantity_returned) and the line must land at exactly
   fully returned — never over, never under.
C: two concurrent requests carrying the SAME client_transaction_id.
   Exactly one SaleReturn row must be created; both callers must observe
   the identical return id (the idempotency fast path + the unique
   constraint's IntegrityError-recovery path must both hold up under a
   real race, not just a sequential retry).
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale, SaleItem, SaleReturn
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import make_product, make_store, make_user


@dataclass
class _ReturnOutcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    return_id: int | None = None


def _attempt_return(
    *,
    sale_id: int,
    store_id: int,
    quantity: Decimal,
    sale_item_id: int,
    barrier: threading.Barrier,
    result: _ReturnOutcome,
    client_transaction_id: str | None = None,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        sale_return = sales_service.create_sale_return(
            session,
            sale_id=sale_id,
            store_id=store_id,
            return_date=date.today(),
            lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=quantity, restock=True)],
            refund_method="CASH",
            client_transaction_id=client_transaction_id or f"ret-txn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.return_id = sale_return.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock/error to surface, not vanish
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _sell_one_line(*, quantity: Decimal) -> tuple[int, int, int, Decimal]:
    """Returns (sale_id, sale_item_id, store_id, unit_price)."""
    session = SessionLocal()
    try:
        store = make_store(session)
        cashier = make_user(session, store)
        product = make_product(
            session,
            store,
            current_price=Decimal("10.00"),
            current_qty_on_hand=Decimal("100"),
        )
        session.commit()
        sale = sales_service.finalize_sale(
            session,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=quantity)],
            payments=[PaymentInput(payment_method="CASH", amount=quantity * Decimal("10.00"))],
        )
        session.commit()
        sale_item_id = (
            session.execute(SaleItem.__table__.select().where(SaleItem.sale_id == sale.id))
            .first()
            .id
        )
        return sale.id, sale_item_id, store.id, Decimal("10.00")
    finally:
        session.close()


def test_a_two_concurrent_returns_of_the_same_single_unit_exactly_one_wins() -> None:
    """A: sell exactly 1 unit; two threads race to return that same 1
    unit. Exactly one must succeed; the other must be rejected (never
    both succeed, which would return -1 units' worth and corrupt
    quantity_returned past `quantity`). The loser's exact error code
    depends on which layered guard it hits: EXCESSIVE_RETURN_QUANTITY if
    it loses the Sale-row lock race while the sale is still
    PARTIALLY_REFUNDED-eligible, or SALE_NOT_RETURNABLE if the winner's
    return already fully completed the sale (advanced it to REFUNDED)
    before the loser re-checks status — both are correct rejections of
    the same over-return attempt, just different layers of the same
    lock-serialized validation."""
    for _ in range(5):
        sale_id, sale_item_id, store_id, _ = _sell_one_line(quantity=Decimal("1"))

        barrier = threading.Barrier(2)
        result_a, result_b = _ReturnOutcome(), _ReturnOutcome()
        thread_a = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("1"),
                sale_item_id=sale_item_id,
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("1"),
                sale_item_id=sale_item_id,
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
        winners = [r for r in outcomes if r.succeeded]
        losers = [r for r in outcomes if not r.succeeded]
        assert len(winners) == 1, outcomes
        assert len(losers) == 1, outcomes
        assert losers[0].error_code in ("EXCESSIVE_RETURN_QUANTITY", "SALE_NOT_RETURNABLE"), losers[
            0
        ]

        verify_session = SessionLocal()
        try:
            item = verify_session.get(SaleItem, sale_item_id)
            assert item.quantity_returned == Decimal("1")  # never 2, never 0
        finally:
            verify_session.close()


def test_b_two_concurrent_partial_returns_summing_exactly_to_quantity_both_succeed() -> None:
    """B: sell 10 units; two threads concurrently return 6 and 4 (summing
    to exactly the sold quantity). Both must succeed — the Sale-row lock
    serializes them so the second sees the first's already-applied
    quantity_returned via `_enforce_store_access`-style fresh reads under
    FOR UPDATE, not a stale in-memory value — and the line must land at
    exactly fully returned, never oversold past 10."""
    for _ in range(5):
        sale_id, sale_item_id, store_id, _ = _sell_one_line(quantity=Decimal("10"))

        barrier = threading.Barrier(2)
        result_a, result_b = _ReturnOutcome(), _ReturnOutcome()
        thread_a = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("6"),
                sale_item_id=sale_item_id,
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("4"),
                sale_item_id=sale_item_id,
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
            item = verify_session.get(SaleItem, sale_item_id)
            assert item.quantity_returned == Decimal("10")  # exactly, not 6, not 4, not >10
            sale = verify_session.get(Sale, sale_id)
            assert sale.status == "REFUNDED"
        finally:
            verify_session.close()


def test_c_two_concurrent_requests_with_the_same_idempotency_key_create_exactly_one_return() -> (
    None
):
    """C: two threads submit a return against the SAME sale/line with the
    SAME client_transaction_id at (as close as threading allows) the same
    instant. Exactly one SaleReturn row must exist afterward, and both
    callers must see the identical return id — proving the
    IntegrityError-recovery path (not just the sequential idempotency
    fast path already covered by tests/test_sales_returns_api.py) holds
    under a genuine race."""
    for _ in range(5):
        sale_id, sale_item_id, store_id, _ = _sell_one_line(quantity=Decimal("5"))
        shared_key = f"ret-txn-{uuid.uuid4().hex}"

        barrier = threading.Barrier(2)
        result_a, result_b = _ReturnOutcome(), _ReturnOutcome()
        thread_a = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("5"),
                sale_item_id=sale_item_id,
                barrier=barrier,
                result=result_a,
                client_transaction_id=shared_key,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_return,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                quantity=Decimal("5"),
                sale_item_id=sale_item_id,
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
        assert result_a.succeeded and result_b.succeeded, (result_a, result_b)
        assert result_a.return_id == result_b.return_id

        verify_session = SessionLocal()
        try:
            count = (
                verify_session.query(SaleReturn).filter_by(client_transaction_id=shared_key).count()
            )
            assert count == 1

            item = verify_session.get(SaleItem, sale_item_id)
            # Only ONE return's worth of quantity applied (5), not 10
            # (double-applied) — the same guarantee test_f in
            # test_purchasing_concurrency.py proves for goods receipts.
            assert item.quantity_returned == Decimal("5")
        finally:
            verify_session.close()
