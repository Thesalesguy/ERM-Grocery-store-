"""M11 Phase 7: labor & payroll analytics — proves POSTED-only scoping,
the employer-contribution-only labor cost definition, and reconciliation
against ACCOUNT_PAYROLL_PAYABLE, using M10's authoritative payroll
engine (never a second payroll computation).
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
from app.modules.payroll.service import PayrollPeriodInput
from app.modules.reports import service as reports_service
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


def _pay(db: Session, employee_id: int, rate=Decimal("2000.00")):
    hr_service.change_compensation(
        db,
        employee_id=employee_id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=rate,
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )


def _full_period_through_posted(db: Session, store, approver, **period_overrides):
    employee = _hire(db, store)
    _pay(db, employee.id)
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
    payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    return payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )


def test_headcount_by_store(db: Session) -> None:
    store = make_store(db)
    _hire(db, store)
    _hire(db, store)
    db.commit()

    rows = reports_service.headcount_by_store(db, store_ids=[store.id])
    row = next(r for r in rows if r.store_id == store.id)
    assert row.active_employee_count == 2


def test_payroll_cost_summary_excludes_non_posted_periods(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    _pay(db, employee.id)
    db.commit()
    payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 15),
            pay_date=date(2024, 2, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    summary = reports_service.payroll_cost_summary(
        db, store_ids=[store.id], period_start=date(2024, 1, 1), period_end=date(2024, 12, 31)
    )
    assert summary.gross_pay == Decimal("0")  # the DRAFT period must not count
    assert summary.pending_period_count == 1


def test_payroll_cost_summary_excludes_approved_but_not_posted_periods(db: Session) -> None:
    """DRAFT is not the only non-final status -- an APPROVED period has
    a real calculated total_gross sitting on the row (unlike DRAFT,
    where it's still zero), so this is the sharper version of the test
    above: it proves the POSTED-only filter, not merely a
    zero-vs-nonzero coincidence."""
    store = make_store(db)
    employee = _hire(db, store)
    _pay(db, employee.id)
    db.commit()
    approver = make_user(db, store)
    period = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 3, 1),
            period_end=date(2024, 3, 15),
            pay_date=date(2024, 3, 20),
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
    db.commit()
    assert approved.total_gross > Decimal("0")  # a real, nonzero calculated total

    summary = reports_service.payroll_cost_summary(
        db, store_ids=[store.id], period_start=date(2024, 1, 1), period_end=date(2024, 12, 31)
    )
    assert summary.gross_pay == Decimal("0")  # APPROVED is not POSTED -- must not count
    assert summary.pending_period_count == 1


def test_payroll_cost_summary_matches_the_posted_period_totals(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db, store)
    posted = _full_period_through_posted(db, store, approver)

    summary = reports_service.payroll_cost_summary(
        db, store_ids=[store.id], period_start=date(2024, 1, 1), period_end=date(2024, 1, 31)
    )
    assert summary.gross_pay == posted.total_gross
    assert summary.net_pay == posted.total_net_pay
    assert summary.labor_cost == posted.total_gross  # no employer contributions in this fixture
    assert summary.pending_period_count == 0


def test_payroll_gl_reconciliation_matches_posted_net_pay(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db, store)
    posted = _full_period_through_posted(db, store, approver)

    row = reports_service.payroll_gl_reconciliation(db, store_ids=[store.id])
    assert row.operational_value == posted.total_net_pay
    assert row.gl_balance == posted.total_net_pay
    assert row.discrepancy == Decimal("0")


def test_payroll_gl_reconciliation_ignores_a_second_approved_but_not_posted_period(
    db: Session,
) -> None:
    """A second period in the same store that only reaches APPROVED
    (never posted, so it never touched the GL) must not pollute the
    operational side of the reconciliation -- only POSTED periods have
    a real ACCOUNT_PAYROLL_PAYABLE entry to compare against."""
    store = make_store(db)
    approver = make_user(db, store)
    posted = _full_period_through_posted(db, store, approver)

    employee2 = _hire(db, store, employee_number=f"EMP-{unique_suffix()}")
    _pay(db, employee2.id)
    db.commit()
    period2 = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 15),
            pay_date=date(2024, 2, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period2.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period2.id, actor_id=approver.id, caller_store_id=None
    )
    approved2 = payroll_service.approve_payroll_period(
        db, payroll_period_id=period2.id, actor_id=approver.id, caller_store_id=None
    )
    db.commit()
    assert approved2.total_net_pay > Decimal("0")  # a real, nonzero amount that must NOT count

    row = reports_service.payroll_gl_reconciliation(db, store_ids=[store.id])
    assert row.operational_value == posted.total_net_pay
    assert row.gl_balance == posted.total_net_pay
    assert row.discrepancy == Decimal("0")


def test_labor_cost_percent_of_sales_handles_zero_sales(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db, store)
    _full_period_through_posted(db, store, approver)

    row = reports_service.labor_cost_percent_of_sales(
        db, store_ids=[store.id], period_start=date(2024, 1, 1), period_end=date(2024, 1, 31)
    )
    assert row.net_sales == Decimal("0")
    assert row.labor_cost_percent is None  # never a division-by-zero exception or a misleading 0
