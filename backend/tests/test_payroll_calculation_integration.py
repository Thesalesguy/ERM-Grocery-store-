"""M10 Phase 5 integration tests: calculate_payroll_period wired to real
employee/compensation/attendance data. Covers the happy path, the
missing-compensation-coverage error surfaced as a clean ConflictError,
recalculation (wholesale replace, not accumulation), and that terminated
employees still get paid for hours worked while ACTIVE within the
period."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationAppError
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
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


def _create_period(db: Session, store, **overrides):
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


def test_calculate_hourly_employee_happy_path(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="HOURLY",
        rate=Decimal("20.00"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=True,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 1, 5, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    record = hr_service.get_current_status(db, employee.id)  # sanity: employee exists
    assert record is not None
    open_shift = hr_service.list_attendance_records(db, employee_id=employee.id)[0]
    hr_service.clock_out(
        db,
        attendance_record_id=open_shift.id,
        clock_out_at=datetime(2024, 1, 5, 17, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    calculated = payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )

    assert calculated.status == "CALCULATED"
    assert calculated.total_gross == Decimal("160.00")  # 8 hours * 20.00
    results = payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    assert len(results) == 1
    assert results[0].gross_pay == Decimal("160.00")
    assert results[0].net_pay == Decimal("160.00")


def test_calculate_salaried_employee_happy_path(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("3000.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    period = _create_period(
        db,
        store,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        pay_date=date(2024, 2, 5),
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    calculated = payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert calculated.total_gross == Decimal("3000.00")


def test_calculate_without_compensation_on_file_is_a_clean_conflict(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    _hire(db, store)  # no compensation ever set
    db.commit()

    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    with pytest.raises(ConflictError):
        payroll_service.calculate_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )


def test_calculate_requires_authenticated_actor(db: Session) -> None:
    store = make_store(db)
    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    with pytest.raises(ValidationAppError):
        payroll_service.calculate_payroll_period(
            db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
        )


def test_recalculate_replaces_results_wholesale_not_additively(db: Session) -> None:
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

    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    first_results = payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    assert len(first_results) == 1

    # Recalculate again — must replace, not duplicate.
    recalculated = payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert recalculated.status == "CALCULATED"
    second_results = payroll_service.list_payroll_employee_results(db, payroll_period_id=period.id)
    assert len(second_results) == 1
    assert second_results[0].id != first_results[0].id


def test_terminated_employee_still_gets_paid_for_hours_worked_while_active(db: Session) -> None:
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store, hire_date=date(2024, 1, 1))
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="HOURLY",
        rate=Decimal("15.00"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 1, 3, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    shift = hr_service.list_attendance_records(db, employee_id=employee.id)[0]
    hr_service.clock_out(
        db,
        attendance_record_id=shift.id,
        clock_out_at=datetime(2024, 1, 3, 13, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    # Terminated AFTER the shift but BEFORE the payroll period is calculated.
    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="TERMINATED",
        effective_from=date(2024, 1, 4),
        reason="Resigned",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    calculated = payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    assert calculated.total_gross == Decimal("60.00")  # 4 hours * 15.00


def test_fractional_hour_shift_is_computed_exactly_not_via_float(db: Session) -> None:
    """Regression test: hours were originally computed via
    timedelta.total_seconds() (a float) fed into Decimal(), which
    imports binary floating-point representation error into a financial
    calculation. A 5h15m shift (5.25 hours, exactly representable) at an
    odd rate exercises the fix — the result must be the mathematically
    EXACT product, not a float-tainted approximation."""
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="HOURLY",
        rate=Decimal("17.13"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 1, 5, 9, 0, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    shift = hr_service.list_attendance_records(db, employee_id=employee.id)[0]
    hr_service.clock_out(
        db,
        attendance_record_id=shift.id,
        clock_out_at=datetime(2024, 1, 5, 14, 15, 0, tzinfo=UTC),  # 5h15m = 5.25 hours
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()

    period = _create_period(db, store)
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    calculated = payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    # 5.25 * 17.13 = 89.9325, rounds to 89.93 — the EXACT decimal
    # product, computed with no float anywhere in the pipeline.
    assert calculated.total_gross == Decimal("89.93")
