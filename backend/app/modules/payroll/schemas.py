"""API-contract schemas for the payroll period lifecycle (M10 Phase 4).
See docs/M10_DESIGN.md Section 8 for the state machine."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PayrollPeriodCreate(BaseModel):
    store_id: int
    period_start: date
    period_end: date
    pay_date: date
    payroll_run_id: str | None = Field(default=None, max_length=36)


class PayrollPeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    payroll_run_id: str | None
    period_start: date
    period_end: date
    pay_date: date
    status: str
    calculated_by: int | None
    calculated_at: datetime | None
    approved_by: int | None
    approved_at: datetime | None
    journal_entry_id: int | None
    posted_by: int | None
    posted_at: datetime | None
    total_gross: Decimal
    total_deductions: Decimal
    total_employer_contributions: Decimal
    total_net_pay: Decimal


class PayrollPeriodCancelInput(BaseModel):
    reason: str


class PayrollPeriodCalculateInput(BaseModel):
    client_transaction_id: str | None = Field(default=None, max_length=100)


class PayrollPeriodPostInput(BaseModel):
    client_transaction_id: str | None = Field(default=None, max_length=100)


class PayrollPeriodReverseInput(BaseModel):
    reason: str


class PayrollEarningLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    earning_type: str
    hours: Decimal | None
    rate: Decimal | None
    amount: Decimal
    description: str | None


class PayrollDeductionLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    deduction_type_id: int
    is_employer_contribution: bool
    amount: Decimal
    description: str | None


class PayrollEmployeeResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payroll_period_id: int
    employee_id: int
    status: str
    store_id: int
    department_id: int | None
    position_id: int | None
    pay_type: str
    pay_rate: Decimal
    currency: str
    regular_hours: Decimal
    overtime_hours: Decimal
    gross_pay: Decimal
    total_deductions: Decimal
    total_employer_contributions: Decimal
    net_pay: Decimal
