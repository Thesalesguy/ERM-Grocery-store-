"""Approval-gated transfer write-off endpoints: create, approve, deny,
reverse, plus admin policy-rule CRUD. See app.modules.writeoffs.service
for the full state-machine and authorization rules this layer defers to
entirely -- no accounting or authorization logic is duplicated here.

Unlike app.api.v1.endpoints.discrepancies, list/detail reads here are
deliberately NOT filtered to the caller's own store_id:
docs/M24D_TECHNICAL_CONTRACT.md Section 10.3's own illustrative example
has a peer Store Manager from a DIFFERENT store approving a write-off,
so approval review is cross-store-visible by design (see
app.modules.writeoffs.service's module docstring and
_check_approval_authority, which likewise applies no store scoping to
the approver). Initiation itself is still dual-store-scoped in the
service layer (either side of the originating transfer, or
unrestricted).

The /policy-rules/ routes are registered BEFORE /{write_off_id} so
"policy-rules" is never captured as a write_off_id path parameter."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.auth.permissions import (
    ACCOUNTING_ADMIN,
    INVENTORY_READ,
    INVENTORY_TRANSFER_WRITEOFF_APPROVE,
    INVENTORY_TRANSFER_WRITEOFF_INITIATE,
    INVENTORY_TRANSFER_WRITEOFF_REVERSE,
)
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.writeoffs import service
from app.modules.writeoffs.schemas import (
    ApprovalPolicyRuleCreate,
    ApprovalPolicyRuleRead,
    TransferWriteOffCreate,
    TransferWriteOffRead,
    WriteOffDecisionRequest,
    WriteOffReverseRequest,
)

router = APIRouter(prefix="/write-offs", tags=["write-offs"])

_read_permission = require_permission(INVENTORY_READ)
_initiate_permission = require_permission(INVENTORY_TRANSFER_WRITEOFF_INITIATE)
_approve_permission = require_permission(INVENTORY_TRANSFER_WRITEOFF_APPROVE)
_reverse_permission = require_permission(INVENTORY_TRANSFER_WRITEOFF_REVERSE)
_admin_permission = require_permission(ACCOUNTING_ADMIN)


@router.get("", response_model=list[TransferWriteOffRead])
def list_write_offs(
    status: str | None = None,
    discrepancy_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[TransferWriteOffRead]:
    write_offs = service.list_write_offs(
        db, status=status, discrepancy_id=discrepancy_id, limit=limit, offset=offset
    )
    return [TransferWriteOffRead.model_validate(w) for w in write_offs]


@router.get("/policy-rules/", response_model=list[ApprovalPolicyRuleRead])
def list_approval_policy_rules(
    action_type: str | None = None,
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_admin_permission),
) -> list[ApprovalPolicyRuleRead]:
    rules = service.list_approval_policy_rules(db, action_type=action_type, is_active=is_active)
    return [ApprovalPolicyRuleRead.model_validate(r) for r in rules]


@router.post("/policy-rules/", response_model=ApprovalPolicyRuleRead)
def create_approval_policy_rule(
    payload: ApprovalPolicyRuleCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_admin_permission),
) -> ApprovalPolicyRuleRead:
    rule = service.create_approval_policy_rule(
        db,
        action_type=payload.action_type,
        scope_store_id=payload.scope_store_id,
        min_amount=payload.min_amount,
        max_amount=payload.max_amount,
        required_role_id=payload.required_role_id,
        escalation_role_id=payload.escalation_role_id,
        is_active=payload.is_active,
        created_by=current_user.id,
    )
    db.commit()
    db.refresh(rule)
    return ApprovalPolicyRuleRead.model_validate(rule)


@router.get("/{write_off_id}", response_model=TransferWriteOffRead)
def get_write_off(
    write_off_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> TransferWriteOffRead:
    write_off = service.get_write_off(db, write_off_id)
    return TransferWriteOffRead.model_validate(write_off)


@router.post("", response_model=TransferWriteOffRead)
def create_write_off(
    payload: TransferWriteOffCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_initiate_permission),
) -> TransferWriteOffRead:
    write_off = service.create_write_off(
        db,
        discrepancy_id=payload.discrepancy_id,
        quantity=payload.quantity,
        reason=payload.reason,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        initiated_by=current_user.id,
    )
    db.commit()
    db.refresh(write_off)
    return TransferWriteOffRead.model_validate(write_off)


@router.post("/{write_off_id}/approve", response_model=TransferWriteOffRead)
def approve_write_off(
    write_off_id: int,
    payload: WriteOffDecisionRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_approve_permission),
) -> TransferWriteOffRead:
    write_off = service.approve_write_off(
        db,
        write_off_id=write_off_id,
        decision_notes=payload.decision_notes,
        approved_by=current_user.id,
    )
    db.commit()
    db.refresh(write_off)
    return TransferWriteOffRead.model_validate(write_off)


@router.post("/{write_off_id}/deny", response_model=TransferWriteOffRead)
def deny_write_off(
    write_off_id: int,
    payload: WriteOffDecisionRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_approve_permission),
) -> TransferWriteOffRead:
    write_off = service.deny_write_off(
        db,
        write_off_id=write_off_id,
        decision_notes=payload.decision_notes,
        approved_by=current_user.id,
    )
    db.commit()
    db.refresh(write_off)
    return TransferWriteOffRead.model_validate(write_off)


@router.post("/{write_off_id}/reverse", response_model=TransferWriteOffRead)
def reverse_write_off(
    write_off_id: int,
    payload: WriteOffReverseRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reverse_permission),
) -> TransferWriteOffRead:
    write_off = service.reverse_write_off(
        db,
        write_off_id=write_off_id,
        reason=payload.reason,
        reversed_by=current_user.id,
    )
    db.commit()
    db.refresh(write_off)
    return TransferWriteOffRead.model_validate(write_off)
