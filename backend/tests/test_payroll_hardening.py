"""M10 Phases 7-10 hardening pass: transaction-boundary, concurrency,
failure-injection, and idempotency tests over the already-built payroll
lifecycle/calculation/accounting-integration functions. Mirrors the
discipline established by the M9 hardening pass — real concurrent
connections (never the `db` fixture's savepoint isolation) for the races,
and a genuine forced-failure for the transaction-atomicity proofs."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.accounting.models import JournalEntry, JournalLine
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
from app.modules.payroll.models import PayrollEmployeeResult, PayrollReversal
from app.modules.payroll.service import PayrollPeriodInput
from tests.factories import make_store, make_user, unique_suffix


def _hire(db: Session, store, **overrides):
    defaults = dict(
        employee_number=f"EMP-{unique_suffix()}",
        legal_name="Jane Doe",
        hire_date=date(2024, 1, 1),
        store_id=store.id,
    )
    defaults.update(overrides)
    return hr_service.hire_employee(
        db, EmployeeHireInput(**defaults), actor_id=None, caller_store_id=None
    )


def _full_period_through_approved(db: Session, store, approver, **period_overrides):
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("2000.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    defaults = dict(
        store_id=store.id,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        pay_date=date(2024, 1, 20),
    )
    defaults.update(period_overrides)
    period = payroll_service.create_payroll_period(
        db, PayrollPeriodInput(**defaults), actor_id=None, caller_store_id=None
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    return payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )


# --- Idempotency: explicit client_transaction_id fast path ------------------


def test_calculate_retry_with_same_client_transaction_id_does_not_recompute(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("2000.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    period = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            pay_date=date(2024, 1, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    txn_id = f"calc-{unique_suffix()}"
    first = payroll_service.calculate_payroll_period(
        db,
        payroll_period_id=period.id,
        actor_id=approver.id,
        caller_store_id=None,
        client_transaction_id=txn_id,
    )
    first_result_ids = {
        r.id for r in payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    }

    # A second raise in compensation AFTER the first calculation — if the
    # retry actually recalculated, the new gross would differ.
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 16),
        pay_type="SALARY",
        rate=Decimal("5000.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    retried = payroll_service.calculate_payroll_period(
        db,
        payroll_period_id=period.id,
        actor_id=approver.id,
        caller_store_id=None,
        client_transaction_id=txn_id,
    )
    assert retried.total_gross == first.total_gross
    retried_result_ids = {
        r.id for r in payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    }
    assert retried_result_ids == first_result_ids  # nothing was deleted/reinserted


def test_post_retry_with_same_client_transaction_id_returns_the_same_journal_entry(
    db: Session,
) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)
    txn_id = f"post-{unique_suffix()}"
    first = payroll_service.post_payroll_period(
        db,
        payroll_period_id=period.id,
        actor_id=approver.id,
        caller_store_id=None,
        client_transaction_id=txn_id,
    )
    second = payroll_service.post_payroll_period(
        db,
        payroll_period_id=period.id,
        actor_id=approver.id,
        caller_store_id=None,
        client_transaction_id=txn_id,
    )
    assert first.journal_entry_id == second.journal_entry_id
    entry_count = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PAYROLL_POSTING",
                JournalEntry.source_id == period.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(entry_count) == 1


# --- Concurrency: real independent connections -------------------------


def test_concurrent_post_of_the_same_period_creates_exactly_one_journal_entry() -> None:
    """The highest-stakes payroll race: two callers both try to post the
    SAME approved period at once. Exactly one journal entry must be
    created — proven against real independent connections, not the `db`
    fixture's savepoint isolation (mirrors
    tests/test_purchasing_concurrency.py's own module docstring for why)."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        approver = make_user(setup)
        period = _full_period_through_approved(setup, store, approver)
        setup.commit()
        period_id = period.id
        approver_id = approver.id
    finally:
        setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            payroll_service.post_payroll_period(
                session,
                payroll_period_id=period_id,
                actor_id=approver_id,
                caller_store_id=None,
            )
            results.append("SUCCESS")
        except Exception:
            session.rollback()
            results.append("REJECTED")
        finally:
            session.close()

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Both should report SUCCESS (post is idempotent-by-state — the
    # second caller, after waiting on the row lock, sees POSTED and
    # returns cleanly rather than erroring) — but only ONE journal entry
    # may exist.
    assert results == ["SUCCESS", "SUCCESS"]
    verify = SessionLocal()
    try:
        entries = list(
            verify.execute(
                select(JournalEntry).where(
                    JournalEntry.source_type == "PAYROLL_POSTING",
                    JournalEntry.source_id == period_id,
                )
            ).scalars()
        )
        assert len(entries) == 1
    finally:
        verify.close()


def test_concurrent_calculate_of_the_same_period_never_leaves_duplicate_results() -> None:
    """Two concurrent (re)calculate calls for the same period must never
    leave two generations of PayrollEmployeeResult rows behind — the row
    lock serializes them, and wholesale delete-then-reinsert means the
    second caller's generation completely supersedes the first's."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        approver = make_user(setup)
        employee = hr_service.hire_employee(
            setup,
            EmployeeHireInput(
                employee_number=f"EMP-{unique_suffix()}",
                legal_name="Concurrency Test",
                hire_date=date(2024, 1, 1),
                store_id=store.id,
            ),
            actor_id=None,
            caller_store_id=None,
        )
        hr_service.change_compensation(
            setup,
            employee_id=employee.id,
            effective_from=date(2024, 1, 1),
            pay_type="SALARY",
            rate=Decimal("2000.00"),
            pay_frequency="MONTHLY",
            overtime_eligible=False,
            currency="USD",
            actor_id=None,
            caller_store_id=None,
        )
        period = payroll_service.create_payroll_period(
            setup,
            PayrollPeriodInput(
                store_id=store.id,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 1, 15),
                pay_date=date(2024, 1, 20),
            ),
            actor_id=None,
            caller_store_id=None,
        )
        payroll_service.open_payroll_period(
            setup, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )
        setup.commit()
        period_id = period.id
        approver_id = approver.id
    finally:
        setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            payroll_service.calculate_payroll_period(
                session,
                payroll_period_id=period_id,
                actor_id=approver_id,
                caller_store_id=None,
            )
            results.append("SUCCESS")
        except Exception:
            session.rollback()
            results.append("REJECTED")
        finally:
            session.close()

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results == ["SUCCESS", "SUCCESS"]
    verify = SessionLocal()
    try:
        result_rows = list(
            verify.execute(
                select(PayrollEmployeeResult).where(
                    PayrollEmployeeResult.payroll_period_id == period_id
                )
            ).scalars()
        )
        # Exactly one result per employee — never two generations left
        # behind by an unserialized race.
        assert len(result_rows) == 1
    finally:
        verify.close()


# --- Failure injection: transaction atomicity ---------------------------


def test_failure_during_posting_leaves_no_partial_state(db: Session, monkeypatch) -> None:
    """A forced failure INSIDE post_payroll_period, after the journal
    entry is created but before the final commit, must roll back
    EVERYTHING — the period must still be APPROVED (not POSTED), and no
    journal entry may exist. This is the same "atomic with the
    operational event" guarantee proven for every other financial
    workflow in this codebase (docs/M4_ACCOUNTING_CORE.md's own posting
    convention)."""
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)

    from app.modules.audit import service as audit_service

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure after journal posting, before commit")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError):
        payroll_service.post_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )
    db.rollback()

    reloaded = payroll_service.get_payroll_period(db, period.id)
    assert reloaded.status == "APPROVED"
    assert reloaded.journal_entry_id is None
    entries = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PAYROLL_POSTING",
                JournalEntry.source_id == period.id,
            )
        )
        .scalars()
        .all()
    )
    assert entries == []


def test_failure_during_reversal_leaves_no_partial_state(db: Session, monkeypatch) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )

    from app.modules.audit import service as audit_service

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure after reversal journal posting, before commit")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError):
        payroll_service.reverse_payroll_period(
            db,
            payroll_period_id=posted.id,
            reason="Testing failure injection",
            actor_id=approver.id,
            caller_store_id=None,
        )
    db.rollback()

    reversal_rows = (
        db.execute(select(PayrollReversal).where(PayrollReversal.payroll_period_id == posted.id))
        .scalars()
        .all()
    )
    assert reversal_rows == []
    # The original posting must be completely untouched.
    reloaded = payroll_service.get_payroll_period(db, posted.id)
    assert reloaded.status == "POSTED"
    original_lines = (
        db.execute(
            select(JournalLine).where(JournalLine.journal_entry_id == posted.journal_entry_id)
        )
        .scalars()
        .all()
    )
    assert len(original_lines) > 0


def test_failure_during_calculation_leaves_previous_results_intact(
    db: Session, monkeypatch
) -> None:
    """A failed RECALCULATE (after the old results were deleted, before
    the new ones fully committed) must roll back to the ORIGINAL
    results, never leave the period with zero results."""
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)
    original_results = payroll_service.list_payroll_employee_results(
        db, payroll_period_id=period.id
    )
    original_ids = {r.id for r in original_results}
    assert original_ids

    # Move back to CALCULATED so recalculate is reachable (approve moved
    # it to APPROVED already).
    from app.modules.payroll.models import PayrollPeriod as _PP

    db.execute(_PP.__table__.update().where(_PP.id == period.id).values(status="CALCULATED"))
    db.commit()

    from app.modules.audit import service as audit_service

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure during recalculation, after delete+reinsert")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError):
        payroll_service.calculate_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )
    db.rollback()

    reloaded_results = payroll_service.list_payroll_employee_results(
        db, payroll_period_id=period.id
    )
    reloaded_ids = {r.id for r in reloaded_results}
    assert reloaded_ids == original_ids
