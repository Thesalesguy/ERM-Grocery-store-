"""Employee master data and effective-dated employment history (M10 Phase
2). See docs/M10_DESIGN.md Sections 4-6 for the state machine, the
boundary-date rule, and the compensation model.

Concurrency: closing the current open period and inserting the new one
must never race with another concurrent change to the SAME employee's
history (two managers reassigning the same employee at once, say) — each
mutating function here locks the `Employee` row itself
(`SELECT ... FOR UPDATE`) before reading "the current open period",
exactly like `app.modules.purchasing.service.receive_goods` locks its
`PurchaseOrder` before mutating its items. This serializes all three
effective-dated dimensions (status, assignment, compensation) through one
lock per employee, which is deliberately coarse — these are rare HR
operations, not a POS hot path, so simplicity wins over fine-grained
per-dimension locking. The EXCLUDE constraints (app.modules.hr.models)
remain the ultimate backstop even if this lock were ever bypassed.

Audit/privacy boundary (M10 approved decision #5): every audit event
below carries identifiers and non-sensitive context (employee_id,
status, store_id, effective dates, actor) and NEVER a compensation rate
or any other monetary value — `change_compensation`'s audit event
records that a change happened, never what the new number is.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.audit import service as audit_service
from app.modules.hr.models import (
    EMPLOYMENT_STATUSES,
    CompensationPeriod,
    Department,
    Employee,
    EmploymentAssignment,
    EmploymentStatusPeriod,
    Position,
)

# M10_DESIGN.md Section 4's state machine. TERMINATED -> ACTIVE is a
# rehire (a new EmploymentStatusPeriod, same Employee.id/employee_number).
_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "ACTIVE": {"ON_LEAVE", "SUSPENDED", "TERMINATED"},
    "ON_LEAVE": {"ACTIVE", "TERMINATED"},
    "SUSPENDED": {"ACTIVE", "TERMINATED"},
    "TERMINATED": {"ACTIVE"},
}


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Mirrors app.modules.purchasing.service's own copy of this check —
    duplicated rather than imported so this module has no dependency on
    app.modules.auth and stays testable by calling its functions
    directly (same rationale as every other module in this codebase)."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


# --- Department / Position (global reference data) --------------------------


@dataclass(frozen=True)
class DepartmentInput:
    name: str


def create_department(
    db: Session, data: DepartmentInput, *, actor_id: int | None = None
) -> Department:
    department = Department(name=data.name)
    db.add(department)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Department {data.name!r} already exists", error_code="DUPLICATE_DEPARTMENT"
        ) from exc
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="DEPARTMENT_CREATED",
        entity_type="department",
        entity_id=department.id,
        after={"name": department.name},
    )
    db.commit()
    db.refresh(department)
    return department


def list_departments(db: Session, *, is_active: bool | None = None) -> list[Department]:
    query = select(Department).order_by(Department.name)
    if is_active is not None:
        query = query.where(Department.is_active == is_active)
    return list(db.execute(query).scalars().all())


@dataclass(frozen=True)
class PositionInput:
    title: str
    department_id: int | None = None


def create_position(db: Session, data: PositionInput, *, actor_id: int | None = None) -> Position:
    if data.department_id is not None and db.get(Department, data.department_id) is None:
        raise ValidationAppError(
            f"Department {data.department_id} does not exist", error_code="INVALID_DEPARTMENT"
        )
    position = Position(title=data.title, department_id=data.department_id)
    db.add(position)
    db.flush()
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="POSITION_CREATED",
        entity_type="position",
        entity_id=position.id,
        after={"title": position.title, "department_id": position.department_id},
    )
    db.commit()
    db.refresh(position)
    return position


def list_positions(db: Session, *, is_active: bool | None = None) -> list[Position]:
    query = select(Position).order_by(Position.title)
    if is_active is not None:
        query = query.where(Position.is_active == is_active)
    return list(db.execute(query).scalars().all())


# --- Employee identity + hire -----------------------------------------------


@dataclass(frozen=True)
class EmployeeHireInput:
    employee_number: str
    legal_name: str
    hire_date: date
    store_id: int
    display_name: str | None = None
    user_id: int | None = None
    department_id: int | None = None
    position_id: int | None = None
    manager_employee_id: int | None = None


def hire_employee(
    db: Session, data: EmployeeHireInput, *, actor_id: int | None, caller_store_id: int | None
) -> Employee:
    """Creates the Employee row plus its FIRST EmploymentStatusPeriod
    (ACTIVE, open-ended) and FIRST EmploymentAssignment (open-ended) —
    one atomic operation, since a bare Employee row with no status/
    assignment is not a meaningful state (M10_DESIGN.md Section 4: hire
    is the entry point into the state machine, not a separate step)."""
    _enforce_store_access(caller_store_id, data.store_id, "employee")
    if data.department_id is not None and db.get(Department, data.department_id) is None:
        raise ValidationAppError(
            f"Department {data.department_id} does not exist", error_code="INVALID_DEPARTMENT"
        )
    if data.position_id is not None and db.get(Position, data.position_id) is None:
        raise ValidationAppError(
            f"Position {data.position_id} does not exist", error_code="INVALID_POSITION"
        )
    if data.manager_employee_id is not None and db.get(Employee, data.manager_employee_id) is None:
        raise ValidationAppError(
            f"Manager employee {data.manager_employee_id} does not exist",
            error_code="INVALID_MANAGER",
        )

    employee = Employee(
        employee_number=data.employee_number,
        legal_name=data.legal_name,
        display_name=data.display_name,
        hire_date=data.hire_date,
        user_id=data.user_id,
    )
    db.add(employee)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Employee number {data.employee_number!r} is already in use",
            error_code="DUPLICATE_EMPLOYEE_NUMBER",
        ) from exc

    db.add(
        EmploymentStatusPeriod(
            employee_id=employee.id,
            status="ACTIVE",
            effective_from=data.hire_date,
            effective_to=None,
            reason="Hire",
            actor_id=actor_id,
        )
    )
    db.add(
        EmploymentAssignment(
            employee_id=employee.id,
            store_id=data.store_id,
            department_id=data.department_id,
            position_id=data.position_id,
            manager_employee_id=data.manager_employee_id,
            effective_from=data.hire_date,
            effective_to=None,
            actor_id=actor_id,
        )
    )
    db.flush()

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="EMPLOYEE_HIRED",
        entity_type="employee",
        entity_id=employee.id,
        after={
            "employee_number": employee.employee_number,
            "store_id": data.store_id,
            "hire_date": data.hire_date,
        },
    )
    db.commit()
    db.refresh(employee)
    return employee


def get_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise NotFoundError(f"Employee {employee_id} not found")
    return employee


def get_current_status(db: Session, employee_id: int) -> EmploymentStatusPeriod | None:
    return db.execute(
        select(EmploymentStatusPeriod).where(
            EmploymentStatusPeriod.employee_id == employee_id,
            EmploymentStatusPeriod.effective_to.is_(None),
        )
    ).scalar_one_or_none()


def get_current_assignment(db: Session, employee_id: int) -> EmploymentAssignment | None:
    return db.execute(
        select(EmploymentAssignment).where(
            EmploymentAssignment.employee_id == employee_id,
            EmploymentAssignment.effective_to.is_(None),
        )
    ).scalar_one_or_none()


def get_current_compensation(db: Session, employee_id: int) -> CompensationPeriod | None:
    return db.execute(
        select(CompensationPeriod).where(
            CompensationPeriod.employee_id == employee_id,
            CompensationPeriod.effective_to.is_(None),
        )
    ).scalar_one_or_none()


def list_employees(db: Session, *, caller_store_id: int | None) -> list[Employee]:
    """Store-scoped via the employee's CURRENT assignment — a store-
    scoped caller sees only employees currently assigned to their store,
    never an employee who once worked there historically."""
    query = select(Employee).join(
        EmploymentAssignment,
        (EmploymentAssignment.employee_id == Employee.id)
        & (EmploymentAssignment.effective_to.is_(None)),
    )
    if caller_store_id is not None:
        query = query.where(EmploymentAssignment.store_id == caller_store_id)
    query = query.order_by(Employee.legal_name)
    return list(db.execute(query).scalars().all())


def employment_status_history(db: Session, employee_id: int) -> list[EmploymentStatusPeriod]:
    return list(
        db.execute(
            select(EmploymentStatusPeriod)
            .where(EmploymentStatusPeriod.employee_id == employee_id)
            .order_by(EmploymentStatusPeriod.effective_from)
        )
        .scalars()
        .all()
    )


def employment_assignment_history(db: Session, employee_id: int) -> list[EmploymentAssignment]:
    return list(
        db.execute(
            select(EmploymentAssignment)
            .where(EmploymentAssignment.employee_id == employee_id)
            .order_by(EmploymentAssignment.effective_from)
        )
        .scalars()
        .all()
    )


def compensation_history(db: Session, employee_id: int) -> list[CompensationPeriod]:
    return list(
        db.execute(
            select(CompensationPeriod)
            .where(CompensationPeriod.employee_id == employee_id)
            .order_by(CompensationPeriod.effective_from)
        )
        .scalars()
        .all()
    )


# --- Effective-dated mutations: status / assignment / compensation ---------


def _lock_employee(db: Session, employee_id: int) -> Employee:
    employee = db.execute(
        select(Employee).where(Employee.id == employee_id).with_for_update()
    ).scalar_one_or_none()
    if employee is None:
        raise NotFoundError(f"Employee {employee_id} not found")
    return employee


def _enforce_store_access_via_current_assignment(
    db: Session, employee_id: int, caller_store_id: int | None
) -> None:
    """Fails CLOSED: a store-scoped caller with no way to verify which
    store an employee belongs to (no current assignment row — should
    never happen for a properly-hired employee, but this must not be a
    silent bypass if it ever does) is denied, not allowed through. An
    unscoped caller (caller_store_id is None — Admin/HR Clerk with
    cross-store visibility) is unaffected either way."""
    if caller_store_id is None:
        return
    current_assignment = get_current_assignment(db, employee_id)
    if current_assignment is None:
        raise ForbiddenError(
            f"Employee {employee_id} has no current store assignment to verify access " "against",
            error_code="STORE_ACCESS_DENIED",
        )
    _enforce_store_access(caller_store_id, current_assignment.store_id, "employee")


def change_employment_status(
    db: Session,
    *,
    employee_id: int,
    new_status: str,
    effective_from: date,
    reason: str | None,
    actor_id: int | None,
    caller_store_id: int | None,
) -> EmploymentStatusPeriod:
    if new_status not in EMPLOYMENT_STATUSES:
        raise ValidationAppError(
            f"Invalid employment status {new_status!r}", error_code="INVALID_STATUS"
        )

    _lock_employee(db, employee_id)
    _enforce_store_access_via_current_assignment(db, employee_id, caller_store_id)

    current = get_current_status(db, employee_id)
    if current is None:
        raise ConflictError(
            f"Employee {employee_id} has no current employment status period — "
            "was this employee ever hired?",
            error_code="NO_CURRENT_STATUS",
        )
    if new_status not in _VALID_STATUS_TRANSITIONS.get(current.status, set()):
        raise ConflictError(
            f"Cannot transition from {current.status} to {new_status}",
            error_code="INVALID_STATUS_TRANSITION",
        )
    if effective_from <= current.effective_from:
        raise ValidationAppError(
            "effective_from must be after the current period's effective_from "
            f"({current.effective_from})",
            error_code="INVALID_EFFECTIVE_DATE",
        )

    current.effective_to = effective_from - timedelta(days=1)
    db.flush()

    new_period = EmploymentStatusPeriod(
        employee_id=employee_id,
        status=new_status,
        effective_from=effective_from,
        effective_to=None,
        reason=reason,
        actor_id=actor_id,
    )
    db.add(new_period)
    db.flush()

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="EMPLOYMENT_STATUS_CHANGED",
        entity_type="employee",
        entity_id=employee_id,
        before={"status": current.status},
        after={"status": new_status, "effective_from": effective_from},
    )
    db.commit()
    db.refresh(new_period)
    return new_period


def reassign_employee(
    db: Session,
    *,
    employee_id: int,
    effective_from: date,
    store_id: int,
    department_id: int | None,
    position_id: int | None,
    manager_employee_id: int | None,
    actor_id: int | None,
    caller_store_id: int | None,
) -> EmploymentAssignment:
    _lock_employee(db, employee_id)
    current = get_current_assignment(db, employee_id)
    if current is None:
        raise ConflictError(
            f"Employee {employee_id} has no current assignment — was this employee ever hired?",
            error_code="NO_CURRENT_ASSIGNMENT",
        )
    # Both the employee's CURRENT store and the NEW store must be
    # reachable by the caller — a store-scoped manager may not reassign
    # an employee out of a store they don't manage, nor into one.
    _enforce_store_access(caller_store_id, current.store_id, "employee")
    _enforce_store_access(caller_store_id, store_id, "employee")

    if department_id is not None and db.get(Department, department_id) is None:
        raise ValidationAppError(
            f"Department {department_id} does not exist", error_code="INVALID_DEPARTMENT"
        )
    if position_id is not None and db.get(Position, position_id) is None:
        raise ValidationAppError(
            f"Position {position_id} does not exist", error_code="INVALID_POSITION"
        )
    if manager_employee_id is not None and db.get(Employee, manager_employee_id) is None:
        raise ValidationAppError(
            f"Manager employee {manager_employee_id} does not exist",
            error_code="INVALID_MANAGER",
        )
    if effective_from <= current.effective_from:
        raise ValidationAppError(
            "effective_from must be after the current assignment's effective_from "
            f"({current.effective_from})",
            error_code="INVALID_EFFECTIVE_DATE",
        )

    current.effective_to = effective_from - timedelta(days=1)
    db.flush()

    new_assignment = EmploymentAssignment(
        employee_id=employee_id,
        store_id=store_id,
        department_id=department_id,
        position_id=position_id,
        manager_employee_id=manager_employee_id,
        effective_from=effective_from,
        effective_to=None,
        actor_id=actor_id,
    )
    db.add(new_assignment)
    db.flush()

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="EMPLOYEE_REASSIGNED",
        entity_type="employee",
        entity_id=employee_id,
        before={"store_id": current.store_id},
        after={"store_id": store_id, "effective_from": effective_from},
    )
    db.commit()
    db.refresh(new_assignment)
    return new_assignment


def change_compensation(
    db: Session,
    *,
    employee_id: int,
    effective_from: date,
    pay_type: str,
    rate: Decimal,
    pay_frequency: str,
    overtime_eligible: bool,
    currency: str,
    actor_id: int | None,
    caller_store_id: int | None,
) -> CompensationPeriod:
    _lock_employee(db, employee_id)
    _enforce_store_access_via_current_assignment(db, employee_id, caller_store_id)

    current = get_current_compensation(db, employee_id)
    if current is not None and effective_from <= current.effective_from:
        raise ValidationAppError(
            "effective_from must be after the current compensation period's "
            f"effective_from ({current.effective_from})",
            error_code="INVALID_EFFECTIVE_DATE",
        )

    if current is not None:
        current.effective_to = effective_from - timedelta(days=1)
        db.flush()

    new_period = CompensationPeriod(
        employee_id=employee_id,
        pay_type=pay_type,
        rate=rate,
        pay_frequency=pay_frequency,
        overtime_eligible=overtime_eligible,
        currency=currency,
        effective_from=effective_from,
        effective_to=None,
        actor_id=actor_id,
    )
    db.add(new_period)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            "Invalid compensation period (overlapping range or invalid pay type/frequency)",
            error_code="INVALID_COMPENSATION_PERIOD",
        ) from exc

    # M10 approved decision #5: the compensation RATE is never written
    # into audit_logs — only the fact that a change happened.
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="EMPLOYEE_COMPENSATION_CHANGED",
        entity_type="employee",
        entity_id=employee_id,
        after={"effective_from": effective_from, "pay_type": pay_type},
    )
    db.commit()
    db.refresh(new_period)
    return new_period
