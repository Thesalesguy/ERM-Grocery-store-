"""HR/workforce: employee master data, effective-dated employment
history, and attendance. See docs/M10_DESIGN.md and
app.modules.auth.permissions for the full RBAC rationale.

`hr.read`/`hr.write` gate employee identity, employment status, and
assignment. `hr.compensation.write` is a SEPARATE, narrower permission
gating both writing AND reading compensation (M10 approved decision #3:
HR Clerk's scope never includes compensation) — `hr.read` alone does not
imply visibility into pay rates. `attendance.read`/`attendance.write`
gate clock-in/out and corrections.
"""

from datetime import date

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import ForbiddenError
from app.modules.auth.permissions import (
    ATTENDANCE_READ,
    ATTENDANCE_WRITE,
    HR_COMPENSATION_WRITE,
    HR_READ,
    HR_WRITE,
)
from app.modules.auth.service import CurrentUser, enforce_store_access, require_permission
from app.modules.hr import service
from app.modules.hr.schemas import (
    AttendanceCorrectionInput,
    AttendanceRecordRead,
    ClockInInput,
    ClockOutInput,
    CompensationChangeInput,
    CompensationPeriodRead,
    DepartmentCreate,
    DepartmentRead,
    EmployeeDetail,
    EmployeeRead,
    EmploymentAssignmentRead,
    EmploymentReassignmentInput,
    EmploymentStatusChangeInput,
    EmploymentStatusPeriodRead,
    PositionCreate,
    PositionRead,
)
from app.modules.hr.schemas import (
    EmployeeHireInput as EmployeeHireSchema,
)
from app.modules.hr.service import (
    DepartmentInput,
    EmployeeHireInput,
    PositionInput,
)

router = APIRouter(prefix="/hr", tags=["hr"])

_read_permission = require_permission(HR_READ)
_write_permission = require_permission(HR_WRITE)
_compensation_permission = require_permission(HR_COMPENSATION_WRITE)
_attendance_read_permission = require_permission(ATTENDANCE_READ)
_attendance_write_permission = require_permission(ATTENDANCE_WRITE)


# --- Departments / Positions -------------------------------------------------


@router.post("/departments", response_model=DepartmentRead, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> DepartmentRead:
    row = service.create_department(
        db, DepartmentInput(name=payload.name), actor_id=current_user.id
    )
    return DepartmentRead.model_validate(row)


@router.get("/departments", response_model=list[DepartmentRead])
def list_departments(
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[DepartmentRead]:
    rows = service.list_departments(db, is_active=is_active)
    return [DepartmentRead.model_validate(row) for row in rows]


@router.post("/positions", response_model=PositionRead, status_code=status.HTTP_201_CREATED)
def create_position(
    payload: PositionCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> PositionRead:
    row = service.create_position(
        db,
        PositionInput(title=payload.title, department_id=payload.department_id),
        actor_id=current_user.id,
    )
    return PositionRead.model_validate(row)


@router.get("/positions", response_model=list[PositionRead])
def list_positions(
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[PositionRead]:
    rows = service.list_positions(db, is_active=is_active)
    return [PositionRead.model_validate(row) for row in rows]


# --- Employees ---------------------------------------------------------------


@router.post("/employees", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED)
def hire_employee(
    payload: EmployeeHireSchema,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> EmployeeRead:
    enforce_store_access(current_user, payload.store_id)
    row = service.hire_employee(
        db,
        EmployeeHireInput(
            employee_number=payload.employee_number,
            legal_name=payload.legal_name,
            display_name=payload.display_name,
            hire_date=payload.hire_date,
            user_id=payload.user_id,
            store_id=payload.store_id,
            department_id=payload.department_id,
            position_id=payload.position_id,
            manager_employee_id=payload.manager_employee_id,
        ),
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return EmployeeRead.model_validate(row)


@router.get("/employees", response_model=list[EmployeeRead])
def list_employees(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[EmployeeRead]:
    rows = service.list_employees(db, caller_store_id=current_user.store_id)
    return [EmployeeRead.model_validate(row) for row in rows]


@router.get("/employees/{employee_id}", response_model=EmployeeDetail)
def get_employee_detail(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> EmployeeDetail:
    employee = service.get_employee(db, employee_id)
    current_assignment = service.get_current_assignment(db, employee_id)
    if (
        current_assignment is not None
        and current_user.store_id is not None
        and current_user.store_id != current_assignment.store_id
    ):
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"access this employee in store {current_assignment.store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    current_status = service.get_current_status(db, employee_id)
    return EmployeeDetail(
        employee=EmployeeRead.model_validate(employee),
        current_status=(
            EmploymentStatusPeriodRead.model_validate(current_status)
            if current_status is not None
            else None
        ),
        current_assignment=(
            EmploymentAssignmentRead.model_validate(current_assignment)
            if current_assignment is not None
            else None
        ),
        current_compensation=None,
    )


@router.get(
    "/employees/{employee_id}/status-history", response_model=list[EmploymentStatusPeriodRead]
)
def get_employment_status_history(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[EmploymentStatusPeriodRead]:
    rows = service.employment_status_history(db, employee_id, caller_store_id=current_user.store_id)
    return [EmploymentStatusPeriodRead.model_validate(row) for row in rows]


@router.post("/employees/{employee_id}/status", response_model=EmploymentStatusPeriodRead)
def change_employment_status(
    employee_id: int,
    payload: EmploymentStatusChangeInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> EmploymentStatusPeriodRead:
    row = service.change_employment_status(
        db,
        employee_id=employee_id,
        new_status=payload.new_status,
        effective_from=payload.effective_from,
        reason=payload.reason,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return EmploymentStatusPeriodRead.model_validate(row)


@router.get(
    "/employees/{employee_id}/assignment-history", response_model=list[EmploymentAssignmentRead]
)
def get_employment_assignment_history(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[EmploymentAssignmentRead]:
    rows = service.employment_assignment_history(
        db, employee_id, caller_store_id=current_user.store_id
    )
    return [EmploymentAssignmentRead.model_validate(row) for row in rows]


@router.post("/employees/{employee_id}/reassign", response_model=EmploymentAssignmentRead)
def reassign_employee(
    employee_id: int,
    payload: EmploymentReassignmentInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> EmploymentAssignmentRead:
    row = service.reassign_employee(
        db,
        employee_id=employee_id,
        effective_from=payload.effective_from,
        store_id=payload.store_id,
        department_id=payload.department_id,
        position_id=payload.position_id,
        manager_employee_id=payload.manager_employee_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return EmploymentAssignmentRead.model_validate(row)


# --- Compensation (hr.compensation.write only — see module docstring) -------


@router.get(
    "/employees/{employee_id}/compensation-history", response_model=list[CompensationPeriodRead]
)
def get_compensation_history(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_compensation_permission),
) -> list[CompensationPeriodRead]:
    rows = service.compensation_history(db, employee_id, caller_store_id=current_user.store_id)
    return [CompensationPeriodRead.model_validate(row) for row in rows]


@router.post("/employees/{employee_id}/compensation", response_model=CompensationPeriodRead)
def change_compensation(
    employee_id: int,
    payload: CompensationChangeInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_compensation_permission),
) -> CompensationPeriodRead:
    row = service.change_compensation(
        db,
        employee_id=employee_id,
        effective_from=payload.effective_from,
        pay_type=payload.pay_type,
        rate=payload.rate,
        pay_frequency=payload.pay_frequency,
        overtime_eligible=payload.overtime_eligible,
        currency=payload.currency,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return CompensationPeriodRead.model_validate(row)


# --- Attendance ----------------------------------------------------------


@router.post(
    "/attendance/clock-in", response_model=AttendanceRecordRead, status_code=status.HTTP_201_CREATED
)
def clock_in(
    payload: ClockInInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_attendance_write_permission),
) -> AttendanceRecordRead:
    row = service.clock_in(
        db,
        employee_id=payload.employee_id,
        store_id=payload.store_id,
        clock_in_at=payload.clock_in_at,
        source=payload.source,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return AttendanceRecordRead.model_validate(row)


@router.post("/attendance/{attendance_record_id}/clock-out", response_model=AttendanceRecordRead)
def clock_out(
    attendance_record_id: int,
    payload: ClockOutInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_attendance_write_permission),
) -> AttendanceRecordRead:
    row = service.clock_out(
        db,
        attendance_record_id=attendance_record_id,
        clock_out_at=payload.clock_out_at,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return AttendanceRecordRead.model_validate(row)


@router.post("/attendance/{attendance_record_id}/correct", response_model=AttendanceRecordRead)
def correct_attendance(
    attendance_record_id: int,
    payload: AttendanceCorrectionInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_attendance_write_permission),
) -> AttendanceRecordRead:
    row = service.correct_attendance(
        db,
        attendance_record_id=attendance_record_id,
        new_clock_in_at=payload.new_clock_in_at,
        new_clock_out_at=payload.new_clock_out_at,
        reason=payload.reason,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return AttendanceRecordRead.model_validate(row)


@router.get("/attendance", response_model=list[AttendanceRecordRead])
def list_attendance_records(
    employee_id: int | None = None,
    work_date_from: date | None = None,
    work_date_to: date | None = None,
    include_voided: bool = False,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_attendance_read_permission),
) -> list[AttendanceRecordRead]:
    rows = service.list_attendance_records(
        db,
        employee_id=employee_id,
        caller_store_id=current_user.store_id,
        work_date_from=work_date_from,
        work_date_to=work_date_to,
        include_voided=include_voided,
    )
    return [AttendanceRecordRead.model_validate(row) for row in rows]
