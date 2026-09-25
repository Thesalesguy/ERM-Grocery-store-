"""Cashier/till shift session endpoints (M15).

`shift.manage` gates opening/closing one's own shift and recording a cash
movement against it; `shift.override` additionally gates closing/
recording a movement against a DIFFERENT cashier's shift (re-checked at
the service layer, not just here — see app.modules.shifts.service);
`shift.read` gates the read-only history/detail endpoints.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.auth.permissions import SHIFT_MANAGE, SHIFT_READ
from app.modules.auth.service import CurrentUser, require_permission, scoped_store_filter
from app.modules.shifts import service
from app.modules.shifts.schemas import (
    CashMovementCreate,
    CashMovementRead,
    ShiftCloseCreate,
    ShiftOpenCreate,
    ShiftRead,
)

router = APIRouter(prefix="/shifts", tags=["shifts"])

_manage_permission = require_permission(SHIFT_MANAGE)
_read_permission = require_permission(SHIFT_READ)


@router.post("", response_model=ShiftRead, status_code=201)
def open_shift(
    payload: ShiftOpenCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_manage_permission),
) -> ShiftRead:
    shift = service.open_shift(
        db,
        store_id=payload.store_id,
        # Always the authenticated caller's own id — there is no
        # "open a shift for someone else" request to authorize.
        cashier_id=current_user.id,
        opening_float=payload.opening_float,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(shift)
    return ShiftRead.model_validate(shift)


@router.get("/active", response_model=ShiftRead | None)
def get_active_shift(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_manage_permission),
) -> ShiftRead | None:
    """The caller's own currently open shift, or null — used by the POS
    UI to show/hide the active-shift banner without a list query."""
    shift = service.get_active_shift_for_cashier(db, current_user.id)
    return ShiftRead.model_validate(shift) if shift is not None else None


@router.get("", response_model=list[ShiftRead])
def list_shifts(
    store_id: int | None = None,
    cashier_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ShiftRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    shifts = service.list_shifts(
        db,
        store_id=effective_store_id,
        cashier_id=cashier_id,
        status=status,
        limit=limit,
        offset=offset,
        caller_store_id=current_user.store_id,
    )
    return [ShiftRead.model_validate(s) for s in shifts]


@router.get("/{shift_id}", response_model=ShiftRead)
def get_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ShiftRead:
    shift = service.get_shift(db, shift_id, caller_store_id=current_user.store_id)
    return ShiftRead.model_validate(shift)


@router.get("/{shift_id}/cash-movements", response_model=list[CashMovementRead])
def list_cash_movements(
    shift_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[CashMovementRead]:
    # M17 item 1: the store-isolation check now lives inside
    # service.list_cash_movements itself (mirroring get_shift/
    # list_shifts), so a separate pre-check call is no longer needed
    # here.
    movements = service.list_cash_movements(db, shift_id, caller_store_id=current_user.store_id)
    return [CashMovementRead.model_validate(m) for m in movements]


@router.post("/{shift_id}/cash-movements", response_model=CashMovementRead, status_code=201)
def record_cash_movement(
    shift_id: int,
    payload: CashMovementCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_manage_permission),
) -> CashMovementRead:
    movement = service.record_cash_movement(
        db,
        shift_id=shift_id,
        movement_type=payload.movement_type,
        amount=payload.amount,
        reason=payload.reason,
        created_by=current_user.id,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(movement)
    return CashMovementRead.model_validate(movement)


@router.post("/{shift_id}/close", response_model=ShiftRead)
def close_shift(
    shift_id: int,
    payload: ShiftCloseCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_manage_permission),
) -> ShiftRead:
    shift = service.close_shift(
        db,
        shift_id=shift_id,
        closing_counted_amount=payload.closing_counted_amount,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        client_transaction_id=payload.client_transaction_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(shift)
    return ShiftRead.model_validate(shift)
