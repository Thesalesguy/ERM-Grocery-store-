"""M15 Session G: failure injection at critical transaction boundaries.

Mirrors tests/test_sale_finalization_failure_injection.py's exact
methodology: monkeypatch a function called partway through a service
function to raise, confirm the whole operation raises out to the caller
with nothing committed, `db.rollback()`, then assert no partial state
survived — and that a clean retry with the SAME idempotency key afterward
succeeds normally.

The invariant under test throughout (docs/M15_DESIGN.md "Failure
injection"): no partial financial state. A failed close must never leave
a closed shift with no variance, a variance without its corresponding
close, GL entries without their operational state, or operational state
without required GL entries.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.audit import service as audit_service
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import CASHIER
from app.modules.shifts import service as shifts_service
from app.modules.shifts.models import CashierShift, CashMovement
from tests.factories import make_store, make_user_with_role, unique_suffix


def _boom(*args, **kwargs):
    raise RuntimeError("simulated failure injected for testing")


# --- Shift opening ------------------------------------------------------


def test_failure_during_shift_open_audit_leaves_no_shift_row(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()

    shift_count_before = db.execute(select(func.count()).select_from(CashierShift)).scalar_one()

    monkeypatch.setattr(audit_service, "log_event", _boom)

    key = f"open-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="simulated failure"):
        shifts_service.open_shift(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("50.00"),
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    assert (
        db.execute(select(func.count()).select_from(CashierShift)).scalar_one()
        == shift_count_before
    )
    assert (
        db.execute(select(CashierShift).where(CashierShift.client_transaction_id == key)).first()
        is None
    )


def test_retry_after_failed_open_succeeds_cleanly(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()

    monkeypatch.setattr(audit_service, "log_event", _boom)
    key = f"open-{unique_suffix()}"
    with pytest.raises(RuntimeError):
        shifts_service.open_shift(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            opening_float=Decimal("50.00"),
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    monkeypatch.undo()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=key,
        caller_store_id=None,
    )
    db.commit()
    assert shift.status == "OPEN"


# --- Cash movements -------------------------------------------------------


def test_failure_during_cash_movement_audit_leaves_no_movement_row(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=f"open-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    movement_count_before = db.execute(select(func.count()).select_from(CashMovement)).scalar_one()

    monkeypatch.setattr(audit_service, "log_event", _boom)
    key = f"mv-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="simulated failure"):
        shifts_service.record_cash_movement(
            db,
            shift_id=shift.id,
            movement_type="PAID_IN",
            amount=Decimal("10.00"),
            reason="test",
            created_by=cashier.id,
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    assert (
        db.execute(select(func.count()).select_from(CashMovement)).scalar_one()
        == movement_count_before
    )
    refreshed = db.get(CashierShift, shift.id)
    assert refreshed.status == "OPEN"


# --- Shift closing ----------------------------------------------------------


def test_failure_during_expected_cash_calculation_leaves_shift_open(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected between acquiring the shift's row lock and mutating it —
    the shift must remain fully OPEN, with no closing field touched."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=f"open-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    monkeypatch.setattr(shifts_service, "_compute_expected_cash", _boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        shifts_service.close_shift(
            db,
            shift_id=shift.id,
            closing_counted_amount=Decimal("50.00"),
            actor_id=cashier.id,
            caller_store_id=None,
            client_transaction_id=f"close-{unique_suffix()}",
        )
    db.rollback()

    refreshed = db.get(CashierShift, shift.id)
    assert refreshed.status == "OPEN"
    assert refreshed.closed_at is None
    assert refreshed.closing_counted_amount is None
    assert refreshed.expected_cash_amount is None
    assert refreshed.variance_amount is None
    assert refreshed.closed_by is None


def test_failure_during_gl_posting_leaves_no_closed_shift_without_variance(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The highest-stakes boundary: by the time GL posting is attempted,
    the shift's closing fields have already been flushed (but not
    committed) in the SAME transaction. A failure here must roll back
    BOTH — never a durably CLOSED shift with no corresponding GL entry,
    and never orphaned GL state either (nothing was posted)."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=f"open-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    journal_count_before = db.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.source_type == "CASH_SHIFT_VARIANCE")
    ).scalar_one()

    monkeypatch.setattr(accounting_service, "post_cash_shift_variance_journal", _boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        shifts_service.close_shift(
            db,
            shift_id=shift.id,
            # Non-zero variance so post_cash_shift_variance_journal would
            # actually have been called and had something to post.
            closing_counted_amount=Decimal("60.00"),
            actor_id=cashier.id,
            caller_store_id=None,
            client_transaction_id=f"close-{unique_suffix()}",
        )
    db.rollback()

    refreshed = db.get(CashierShift, shift.id)
    # Never durably closed -- the flushed-but-uncommitted CLOSED status
    # was rolled back along with everything else in the transaction.
    assert refreshed.status == "OPEN"
    assert refreshed.variance_amount is None
    assert (
        db.execute(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.source_type == "CASH_SHIFT_VARIANCE")
        ).scalar_one()
        == journal_count_before
    )


def test_retry_after_failed_close_succeeds_cleanly(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=f"open-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    monkeypatch.setattr(accounting_service, "post_cash_shift_variance_journal", _boom)
    close_key = f"close-{unique_suffix()}"
    with pytest.raises(RuntimeError):
        shifts_service.close_shift(
            db,
            shift_id=shift.id,
            closing_counted_amount=Decimal("55.00"),
            actor_id=cashier.id,
            caller_store_id=None,
            client_transaction_id=close_key,
        )
    db.rollback()

    monkeypatch.undo()
    closed = shifts_service.close_shift(
        db,
        shift_id=shift.id,
        closing_counted_amount=Decimal("55.00"),
        actor_id=cashier.id,
        caller_store_id=None,
        client_transaction_id=close_key,
    )
    db.commit()
    assert closed.status == "CLOSED"
    assert closed.variance_amount == Decimal("5.00")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "CASH_SHIFT_VARIANCE", JournalEntry.source_id == shift.id
        )
    ).scalar_one()
    assert entry is not None


def test_audit_log_not_left_dangling_after_failed_close(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHIFT_CLOSE_INITIATED is logged before the mutation; SHIFT_CLOSED
    only after everything (including GL posting) succeeds. A failure
    during GL posting must roll back the SHIFT_CLOSE_INITIATED entry too
    — it is not independently committed (docs/M15_DESIGN.md "Failure
    injection": unlike M14's approval-rejection audit commit, this
    function never calls db.commit() itself anywhere)."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    db.commit()
    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("50.00"),
        client_transaction_id=f"open-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    monkeypatch.setattr(accounting_service, "post_cash_shift_variance_journal", _boom)
    with pytest.raises(RuntimeError):
        shifts_service.close_shift(
            db,
            shift_id=shift.id,
            closing_counted_amount=Decimal("55.00"),
            actor_id=cashier.id,
            caller_store_id=None,
            client_transaction_id=f"close-{unique_suffix()}",
        )
    db.rollback()

    events = (
        db.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "cashier_shift", AuditLog.entity_id == shift.id
            )
        )
        .scalars()
        .all()
    )
    actions = {e.action for e in events}
    assert "SHIFT_OPENED" in actions  # this one committed earlier, legitimately survives
    assert "SHIFT_CLOSE_INITIATED" not in actions
    assert "SHIFT_CLOSED" not in actions
