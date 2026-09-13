"""API-contract schemas for HR master data and effective-dated employment
history. See docs/M10_DESIGN.md for the design behind every field here.

Deliberately absent: any schema exposing `CompensationPeriod.rate` inside
an audit/log-shaped payload — read schemas expose it (an authorized
`hr.read`/`hr.compensation.write` caller is allowed to SEE pay rates
through the API), but nothing here feeds it into `audit_service.log_event`
(see service.py's own compensation functions for that boundary).
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DepartmentCreate(BaseModel):
    name: str = Field(max_length=150)


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_active: bool


class PositionCreate(BaseModel):
    title: str = Field(max_length=150)
    department_id: int | None = None


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    department_id: int | None
    is_active: bool


class EmployeeHireInput(BaseModel):
    employee_number: str = Field(max_length=32)
    legal_name: str = Field(max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    hire_date: date
    user_id: int | None = None
    store_id: int
    department_id: int | None = None
    position_id: int | None = None
    manager_employee_id: int | None = None


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_number: str
    legal_name: str
    display_name: str | None
    hire_date: date
    user_id: int | None


class EmploymentStatusPeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    status: str
    effective_from: date
    effective_to: date | None
    reason: str | None
    actor_id: int | None


class EmploymentStatusChangeInput(BaseModel):
    new_status: str
    effective_from: date
    reason: str | None = None


class EmploymentAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    store_id: int
    department_id: int | None
    position_id: int | None
    manager_employee_id: int | None
    effective_from: date
    effective_to: date | None


class EmploymentReassignmentInput(BaseModel):
    effective_from: date
    store_id: int
    department_id: int | None = None
    position_id: int | None = None
    manager_employee_id: int | None = None


class CompensationPeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    pay_type: str
    rate: Decimal
    pay_frequency: str
    overtime_eligible: bool
    currency: str
    effective_from: date
    effective_to: date | None


class CompensationChangeInput(BaseModel):
    effective_from: date
    pay_type: str
    rate: Decimal = Field(ge=0)
    pay_frequency: str
    overtime_eligible: bool = False
    currency: str = Field(default="USD", max_length=3)


class EmployeeDetail(BaseModel):
    """A single read model joining an employee's identity with their
    CURRENT (effective_to IS NULL) status/assignment/compensation —
    the "what is true right now" view (M10_DESIGN.md: never a second,
    independently-maintained current-value column)."""

    employee: EmployeeRead
    current_status: EmploymentStatusPeriodRead | None
    current_assignment: EmploymentAssignmentRead | None
    current_compensation: CompensationPeriodRead | None
