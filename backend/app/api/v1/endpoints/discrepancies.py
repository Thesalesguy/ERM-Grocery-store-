"""Receiving-discrepancy investigation endpoints: list, detail, investigate,
resolve. See app.modules.discrepancies.service for the full state-machine
and authorization rules this layer defers to entirely -- no accounting or
authorization logic is duplicated here.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import INVENTORY_READ, INVENTORY_TRANSFER_DISCREPANCY_INVESTIGATE
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.discrepancies import service
from app.modules.discrepancies.schemas import DiscrepancyResolveRequest, ReceivingDiscrepancyRead

router = APIRouter(prefix="/discrepancies", tags=["discrepancies"])

_read_permission = require_permission(INVENTORY_READ)
_investigate_permission = require_permission(INVENTORY_TRANSFER_DISCREPANCY_INVESTIGATE)


@router.get("", response_model=list[ReceivingDiscrepancyRead])
def list_discrepancies(
    status: str | None = None,
    transfer_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ReceivingDiscrepancyRead]:
    discrepancies = service.list_discrepancies(
        db,
        store_id=current_user.store_id,
        status=status,
        transfer_id=transfer_id,
        limit=limit,
        offset=offset,
    )
    return [ReceivingDiscrepancyRead.model_validate(d) for d in discrepancies]


@router.get("/{discrepancy_id}", response_model=ReceivingDiscrepancyRead)
def get_discrepancy(
    discrepancy_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ReceivingDiscrepancyRead:
    discrepancy = service.get_discrepancy(db, discrepancy_id)
    # NotFoundError, not ForbiddenError, for a cross-store read -- mirrors
    # app.api.v1.endpoints.transfers.get_transfer: never confirm a
    # resource's existence to a caller not authorized to see it.
    if current_user.store_id is not None and current_user.store_id != discrepancy.store_id:
        raise NotFoundError(f"Discrepancy {discrepancy_id} not found")
    return ReceivingDiscrepancyRead.model_validate(discrepancy)


@router.post("/{discrepancy_id}/investigate", response_model=ReceivingDiscrepancyRead)
def investigate_discrepancy(
    discrepancy_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_investigate_permission),
) -> ReceivingDiscrepancyRead:
    discrepancy = service.investigate_discrepancy(
        db,
        discrepancy_id,
        caller_store_id=current_user.store_id,
        actor_id=current_user.id,
    )
    db.commit()
    db.refresh(discrepancy)
    return ReceivingDiscrepancyRead.model_validate(discrepancy)


@router.post("/{discrepancy_id}/resolve", response_model=ReceivingDiscrepancyRead)
def resolve_discrepancy(
    discrepancy_id: int,
    payload: DiscrepancyResolveRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_investigate_permission),
) -> ReceivingDiscrepancyRead:
    discrepancy = service.resolve_discrepancy(
        db,
        discrepancy_id,
        resolution_type=payload.resolution_type,
        resolution_notes=payload.resolution_notes,
        caller_store_id=current_user.store_id,
        actor_id=current_user.id,
    )
    db.commit()
    db.refresh(discrepancy)
    return ReceivingDiscrepancyRead.model_validate(discrepancy)
