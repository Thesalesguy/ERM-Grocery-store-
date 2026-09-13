"""M10 Phase 2: employee master data, employment history, and
compensation service tests. Covers hire, status transitions, store
reassignment, compensation changes, RBAC store-scoping, and the
audit/privacy boundary (a compensation rate must never appear in
audit_logs)."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.db.session import SessionLocal
from app.modules.audit.models import AuditLog
from app.modules.hr import service as hr_service
from app.modules.hr.models import Employee
from app.modules.hr.service import DepartmentInput, EmployeeHireInput, PositionInput
from tests.factories import make_store, unique_suffix


def _hire(db: Session, store, **overrides) -> Employee:
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


# --- Department / Position ---------------------------------------------------


def test_create_department_and_duplicate_name_is_rejected(db: Session) -> None:
    name = f"Produce-{unique_suffix()}"
    hr_service.create_department(db, DepartmentInput(name=name))
    with pytest.raises(ConflictError):
        hr_service.create_department(db, DepartmentInput(name=name))


def test_create_position_with_invalid_department_is_rejected(db: Session) -> None:
    with pytest.raises(ValidationAppError):
        hr_service.create_position(db, PositionInput(title="Cashier", department_id=999_999_999))


# --- Hire --------------------------------------------------------------------


def test_hire_employee_creates_status_and_assignment_atomically(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)

    status = hr_service.get_current_status(db, employee.id)
    assignment = hr_service.get_current_assignment(db, employee.id)
    assert status is not None
    assert status.status == "ACTIVE"
    assert status.effective_to is None
    assert assignment is not None
    assert assignment.store_id == store.id
    assert assignment.effective_to is None


def test_duplicate_employee_number_is_a_clean_conflict_error(db: Session) -> None:
    store = make_store(db)
    number = f"EMP-{unique_suffix()}"
    _hire(db, store, employee_number=number)
    with pytest.raises(ConflictError):
        _hire(db, store, employee_number=number)


def test_hire_with_nonexistent_department_is_rejected(db: Session) -> None:
    store = make_store(db)
    with pytest.raises(ValidationAppError):
        _hire(db, store, department_id=999_999_999)


# --- Employment status transitions -------------------------------------------


def test_valid_status_transition_closes_prior_period(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)

    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="ON_LEAVE",
        effective_from=date(2024, 3, 1),
        reason="Parental leave",
        actor_id=None,
        caller_store_id=None,
    )
    history = hr_service.employment_status_history(db, employee.id)
    assert [h.status for h in history] == ["ACTIVE", "ON_LEAVE"]
    assert history[0].effective_to == date(2024, 2, 29)
    assert history[1].effective_to is None


def test_invalid_status_transition_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    with pytest.raises(ConflictError):
        # ACTIVE -> ACTIVE is not a listed transition.
        hr_service.change_employment_status(
            db,
            employee_id=employee.id,
            new_status="ACTIVE",
            effective_from=date(2024, 3, 1),
            reason=None,
            actor_id=None,
            caller_store_id=None,
        )


def test_terminate_then_rehire_cycle(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)

    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="TERMINATED",
        effective_from=date(2024, 6, 1),
        reason="Resignation",
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="ACTIVE",
        effective_from=date(2024, 9, 1),
        reason="Rehire",
        actor_id=None,
        caller_store_id=None,
    )
    history = hr_service.employment_status_history(db, employee.id)
    assert [h.status for h in history] == ["ACTIVE", "TERMINATED", "ACTIVE"]


def test_backdated_status_change_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store, hire_date=date(2024, 6, 1))
    with pytest.raises(ValidationAppError):
        hr_service.change_employment_status(
            db,
            employee_id=employee.id,
            new_status="ON_LEAVE",
            effective_from=date(2024, 1, 1),
            reason=None,
            actor_id=None,
            caller_store_id=None,
        )


# --- Reassignment + RBAC store-scoping --------------------------------------


def test_reassign_employee_to_a_new_store_closes_prior_assignment(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_a)

    hr_service.reassign_employee(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 7, 1),
        store_id=store_b.id,
        department_id=None,
        position_id=None,
        manager_employee_id=None,
        actor_id=None,
        caller_store_id=None,
    )
    history = hr_service.employment_assignment_history(db, employee.id)
    assert [a.store_id for a in history] == [store_a.id, store_b.id]
    assert history[0].effective_to == date(2024, 6, 30)


def test_store_scoped_caller_cannot_reassign_employee_out_of_their_store(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_a)

    with pytest.raises(ForbiddenError):
        hr_service.reassign_employee(
            db,
            employee_id=employee.id,
            effective_from=date(2024, 7, 1),
            store_id=store_b.id,
            department_id=None,
            position_id=None,
            manager_employee_id=None,
            actor_id=None,
            caller_store_id=store_b.id,
        )


def test_store_scoped_caller_cannot_change_status_of_another_stores_employee(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_a)

    with pytest.raises(ForbiddenError):
        hr_service.change_employment_status(
            db,
            employee_id=employee.id,
            new_status="ON_LEAVE",
            effective_from=date(2024, 3, 1),
            reason=None,
            actor_id=None,
            caller_store_id=store_b.id,
        )


def test_list_employees_is_store_scoped(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee_a = _hire(db, store_a)
    _hire(db, store_b)

    results = hr_service.list_employees(db, caller_store_id=store_a.id)
    assert [e.id for e in results] == [employee_a.id]


# --- Compensation + audit/privacy boundary ----------------------------------


def test_change_compensation_closes_prior_period(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)

    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="HOURLY",
        rate=Decimal("10.00"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=True,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 7, 1),
        pay_type="HOURLY",
        rate=Decimal("12.00"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=True,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    history = hr_service.compensation_history(db, employee.id)
    assert [c.rate for c in history] == [Decimal("10.00"), Decimal("12.00")]
    assert history[0].effective_to == date(2024, 6, 30)


def test_compensation_rate_never_appears_in_audit_log(db: Session) -> None:
    """M10 approved decision #5: salary amounts must never be written
    into ordinary audit metadata."""
    store = make_store(db)
    employee = _hire(db, store)

    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("123456.78"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    events = list(
        db.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "employee",
                AuditLog.entity_id == employee.id,
                AuditLog.action == "EMPLOYEE_COMPENSATION_CHANGED",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    payloads = [events[0].before_state, events[0].after_state]
    for payload in payloads:
        if payload is None:
            continue
        serialized = str(payload)
        assert "123456.78" not in serialized
        assert "rate" not in payload


# --- Concurrency: the row lock actually serializes -------------------------


def test_concurrent_reassignment_of_the_same_employee_serializes_via_row_lock() -> None:
    """Two threads try to reassign the SAME employee at the same
    effective_from. Without the Employee row lock, both could read the
    same "current assignment" and both attempt to close+replace it — a
    lost update or a raw IntegrityError race. With the lock, the second
    thread waits, then sees the first thread's already-committed new
    assignment and is cleanly rejected by the ordinary effective-date
    validation (not by a database race)."""
    setup = SessionLocal()
    try:
        store_a = make_store(setup)
        store_b = make_store(setup)
        store_c = make_store(setup)
        employee = _hire(setup, store_a)
        setup.commit()
        employee_id = employee.id
        store_b_id, store_c_id = store_b.id, store_c.id
    finally:
        setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(target_store_id: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            hr_service.reassign_employee(
                session,
                employee_id=employee_id,
                effective_from=date(2024, 7, 1),
                store_id=target_store_id,
                department_id=None,
                position_id=None,
                manager_employee_id=None,
                actor_id=None,
                caller_store_id=None,
            )
            results.append("SUCCESS")
        except (ConflictError, ValidationAppError, NotFoundError):
            session.rollback()
            results.append("REJECTED")
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=(store_b_id,))
    t2 = threading.Thread(target=attempt, args=(store_c_id,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert sorted(results) == ["REJECTED", "SUCCESS"]

    verify = SessionLocal()
    try:
        history = hr_service.employment_assignment_history(verify, employee_id)
        # Exactly one reassignment took effect — never both, never neither.
        assert len(history) == 2
        assert history[0].effective_to == date(2024, 6, 30)
        assert history[1].store_id in (store_b_id, store_c_id)
    finally:
        verify.close()
