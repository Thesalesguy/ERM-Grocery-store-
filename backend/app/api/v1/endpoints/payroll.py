"""Payroll period lifecycle, calculation, and GL posting. See
docs/M10_DESIGN.md Section 8 for the state machine and
app.modules.auth.permissions for the full RBAC rationale — in
particular why Manager gets `payroll.approve` but NOT `payroll.post`/
`payroll.reverse` (a documented conflict-of-interest decision, not an
oversight)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import ForbiddenError
from app.modules.auth.permissions import (
    PAYROLL_APPROVE,
    PAYROLL_CALCULATE,
    PAYROLL_POST,
    PAYROLL_READ,
    PAYROLL_REVERSE,
)
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.payroll import service
from app.modules.payroll.schemas import (
    PayrollEmployeeResultRead,
    PayrollPeriodCalculateInput,
    PayrollPeriodCancelInput,
    PayrollPeriodCreate,
    PayrollPeriodPostInput,
    PayrollPeriodRead,
    PayrollPeriodReverseInput,
)
from app.modules.payroll.service import PayrollPeriodInput

router = APIRouter(prefix="/payroll", tags=["payroll"])

_read_permission = require_permission(PAYROLL_READ)
_calculate_permission = require_permission(PAYROLL_CALCULATE)
_approve_permission = require_permission(PAYROLL_APPROVE)
_post_permission = require_permission(PAYROLL_POST)
_reverse_permission = require_permission(PAYROLL_REVERSE)


@router.post("/periods", response_model=PayrollPeriodRead, status_code=status.HTTP_201_CREATED)
def create_payroll_period(
    payload: PayrollPeriodCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_calculate_permission),
) -> PayrollPeriodRead:
    row = service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=payload.store_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            pay_date=payload.pay_date,
            payroll_run_id=payload.payroll_run_id,
        ),
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.get("/periods", response_model=list[PayrollPeriodRead])
def list_payroll_periods(
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[PayrollPeriodRead]:
    rows = service.list_payroll_periods(
        db, caller_store_id=current_user.store_id, status=status_filter
    )
    return [PayrollPeriodRead.model_validate(row) for row in rows]


@router.get("/periods/{payroll_period_id}", response_model=PayrollPeriodRead)
def get_payroll_period(
    payroll_period_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PayrollPeriodRead:
    row = service.get_payroll_period(db, payroll_period_id)
    if current_user.store_id is not None and current_user.store_id != row.store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"access this payroll period in store {row.store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    return PayrollPeriodRead.model_validate(row)


@router.get("/periods/{payroll_period_id}/results", response_model=list[PayrollEmployeeResultRead])
def list_payroll_employee_results(
    payroll_period_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[PayrollEmployeeResultRead]:
    period = service.get_payroll_period(db, payroll_period_id)
    if current_user.store_id is not None and current_user.store_id != period.store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"access this payroll period in store {period.store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    rows = service.list_payroll_employee_results(db, payroll_period_id=payroll_period_id)
    return [PayrollEmployeeResultRead.model_validate(row) for row in rows]


@router.post("/periods/{payroll_period_id}/open", response_model=PayrollPeriodRead)
def open_payroll_period(
    payroll_period_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_calculate_permission),
) -> PayrollPeriodRead:
    row = service.open_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.post("/periods/{payroll_period_id}/calculate", response_model=PayrollPeriodRead)
def calculate_payroll_period(
    payroll_period_id: int,
    payload: PayrollPeriodCalculateInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_calculate_permission),
) -> PayrollPeriodRead:
    row = service.calculate_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        client_transaction_id=payload.client_transaction_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.post("/periods/{payroll_period_id}/approve", response_model=PayrollPeriodRead)
def approve_payroll_period(
    payroll_period_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_approve_permission),
) -> PayrollPeriodRead:
    row = service.approve_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.post("/periods/{payroll_period_id}/cancel", response_model=PayrollPeriodRead)
def cancel_payroll_period(
    payroll_period_id: int,
    payload: PayrollPeriodCancelInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_calculate_permission),
) -> PayrollPeriodRead:
    row = service.cancel_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        reason=payload.reason,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.post("/periods/{payroll_period_id}/post", response_model=PayrollPeriodRead)
def post_payroll_period(
    payroll_period_id: int,
    payload: PayrollPeriodPostInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_post_permission),
) -> PayrollPeriodRead:
    row = service.post_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        client_transaction_id=payload.client_transaction_id,
    )
    return PayrollPeriodRead.model_validate(row)


@router.post("/periods/{payroll_period_id}/reverse", response_model=PayrollPeriodRead)
def reverse_payroll_period(
    payroll_period_id: int,
    payload: PayrollPeriodReverseInput,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reverse_permission),
) -> PayrollPeriodRead:
    row = service.reverse_payroll_period(
        db,
        payroll_period_id=payroll_period_id,
        reason=payload.reason,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return PayrollPeriodRead.model_validate(row)
