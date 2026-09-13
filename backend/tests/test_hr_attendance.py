"""M10 Phase 3: attendance service tests. Covers the required cross-
midnight scenarios (M10 approved decision #6: normal same-day shift,
midnight crossing, multi-hour overnight shift, ambiguous boundary,
store-specific configuration), clock-in/out lifecycle, corrections, and
the ACTIVE/ON_LEAVE-only clockability rule."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationAppError
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from tests.factories import make_store, unique_suffix


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


# --- resolve_work_date: the required cross-midnight scenarios --------------


def test_normal_same_day_shift_uses_the_local_calendar_date() -> None:
    clock_in = datetime(2024, 3, 1, 14, 0, tzinfo=UTC)  # 14:00 UTC
    work_date = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=0)
    assert work_date == date(2024, 3, 1)


def test_midnight_crossing_shift_with_zero_boundary_uses_clock_in_calendar_date() -> None:
    """With the default boundary (0 = no shifting), a shift starting at
    23:00 and ending after midnight still belongs to the CLOCK-IN date —
    duration is computed from real timestamps, never a naive same-day
    subtraction (M10_DESIGN.md Section 7)."""
    clock_in = datetime(2024, 3, 1, 23, 0, tzinfo=UTC)
    work_date = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=0)
    assert work_date == date(2024, 3, 1)


def test_multi_hour_overnight_shift_before_boundary_shifts_to_previous_day() -> None:
    """A store configured with attendance_day_boundary_hour=6: a clock-in
    at 01:00 local time belongs to YESTERDAY's work_date (the overnight
    shift that started the evening before)."""
    clock_in = datetime(2024, 3, 2, 1, 0, tzinfo=UTC)  # 01:00 UTC
    work_date = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=6)
    assert work_date == date(2024, 3, 1)


def test_ambiguous_boundary_exact_hour_does_not_shift() -> None:
    """A clock-in exactly AT the boundary hour is NOT before it — the
    comparison is strict less-than, so 06:00 with boundary_hour=6 belongs
    to today, not yesterday (the deterministic tie-break)."""
    clock_in = datetime(2024, 3, 2, 6, 0, tzinfo=UTC)
    work_date = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=6)
    assert work_date == date(2024, 3, 2)

    just_before = datetime(2024, 3, 2, 5, 59, tzinfo=UTC)
    assert hr_service.resolve_work_date(just_before, "UTC", day_boundary_hour=6) == date(2024, 3, 1)


def test_store_specific_configuration_changes_the_resolved_work_date() -> None:
    """The SAME instant resolves to a different work_date depending on
    the store's own configured boundary — proving the rule is genuinely
    store configuration, not a global constant (M10 approved decision
    #6)."""
    clock_in = datetime(2024, 3, 2, 2, 0, tzinfo=UTC)
    no_shift_store = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=0)
    late_boundary_store = hr_service.resolve_work_date(clock_in, "UTC", day_boundary_hour=6)
    assert no_shift_store == date(2024, 3, 2)
    assert late_boundary_store == date(2024, 3, 1)


def test_invalid_store_timezone_is_rejected_cleanly() -> None:
    clock_in = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationAppError):
        hr_service.resolve_work_date(clock_in, "Not/A_Real_Zone", day_boundary_hour=0)


# --- Clock-in / clock-out lifecycle -----------------------------------------


def test_clock_in_and_clock_out_happy_path(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)

    record = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    assert record.status == "OPEN"
    assert record.work_date == date(2024, 3, 1)

    closed = hr_service.clock_out(
        db,
        attendance_record_id=record.id,
        clock_out_at=datetime(2024, 3, 1, 17, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    assert closed.status == "CLOSED"
    assert closed.clock_out_at == datetime(2024, 3, 1, 17, 0, tzinfo=UTC)


def test_terminated_employee_cannot_clock_in(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="TERMINATED",
        effective_from=date(2024, 2, 1),
        reason="End of contract",
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ConflictError):
        hr_service.clock_in(
            db,
            employee_id=employee.id,
            store_id=store.id,
            clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
            actor_id=None,
            caller_store_id=None,
        )


def test_on_leave_employee_can_still_clock_in(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="ON_LEAVE",
        effective_from=date(2024, 2, 1),
        reason="Medical leave",
        actor_id=None,
        caller_store_id=None,
    )
    record = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    assert record.status == "OPEN"


def test_second_open_clock_in_without_clocking_out_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ConflictError):
        hr_service.clock_in(
            db,
            employee_id=employee.id,
            store_id=store.id,
            clock_in_at=datetime(2024, 3, 1, 10, 0, tzinfo=UTC),
            actor_id=None,
            caller_store_id=None,
        )


def test_clock_out_an_already_closed_record_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    record = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.clock_out(
        db,
        attendance_record_id=record.id,
        clock_out_at=datetime(2024, 3, 1, 17, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ConflictError):
        hr_service.clock_out(
            db,
            attendance_record_id=record.id,
            clock_out_at=datetime(2024, 3, 1, 18, 0, tzinfo=UTC),
            actor_id=None,
            caller_store_id=None,
        )


def test_store_scoped_caller_cannot_clock_in_at_another_store(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_a)
    with pytest.raises(ForbiddenError):
        hr_service.clock_in(
            db,
            employee_id=employee.id,
            store_id=store_a.id,
            clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
            actor_id=None,
            caller_store_id=store_b.id,
        )


# --- Corrections -------------------------------------------------------------


def test_correction_voids_original_and_supersedes_it(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    original = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.clock_out(
        db,
        attendance_record_id=original.id,
        clock_out_at=datetime(2024, 3, 1, 16, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )

    correction = hr_service.correct_attendance(
        db,
        attendance_record_id=original.id,
        new_clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        new_clock_out_at=datetime(2024, 3, 1, 17, 30, tzinfo=UTC),
        reason="Forgot to clock out on time",
        actor_id=None,
        caller_store_id=None,
    )

    db.refresh(original)
    assert original.status == "VOIDED"
    assert correction.correction_of_id == original.id
    assert correction.clock_out_at == datetime(2024, 3, 1, 17, 30, tzinfo=UTC)


def test_correction_without_reason_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    original = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ValidationAppError):
        hr_service.correct_attendance(
            db,
            attendance_record_id=original.id,
            new_clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
            new_clock_out_at=datetime(2024, 3, 1, 17, 0, tzinfo=UTC),
            reason="   ",
            actor_id=None,
            caller_store_id=None,
        )


def test_double_correction_of_the_same_record_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    original = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.correct_attendance(
        db,
        attendance_record_id=original.id,
        new_clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        new_clock_out_at=datetime(2024, 3, 1, 17, 0, tzinfo=UTC),
        reason="First correction",
        actor_id=None,
        caller_store_id=None,
    )
    with pytest.raises(ConflictError):
        hr_service.correct_attendance(
            db,
            attendance_record_id=original.id,
            new_clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
            new_clock_out_at=datetime(2024, 3, 1, 18, 0, tzinfo=UTC),
            reason="Second correction attempt",
            actor_id=None,
            caller_store_id=None,
        )


def test_list_attendance_records_excludes_voided_by_default(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    original = hr_service.clock_in(
        db,
        employee_id=employee.id,
        store_id=store.id,
        clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.correct_attendance(
        db,
        attendance_record_id=original.id,
        new_clock_in_at=datetime(2024, 3, 1, 9, 0, tzinfo=UTC),
        new_clock_out_at=datetime(2024, 3, 1, 17, 0, tzinfo=UTC),
        reason="Correction",
        actor_id=None,
        caller_store_id=None,
    )

    visible = hr_service.list_attendance_records(db, employee_id=employee.id)
    assert len(visible) == 1
    assert visible[0].correction_of_id == original.id

    with_voided = hr_service.list_attendance_records(
        db, employee_id=employee.id, include_voided=True
    )
    assert len(with_voided) == 2
