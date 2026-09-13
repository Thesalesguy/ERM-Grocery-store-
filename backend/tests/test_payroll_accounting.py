"""M10 Phase 6: payroll accounting integration tests. Covers the full
lifecycle through POSTED (real GL entry, balanced by construction),
idempotent re-posting, reversal (a compensating entry, PayrollPeriod.status
never changes), double-reversal rejection, and the payroll-to-GL
reconciliation invariant."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationAppError
from app.modules.accounting.models import JournalEntry, JournalLine
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
from app.modules.payroll.models import DeductionRate, DeductionType, PayrollReversal
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


def _full_period_through_calculated(db: Session, store, approver, **period_overrides):
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


def _journal_lines_for(db: Session, journal_entry_id: int) -> list[JournalLine]:
    return list(
        db.execute(
            select(JournalLine).where(JournalLine.journal_entry_id == journal_entry_id)
        ).scalars()
    )


def test_post_creates_a_balanced_journal_entry(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)

    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert posted.status == "POSTED"
    assert posted.journal_entry_id is not None

    entry = db.get(JournalEntry, posted.journal_entry_id)
    assert entry is not None
    assert entry.source_type == "PAYROLL_POSTING"
    assert entry.source_id == posted.id

    lines = _journal_lines_for(db, entry.id)
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) > 0


def test_post_flips_employee_results_to_final(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    results = payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    assert all(r.status == "FINAL" for r in results)


def test_post_is_idempotent_by_state(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    first = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    second = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert first.journal_entry_id == second.journal_entry_id


def test_post_requires_approved_status(db: Session) -> None:
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
    with pytest.raises(ConflictError):
        payroll_service.post_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )


def test_post_requires_authenticated_actor(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    with pytest.raises(ValidationAppError):
        payroll_service.post_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )


# --- Reversal ----------------------------------------------------------------


def test_reverse_creates_a_compensating_entry_and_never_changes_status(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    original_entry = db.get(JournalEntry, posted.journal_entry_id)
    original_lines = {
        (line.account_id, line.debit, line.credit)
        for line in _journal_lines_for(db, original_entry.id)
    }

    reversed_period = payroll_service.reverse_payroll_period(
        db,
        payroll_period_id=period.id,
        reason="Duplicate payroll run",
        actor_id=approver.id,
        caller_store_id=None,
    )
    # POSTED forever — reversal is a new row, never a status flip.
    assert reversed_period.status == "POSTED"

    reversal_row = db.execute(
        select(PayrollReversal).where(PayrollReversal.payroll_period_id == period.id)
    ).scalar_one()
    reversal_entry = db.get(JournalEntry, reversal_row.reversal_journal_entry_id)
    assert reversal_entry.source_type == "PAYROLL_REVERSAL"
    reversal_lines = {
        (line.account_id, line.debit, line.credit)
        for line in _journal_lines_for(db, reversal_entry.id)
    }
    # Every debit/credit swapped relative to the original.
    swapped = {(account_id, credit, debit) for account_id, debit, credit in original_lines}
    assert reversal_lines == swapped


def test_reverse_requires_posted_status(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    with pytest.raises(ConflictError):
        payroll_service.reverse_payroll_period(
            db,
            payroll_period_id=period.id,
            reason="Too early",
            actor_id=approver.id,
            caller_store_id=None,
        )


def test_reverse_requires_a_reason(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    with pytest.raises(ValidationAppError):
        payroll_service.reverse_payroll_period(
            db,
            payroll_period_id=period.id,
            reason="   ",
            actor_id=approver.id,
            caller_store_id=None,
        )


def test_double_reversal_is_idempotent_not_a_second_entry(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    payroll_service.reverse_payroll_period(
        db, payroll_period_id=period.id, reason="First", actor_id=approver.id, caller_store_id=None
    )
    payroll_service.reverse_payroll_period(
        db,
        payroll_period_id=period.id,
        reason="Second attempt",
        actor_id=approver.id,
        caller_store_id=None,
    )
    reversal_count = (
        db.execute(select(PayrollReversal).where(PayrollReversal.payroll_period_id == period.id))
        .scalars()
        .all()
    )
    assert len(reversal_count) == 1


# --- Reconciliation ------------------------------------------------------


def test_payroll_to_gl_reconciliation(db: Session) -> None:
    """M10_DESIGN.md Section 11: walk the chain forward from the leaf
    lines and prove it equals the journal amounts, rather than the
    'sum unrelated tables and hope' anti-pattern."""
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_calculated(db, store, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )

    results = payroll_service.list_payroll_employee_results(db, payroll_period_id=posted.id)
    sum_gross = sum((r.gross_pay for r in results), Decimal("0"))
    sum_net = sum((r.net_pay for r in results), Decimal("0"))

    lines = _journal_lines_for(db, posted.journal_entry_id)
    total_debit = sum(line.debit for line in lines)
    total_credit = sum(line.credit for line in lines)
    assert total_debit == total_credit
    assert total_debit == sum_gross
    assert posted.total_net_pay == sum_net


def test_multi_employee_period_with_deductions_and_employer_contributions_balances(
    db: Session,
) -> None:
    """One journal entry per PERIOD, not per employee (M10_DESIGN.md
    Section 10) — aggregates two employees, one with an employee
    deduction and an employer contribution, and still balances exactly."""
    store = make_store(db)
    approver = make_user(db)

    health = DeductionType(
        code=f"HEALTH-{unique_suffix()}", name="Health Insurance", category="EMPLOYEE"
    )
    retirement = DeductionType(
        code=f"RETIRE-{unique_suffix()}",
        name="Employer Retirement Match",
        category="EMPLOYER_CONTRIBUTION",
    )
    db.add_all([health, retirement])
    db.flush()
    db.add_all(
        [
            DeductionRate(
                deduction_type_id=health.id,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "5.00"},
                effective_from=date(2024, 1, 1),
            ),
            DeductionRate(
                deduction_type_id=retirement.id,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "3.00"},
                effective_from=date(2024, 1, 1),
            ),
        ]
    )
    db.commit()

    employee_1 = _hire(db, store)
    employee_2 = _hire(db, store)
    for employee in (employee_1, employee_2):
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
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    approved = payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=approved.id, actor_id=approver.id, caller_store_id=None
    )

    results = payroll_service.list_payroll_employee_results(db, payroll_period_id=posted.id)
    assert len(results) == 2
    assert posted.total_gross == Decimal("4000.00")
    assert posted.total_deductions == Decimal("200.00")  # 5% of 4000
    assert posted.total_employer_contributions == Decimal("120.00")  # 3% of 4000
    assert posted.total_net_pay == Decimal("3800.00")

    lines = _journal_lines_for(db, posted.journal_entry_id)
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) == Decimal("4120.00")  # gross + employer contrib
