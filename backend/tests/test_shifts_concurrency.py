"""M15 Session F: concurrency, proven against real PostgreSQL with
genuinely independent connections (same discipline as
tests/test_purchasing_concurrency.py — see that file's module docstring
for why the `db` fixture's savepoint isolation can't be used here).

Covers the task's mandatory concurrency matrix:
1. Two simultaneous shift opens for the same cashier.
2. Two simultaneous closes for the same shift (duplicate key).
3. A sale finalization racing with shift closure.
4. A cash movement racing with shift closure.
5. Duplicate requests with the same idempotency key (open, cash movement).
"""

import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.db.session import SessionLocal
from app.modules.auth.permissions import CASHIER
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput
from app.modules.shifts import service as shifts_service
from app.modules.shifts.models import CashierShift, CashMovement
from tests.factories import make_product, make_store, make_user_with_role, unique_suffix


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    result_id: int | None = None


def _err_code(exc: Exception) -> str | None:
    return getattr(exc, "error_code", None)


# --- 1. Two concurrent opens for the same cashier ---------------------------


def _attempt_open(
    *, store_id: int, cashier_id: int, client_transaction_id: str, barrier, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        shift = shifts_service.open_shift(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            opening_float=Decimal("10.00"),
            client_transaction_id=client_transaction_id,
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.result_id = shift.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def test_two_concurrent_shift_opens_for_same_cashier_only_one_succeeds() -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        store_id, cashier_id = store.id, cashier.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_open,
        kwargs=dict(
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=f"open-a-{unique_suffix()}",
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_open,
        kwargs=dict(
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=f"open-b-{unique_suffix()}",
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
    assert failed[0].error_code == "SHIFT_ALREADY_OPEN"

    verify = SessionLocal()
    try:
        open_shifts = (
            verify.query(CashierShift).filter_by(cashier_id=cashier_id, status="OPEN").all()
        )
        assert len(open_shifts) == 1
    finally:
        verify.close()


# --- 2. Two concurrent closes, same key (idempotent retry race) -------------


def _attempt_close(
    *,
    shift_id: int,
    closing_counted_amount: Decimal,
    actor_id: int,
    client_transaction_id: str,
    barrier,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        shift = shifts_service.close_shift(
            session,
            shift_id=shift_id,
            closing_counted_amount=closing_counted_amount,
            actor_id=actor_id,
            caller_store_id=None,
            client_transaction_id=client_transaction_id,
        )
        session.commit()
        result.succeeded = True
        result.result_id = shift.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def test_concurrent_duplicate_close_requests_same_key_return_same_result() -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        shift = shifts_service.open_shift(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("50.00"),
            client_transaction_id=f"open-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        shift_id, cashier_id = shift.id, cashier.id
    finally:
        setup.close()

    shared_key = f"close-{unique_suffix()}"
    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_close,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("50.00"),
            actor_id=cashier_id,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_close,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("50.00"),
            actor_id=cashier_id,
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
    assert result_a.result_id == result_b.result_id

    verify = SessionLocal()
    try:
        closed = verify.get(CashierShift, shift_id)
        assert closed.status == "CLOSED"
        assert closed.variance_amount == Decimal("0.00")
    finally:
        verify.close()


def test_two_concurrent_close_requests_different_keys_only_one_succeeds() -> None:
    """Distinct from the idempotent-retry race above: two GENUINELY
    different close attempts (different client_transaction_id each, e.g.
    two browser tabs) racing for the same shift. Exactly one must win;
    the other must see the shift already CLOSED and be rejected, never
    silently closing it a second time or double-posting a variance."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        shift = shifts_service.open_shift(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("30.00"),
            client_transaction_id=f"open-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        shift_id, cashier_id = shift.id, cashier.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_close,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("30.00"),
            actor_id=cashier_id,
            client_transaction_id=f"close-a-{unique_suffix()}",
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_close,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("35.00"),
            actor_id=cashier_id,
            client_transaction_id=f"close-b-{unique_suffix()}",
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
    assert failed[0].error_code == "SHIFT_NOT_OPEN"

    verify = SessionLocal()
    try:
        closed = verify.get(CashierShift, shift_id)
        assert closed.status == "CLOSED"
        # Exactly one variance journal entry — the loser's attempt never
        # posted anything.
        from app.modules.accounting.models import JournalEntry

        entries = (
            verify.query(JournalEntry)
            .filter_by(source_type="CASH_SHIFT_VARIANCE", source_id=shift_id)
            .all()
        )
        assert len(entries) <= 1
    finally:
        verify.close()


# --- 3. Sale finalization racing shift closure -------------------------------


def _attempt_sale_holding_shift_lock(
    *,
    store_id: int,
    cashier_id: int,
    product_id: int,
    barrier,
    hold_seconds: float,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        # Deliberately acquire the shift's row lock FIRST (exactly what
        # finalize_sale does internally), then hold it briefly so the
        # concurrent close attempt below genuinely blocks on it — proving
        # the lock, not luck, is what prevents the lost-update race
        # docs/M15_DESIGN.md warns about.
        shifts_service.lock_active_shift_for_cashier(session, cashier_id)
        barrier.wait(timeout=10)
        time.sleep(hold_seconds)
        sale = sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=f"sale-{unique_suffix()}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product_id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
        )
        session.commit()
        result.succeeded = True
        result.result_id = sale.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def _attempt_close_no_delay(
    *, shift_id: int, closing_counted_amount: Decimal, actor_id: int, barrier, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        shift = shifts_service.close_shift(
            session,
            shift_id=shift_id,
            closing_counted_amount=closing_counted_amount,
            actor_id=actor_id,
            caller_store_id=None,
            client_transaction_id=f"close-{unique_suffix()}",
        )
        session.commit()
        result.succeeded = True
        result.result_id = shift.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def test_sale_finalization_racing_shift_close_is_never_lost() -> None:
    """The critical invariant (docs/M15_DESIGN.md "Concurrency: sale
    finalization vs shift close"): a cash sale that wins the race to
    attach to a shift must ALWAYS be counted in that shift's expected
    cash when it closes — never silently understating the till."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        product = make_product(
            setup, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("50")
        )
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        shift = shifts_service.open_shift(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("100.00"),
            client_transaction_id=f"open-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        store_id, product_id, cashier_id, shift_id = store.id, product.id, cashier.id, shift.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    sale_result, close_result = _Outcome(), _Outcome()
    thread_sale = threading.Thread(
        target=_attempt_sale_holding_shift_lock,
        kwargs=dict(
            store_id=store_id,
            cashier_id=cashier_id,
            product_id=product_id,
            barrier=barrier,
            hold_seconds=0.4,
            result=sale_result,
        ),
    )
    thread_close = threading.Thread(
        target=_attempt_close_no_delay,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("120.00"),
            actor_id=cashier_id,
            barrier=barrier,
            result=close_result,
        ),
    )
    thread_sale.start()
    thread_close.start()
    thread_sale.join(timeout=15)
    thread_close.join(timeout=15)

    assert sale_result.unexpected_error is None, sale_result
    assert close_result.unexpected_error is None, close_result
    assert sale_result.succeeded, sale_result
    assert close_result.succeeded, close_result

    verify = SessionLocal()
    try:
        sale = verify.get(Sale, sale_result.result_id)
        closed_shift = verify.get(CashierShift, shift_id)
        # The sale finished (and held the lock) BEFORE close_shift could
        # acquire it, so it MUST be attributed to the shift...
        assert sale.shift_id == shift_id
        # ...and therefore MUST be counted: opening 100 + this 20 cash
        # sale = 120 expected, matching what was counted -> zero variance.
        assert closed_shift.expected_cash_amount == Decimal("120.00")
        assert closed_shift.variance_amount == Decimal("0.00")
    finally:
        verify.close()


# --- 4. Cash movement racing shift closure -----------------------------------


def _attempt_movement_holding_lock(
    *, shift_id: int, actor_id: int, barrier, hold_seconds: float, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        # record_cash_movement locks the shift row itself (see
        # app.modules.shifts.service.record_cash_movement); grabbing it
        # here first and sleeping proves the close below genuinely blocks
        # on it rather than racing unprotected.
        session.execute(select(CashierShift).where(CashierShift.id == shift_id).with_for_update())
        barrier.wait(timeout=10)
        time.sleep(hold_seconds)
        movement = shifts_service.record_cash_movement(
            session,
            shift_id=shift_id,
            movement_type="PAID_IN",
            amount=Decimal("15.00"),
            reason="race test top-up",
            created_by=actor_id,
            client_transaction_id=f"mv-{unique_suffix()}",
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.result_id = movement.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def test_cash_movement_racing_shift_close_resolves_deterministically() -> None:
    """Whichever side acquires the shift row lock first wins; the other
    resolves deterministically (the movement is rejected SHIFT_NOT_OPEN if
    the close won, or the close correctly includes the movement in
    expected cash if the movement won) — never a lost update, never a
    partial/inconsistent state."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        shift = shifts_service.open_shift(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("50.00"),
            client_transaction_id=f"open-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        cashier_id, shift_id = cashier.id, shift.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    movement_result, close_result = _Outcome(), _Outcome()
    thread_movement = threading.Thread(
        target=_attempt_movement_holding_lock,
        kwargs=dict(
            shift_id=shift_id,
            actor_id=cashier_id,
            barrier=barrier,
            hold_seconds=0.4,
            result=movement_result,
        ),
    )
    thread_close = threading.Thread(
        target=_attempt_close_no_delay,
        kwargs=dict(
            shift_id=shift_id,
            closing_counted_amount=Decimal("65.00"),
            actor_id=cashier_id,
            barrier=barrier,
            result=close_result,
        ),
    )
    thread_movement.start()
    thread_close.start()
    thread_movement.join(timeout=15)
    thread_close.join(timeout=15)

    assert movement_result.unexpected_error is None, movement_result
    assert close_result.unexpected_error is None, close_result
    # The movement held the lock first, so it must have won and the close
    # (blocked behind it) must then see the shift still OPEN and succeed,
    # correctly counting the PAID_IN in expected cash.
    assert movement_result.succeeded, movement_result
    assert close_result.succeeded, close_result

    verify = SessionLocal()
    try:
        closed_shift = verify.get(CashierShift, shift_id)
        # 50 opening + 15 paid-in = 65 expected, matching counted -> zero variance.
        assert closed_shift.expected_cash_amount == Decimal("65.00")
        assert closed_shift.variance_amount == Decimal("0.00")
        movements = verify.query(CashMovement).filter_by(shift_id=shift_id).all()
        assert len(movements) == 1
    finally:
        verify.close()


# --- 5. Duplicate requests with the same idempotency key --------------------


def _attempt_movement(
    *, shift_id: int, actor_id: int, client_transaction_id: str, barrier, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        movement = shifts_service.record_cash_movement(
            session,
            shift_id=shift_id,
            movement_type="PAID_IN",
            amount=Decimal("8.00"),
            reason="duplicate key race",
            created_by=actor_id,
            client_transaction_id=client_transaction_id,
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.result_id = movement.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error_code = _err_code(exc)
        result.unexpected_error = repr(exc) if result.error_code is None else None
    finally:
        session.close()


def test_concurrent_duplicate_cash_movement_requests_create_only_one() -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user_with_role(setup, store, CASHIER, username=f"c_{unique_suffix()}")
        setup.commit()
        shift = shifts_service.open_shift(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("10.00"),
            client_transaction_id=f"open-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        cashier_id, shift_id = cashier.id, shift.id
    finally:
        setup.close()

    shared_key = f"mv-{unique_suffix()}"
    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_movement,
        kwargs=dict(
            shift_id=shift_id,
            actor_id=cashier_id,
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_movement,
        kwargs=dict(
            shift_id=shift_id,
            actor_id=cashier_id,
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
    assert result_a.result_id == result_b.result_id

    verify = SessionLocal()
    try:
        count = verify.query(CashMovement).filter_by(client_transaction_id=shared_key).count()
        assert count == 1
    finally:
        verify.close()
