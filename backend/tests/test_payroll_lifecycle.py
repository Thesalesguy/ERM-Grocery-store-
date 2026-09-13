"""M10 Phase 4: payroll period lifecycle tests. Covers create/open/
approve/cancel, idempotency-by-state, store scoping, and the approve-
time staleness guard (a corrected attendance record newer than
calculated_at must block approval)."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationAppError
from app.modules.payroll import service as payroll_service
from app.modules.payroll.service import PayrollPeriodInput
from tests.factories import make_store, make_user


def _create(db: Session, store, **overrides) -> object:
    defaults = dict(
        store_id=store.id,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        pay_date=date(2024, 1, 20),
    )
    defaults.update(overrides)
    return payroll_service.create_payroll_period(
        db, PayrollPeriodInput(**defaults), actor_id=None, caller_store_id=None
    )


def _mark_calculated(db: Session, period_id: int, calculated_by: int, calculated_at: str) -> None:
    db.execute(
        text(
            "UPDATE payroll_periods SET status = 'CALCULATED', calculated_by = :u, "
            "calculated_at = :t WHERE id = :p"
        ),
        {"u": calculated_by, "t": calculated_at, "p": period_id},
    )


def test_create_payroll_period_starts_in_draft(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    assert period.status == "DRAFT"


def test_duplicate_period_same_store_and_range_is_a_clean_conflict(db: Session) -> None:
    store = make_store(db)
    _create(db, store)
    with pytest.raises(ConflictError):
        _create(db, store)


def test_invalid_period_range_is_rejected(db: Session) -> None:
    store = make_store(db)
    with pytest.raises(ValidationAppError):
        _create(db, store, period_start=date(2024, 1, 15), period_end=date(2024, 1, 1))


def test_pay_date_before_period_end_is_rejected(db: Session) -> None:
    store = make_store(db)
    with pytest.raises(ValidationAppError):
        _create(db, store, pay_date=date(2024, 1, 1))


def test_open_transition_and_idempotency(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)

    opened = payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    assert opened.status == "OPEN"

    # Idempotent by state: calling open again on an already-OPEN period
    # is a no-op returning the current state, not an error.
    opened_again = payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    assert opened_again.status == "OPEN"


def test_open_a_non_draft_period_that_is_not_open_is_rejected(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    payroll_service.cancel_payroll_period(
        db,
        payroll_period_id=period.id,
        reason="Store closed for the period",
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ConflictError):
        payroll_service.open_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )


def test_cancel_is_idempotent_and_blocked_after_posted(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)

    cancelled = payroll_service.cancel_payroll_period(
        db,
        payroll_period_id=period.id,
        reason="Store closed for the period",
        actor_id=None,
        caller_store_id=None,
    )
    assert cancelled.status == "CANCELLED"

    # Idempotent: cancelling an already-CANCELLED period is a no-op.
    cancelled_again = payroll_service.cancel_payroll_period(
        db,
        payroll_period_id=period.id,
        reason="Store closed for the period",
        actor_id=None,
        caller_store_id=None,
    )
    assert cancelled_again.status == "CANCELLED"


def test_cancel_requires_a_reason(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    with pytest.raises(ValidationAppError):
        payroll_service.cancel_payroll_period(
            db, payroll_period_id=period.id, reason="   ", actor_id=None, caller_store_id=None
        )


def test_cancel_a_posted_period_is_rejected(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    # No calculation/posting engine exists yet (Phases 5/6) — simulate a
    # POSTED period directly via raw SQL to exercise this phase's own
    # guard in isolation, exactly like the migration guard tests do for
    # the schema layer.
    db.execute(
        text(
            "INSERT INTO users (username, email, password_hash, full_name, is_active, "
            " created_at) VALUES ('poster', 'poster@test.local', 'x', 'Poster', true, now()) "
            "RETURNING id"
        )
    )
    user_id = db.execute(text("SELECT id FROM users WHERE username = 'poster'")).scalar_one()
    journal_entry_id = db.execute(
        text(
            "INSERT INTO journal_entries (journal_number, store_id, posting_date, entry_type, "
            " source_type, created_at) VALUES ('JE-TEST', :s, '2024-01-20', 'STANDARD', "
            "'MANUAL', now()) RETURNING id"
        ),
        {"s": store.id},
    ).scalar_one()
    db.execute(
        text(
            "UPDATE payroll_periods SET status = 'POSTED', calculated_by = :u, "
            "calculated_at = now(), approved_by = :u, approved_at = now(), "
            "journal_entry_id = :j, posted_by = :u, posted_at = now() WHERE id = :p"
        ),
        {"u": user_id, "j": journal_entry_id, "p": period.id},
    )
    db.commit()

    with pytest.raises(ConflictError):
        payroll_service.cancel_payroll_period(
            db,
            payroll_period_id=period.id,
            reason="Too late",
            actor_id=None,
            caller_store_id=None,
        )


def test_store_scoped_caller_cannot_create_period_for_another_store(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    with pytest.raises(ForbiddenError):
        payroll_service.create_payroll_period(
            db,
            PayrollPeriodInput(
                store_id=store_a.id,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 1, 15),
                pay_date=date(2024, 1, 20),
            ),
            actor_id=None,
            caller_store_id=store_b.id,
        )


def test_list_payroll_periods_is_store_scoped(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    period_a = _create(db, store_a)
    _create(db, store_b)

    results = payroll_service.list_payroll_periods(db, caller_store_id=store_a.id)
    assert [p.id for p in results] == [period_a.id]


# --- Approve staleness guard --------------------------------------------------


def test_approve_requires_calculated_status(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    with pytest.raises(ConflictError):
        payroll_service.approve_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )


def test_approve_requires_an_authenticated_actor(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    approver = make_user(db)
    db.commit()
    _mark_calculated(db, period.id, approver.id, "2024-01-16 00:00:00+00")
    db.commit()

    with pytest.raises(ValidationAppError):
        payroll_service.approve_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )


def test_approve_is_blocked_by_attendance_corrected_after_calculation(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    approver = make_user(db)
    db.commit()
    # Simulate a completed calculation (Phase 5 doesn't exist yet) via
    # raw SQL, exactly to exercise THIS phase's staleness-guard logic in
    # isolation.
    _mark_calculated(db, period.id, approver.id, "2024-01-16 00:00:00+00")
    employee_id = db.execute(
        text(
            "INSERT INTO employees (employee_number, legal_name, hire_date, created_at) "
            "VALUES ('EMP-STALE', 'Stale Test', '2024-01-01', now()) RETURNING id"
        )
    ).scalar_one()
    # An attendance record inside the period's date range, updated AFTER
    # calculated_at (simulated directly, since the ORM sets updated_at
    # via onupdate=func.now() on a real UPDATE).
    db.execute(
        text(
            "INSERT INTO attendance_records (employee_id, store_id, work_date, clock_in_at, "
            " clock_out_at, source, status, created_at, updated_at) "
            "VALUES (:e, :s, '2024-01-10', '2024-01-10 09:00:00+00', "
            "'2024-01-10 17:00:00+00', 'MANUAL', 'CLOSED', now(), '2024-01-17 00:00:00+00')"
        ),
        {"e": employee_id, "s": store.id},
    )
    db.commit()

    with pytest.raises(ConflictError):
        payroll_service.approve_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )


def test_approve_succeeds_when_no_attendance_is_newer_than_calculation(db: Session) -> None:
    store = make_store(db)
    period = _create(db, store)
    approver = make_user(db)
    db.commit()
    _mark_calculated(db, period.id, approver.id, "2024-01-16 00:00:00+00")
    employee_id = db.execute(
        text(
            "INSERT INTO employees (employee_number, legal_name, hire_date, created_at) "
            "VALUES ('EMP-FRESH', 'Fresh Test', '2024-01-01', now()) RETURNING id"
        )
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO attendance_records (employee_id, store_id, work_date, clock_in_at, "
            " clock_out_at, source, status, created_at, updated_at) "
            "VALUES (:e, :s, '2024-01-10', '2024-01-10 09:00:00+00', "
            "'2024-01-10 17:00:00+00', 'MANUAL', 'CLOSED', now(), '2024-01-10 17:00:00+00')"
        ),
        {"e": employee_id, "s": store.id},
    )
    db.commit()

    approved = payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert approved.status == "APPROVED"
    assert approved.approved_by == approver.id
    assert approved.approved_at is not None
