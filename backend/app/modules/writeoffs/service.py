"""Approval-gated transfer write-off business logic.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/6.2/10/20 and the module
docstring in app.modules.writeoffs.models for the full design. Summary of
the control flow this module implements:

- A write-off always references a real, persisted, RESOLVED
  ReceivingDiscrepancy (Phase 3) whose finding is not VENDOR_CAUSED
  (vendor-caused losses route through the not-yet-built supplier-credit-
  note workflow instead -- Section 20 Scenario 8: "no TransferWriteOff/
  shrinkage posting at all" for that finding).
- `create_write_off` never posts anything -- it only records the REQUEST
  and freezes which role may decide it (Section 17's "approval-policy
  changes during a pending request" hazard).
- `approve_write_off` is the one function that has an economic
  consequence: it posts the GL journal, increments the transfer line's
  `written_off_quantity`, and resolves the approval record all in the
  SAME transaction (Section 4's "implement the economic consequence
  only after the required approval succeeds").
- `deny_write_off` has NO accounting or inventory effect at all -- it
  only records the decision and reopens the discrepancy
  (app.modules.discrepancies.service.reopen_discrepancy_after_write_off_denial)
  for a fresh investigation.
- `reverse_write_off` is Central/System Admin-only, mirrors
  app.modules.ap.service.reverse_supplier_payment's exact structure, and
  never edits the original journal or approval row.
"""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.audit import service as audit_service
from app.modules.auth.models import Role
from app.modules.auth.permissions import ADMIN
from app.modules.auth.service import get_user_role_name
from app.modules.discrepancies import service as discrepancy_service
from app.modules.discrepancies.models import ReceivingDiscrepancy
from app.modules.transfers.models import InterStoreTransferLine, InterStoreTransferReceiptItem
from app.modules.writeoffs.models import (
    APPROVAL_ACTION_TYPES,
    ApprovalPolicyRule,
    TransferWriteOff,
    WriteOffApproval,
)

_LEDGER_QUANTUM = Decimal("0.000001")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_LEDGER_QUANTUM, rounding=ROUND_HALF_UP)


def _enforce_dual_store_access(
    caller_store_id: int | None, store_a: int, store_b: int | None, noun: str
) -> None:
    """Mirrors app.modules.transfers.service.cancel_transfer's own
    dual-store check: a store-scoped caller may act if THEIR store is
    either side of the transfer this discrepancy came from."""
    allowed = {store_a}
    if store_b is not None:
        allowed.add(store_b)
    if caller_store_id is not None and caller_store_id not in allowed:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access {noun}",
            error_code="STORE_ACCESS_DENIED",
        )


# --- Approval policy (admin-managed configuration, D2) -----------------


def create_approval_policy_rule(
    db: Session,
    *,
    action_type: str,
    scope_store_id: int | None,
    min_amount: Decimal,
    max_amount: Decimal | None,
    required_role_id: int,
    escalation_role_id: int | None,
    is_active: bool,
    created_by: int | None,
) -> ApprovalPolicyRule:
    if action_type not in APPROVAL_ACTION_TYPES:
        raise ValidationAppError(
            f"Invalid action_type {action_type!r}", error_code="INVALID_ACTION_TYPE"
        )
    if db.get(Role, required_role_id) is None:
        raise NotFoundError(f"Role {required_role_id} not found")
    if escalation_role_id is not None and db.get(Role, escalation_role_id) is None:
        raise NotFoundError(f"Role {escalation_role_id} not found")

    rule = ApprovalPolicyRule(
        action_type=action_type,
        scope_store_id=scope_store_id,
        min_amount=min_amount,
        max_amount=max_amount,
        required_role_id=required_role_id,
        escalation_role_id=escalation_role_id,
        is_active=is_active,
        created_by=created_by,
    )
    db.add(rule)
    db.flush()
    audit_service.log_event(
        db,
        user_id=created_by,
        action="APPROVAL_POLICY_RULE_CREATED",
        entity_type="approval_policy_rule",
        entity_id=rule.id,
        after={
            "action_type": action_type,
            "scope_store_id": scope_store_id,
            "min_amount": str(min_amount),
            "max_amount": str(max_amount) if max_amount is not None else None,
            "required_role_id": required_role_id,
        },
    )
    return rule


def list_approval_policy_rules(
    db: Session, *, action_type: str | None = None, is_active: bool | None = None
) -> list[ApprovalPolicyRule]:
    query = select(ApprovalPolicyRule).order_by(ApprovalPolicyRule.id)
    if action_type is not None:
        query = query.where(ApprovalPolicyRule.action_type == action_type)
    if is_active is not None:
        query = query.where(ApprovalPolicyRule.is_active == is_active)
    return list(db.execute(query).scalars().all())


def _resolve_required_role(
    db: Session, *, store_id: int, amount: Decimal
) -> tuple[int, int | None]:
    """Picks the ApprovalPolicyRule whose range covers `amount`, preferring
    a store-specific row over the store-agnostic fallback at the same
    amount (Section 10.1). Fails CLOSED to the Admin role -- never to the
    laxest or most permissive rule -- when no active rule covers this
    amount/store at all (an unconfigured gap, Section 10.1's explicit
    "unconfigured gap" case), matching the contract's own invariant that
    an approval gap must never be silently permissive."""
    rules = list(
        db.execute(
            select(ApprovalPolicyRule).where(
                ApprovalPolicyRule.action_type == "TRANSFER_WRITE_OFF",
                ApprovalPolicyRule.is_active.is_(True),
                ApprovalPolicyRule.min_amount <= amount,
                (ApprovalPolicyRule.max_amount.is_(None))
                | (ApprovalPolicyRule.max_amount > amount),
                (ApprovalPolicyRule.scope_store_id.is_(None))
                | (ApprovalPolicyRule.scope_store_id == store_id),
            )
        )
        .scalars()
        .all()
    )
    if not rules:
        admin_role_id = db.execute(select(Role.id).where(Role.name == ADMIN)).scalar_one()
        return admin_role_id, None

    rules.sort(key=lambda r: (r.scope_store_id is None, -r.min_amount))
    chosen = rules[0]
    return chosen.required_role_id, chosen.id


# --- Write-off creation (the approval REQUEST only -- no GL/inventory effect) --


def create_write_off(
    db: Session,
    *,
    discrepancy_id: int,
    quantity: Decimal,
    reason: str,
    client_transaction_id: str,
    caller_store_id: int | None,
    initiated_by: int | None,
) -> TransferWriteOff:
    existing = db.execute(
        select(TransferWriteOff).where(
            TransferWriteOff.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if quantity <= 0:
        raise ValidationAppError("quantity must be positive", error_code="INVALID_QUANTITY")
    if not reason or not reason.strip():
        raise ValidationAppError("reason is required", error_code="REASON_REQUIRED")
    if initiated_by is None:
        raise ValidationAppError(
            "Write-off initiation requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    discrepancy = db.execute(
        select(ReceivingDiscrepancy)
        .where(ReceivingDiscrepancy.id == discrepancy_id)
        .with_for_update()
    ).scalar_one_or_none()
    if discrepancy is None:
        raise NotFoundError(f"Discrepancy {discrepancy_id} not found")

    _enforce_dual_store_access(
        caller_store_id,
        discrepancy.store_id,
        discrepancy.counterparty_store_id,
        "this discrepancy's write-off",
    )

    # Post-lock idempotency re-check -- a racing caller may have already
    # created this exact write-off while this caller waited for the lock.
    existing = db.execute(
        select(TransferWriteOff).where(
            TransferWriteOff.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if discrepancy.source_type != "TRANSFER_RECEIPT":
        raise ValidationAppError(
            f"Discrepancy {discrepancy_id} has source_type {discrepancy.source_type!r}, which "
            "this phase's write-off workflow does not support",
            error_code="UNSUPPORTED_SOURCE_TYPE",
        )
    if discrepancy.status != "RESOLVED":
        raise ConflictError(
            f"Discrepancy {discrepancy_id} is {discrepancy.status}, not RESOLVED -- a "
            "discrepancy with an unresolved investigation must not be written off",
            error_code="DISCREPANCY_NOT_RESOLVED",
        )
    if discrepancy.resolution_type == "VENDOR_CAUSED":
        raise ConflictError(
            f"Discrepancy {discrepancy_id} was resolved as VENDOR_CAUSED -- it is not "
            "eligible for a transfer write-off (routes through a supplier credit note instead)",
            error_code="VENDOR_CAUSED_NOT_ELIGIBLE",
        )
    if discrepancy.counterparty_store_id is None:
        raise ConflictError(
            f"Discrepancy {discrepancy_id} has no counterparty store to attribute a write-off "
            "to",
            error_code="NO_ATTRIBUTION_STORE",
        )

    pending = db.execute(
        select(TransferWriteOff.id).where(
            TransferWriteOff.discrepancy_id == discrepancy_id,
            TransferWriteOff.status == "PENDING_APPROVAL",
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise ConflictError(
            f"Discrepancy {discrepancy_id} already has a pending write-off request "
            f"({pending}) -- only one may be outstanding at a time",
            error_code="DUPLICATE_PENDING_WRITE_OFF",
        )

    already_written_off = (
        db.execute(
            select(TransferWriteOff.quantity).where(
                TransferWriteOff.discrepancy_id == discrepancy_id,
                TransferWriteOff.status == "POSTED",
            )
        )
        .scalars()
        .all()
    )
    remaining = (discrepancy.quantity_short + discrepancy.quantity_damaged) - sum(
        already_written_off, Decimal("0")
    )
    if quantity > remaining:
        raise ValidationAppError(
            f"Requested quantity {quantity} exceeds the discrepancy's remaining "
            f"unresolved quantity {remaining}",
            error_code="QUANTITY_EXCEEDS_REMAINING_DISCREPANCY",
        )

    receipt_item = db.get(InterStoreTransferReceiptItem, discrepancy.source_id)
    if receipt_item is None:
        raise ConflictError(
            f"Discrepancy {discrepancy_id}'s source receipt item no longer exists",
            error_code="SOURCE_RECEIPT_ITEM_MISSING",
        )

    amount = _quantize(quantity * discrepancy.unit_cost)
    required_role_id, matched_rule_id = _resolve_required_role(
        db, store_id=discrepancy.counterparty_store_id, amount=amount
    )

    write_off = TransferWriteOff(
        transfer_id=discrepancy.transfer_id,
        transfer_line_id=receipt_item.inter_store_transfer_line_id,
        discrepancy_id=discrepancy.id,
        quantity=quantity,
        unit_cost=discrepancy.unit_cost,
        amount=amount,
        store_id=discrepancy.counterparty_store_id,
        reason=reason,
        initiated_by=initiated_by,
        initiated_at=datetime.now(UTC),
        required_role_id=required_role_id,
        matched_rule_id=matched_rule_id,
        status="PENDING_APPROVAL",
        client_transaction_id=client_transaction_id,
    )
    db.add(write_off)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(TransferWriteOff).where(
                TransferWriteOff.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    audit_service.log_event(
        db,
        user_id=initiated_by,
        action="WRITE_OFF_REQUESTED",
        entity_type="transfer_write_off",
        entity_id=write_off.id,
        after={
            "discrepancy_id": discrepancy.id,
            "quantity": str(quantity),
            "amount": str(amount),
            "store_id": write_off.store_id,
            "required_role_id": required_role_id,
        },
    )
    return write_off


# --- Reads ---------------------------------------------------------------


def get_write_off(db: Session, write_off_id: int) -> TransferWriteOff:
    write_off = db.get(TransferWriteOff, write_off_id)
    if write_off is None:
        raise NotFoundError(f"Write-off {write_off_id} not found")
    return write_off


def list_write_offs(
    db: Session,
    *,
    store_id: int | None = None,
    status: str | None = None,
    discrepancy_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TransferWriteOff]:
    query = (
        select(TransferWriteOff)
        .order_by(TransferWriteOff.created_at.desc(), TransferWriteOff.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(TransferWriteOff.store_id == store_id)
    if status is not None:
        query = query.where(TransferWriteOff.status == status)
    if discrepancy_id is not None:
        query = query.where(TransferWriteOff.discrepancy_id == discrepancy_id)
    return list(db.execute(query).scalars().all())


# --- Approval decision ---------------------------------------------------


def _check_approval_authority(db: Session, write_off: TransferWriteOff, approved_by: int) -> None:
    if approved_by == write_off.initiated_by:
        raise ForbiddenError(
            "The initiator of a write-off request may not also decide it",
            error_code="SELF_APPROVAL_PROHIBITED",
        )
    role_name = get_user_role_name(db, approved_by)
    if role_name == ADMIN:
        return
    required_role = db.get(Role, write_off.required_role_id)
    if role_name is None or required_role is None or role_name != required_role.name:
        raise ForbiddenError(
            "You do not hold the approval authority this write-off's policy tier requires",
            error_code="INSUFFICIENT_APPROVAL_AUTHORITY",
        )


def approve_write_off(
    db: Session,
    *,
    write_off_id: int,
    decision_notes: str,
    approved_by: int | None,
    posting_date: date | None = None,
) -> TransferWriteOff:
    """PENDING_APPROVAL -> POSTED, atomically: posts the GL journal,
    increments the transfer line's written_off_quantity, and records the
    WriteOffApproval row all in one transaction (Section 4) -- `status`
    is never left sitting at the contract's intermediate `APPROVED` value
    on its own (see the module docstring in
    app.modules.writeoffs.models for why)."""
    if not decision_notes or not decision_notes.strip():
        raise ValidationAppError("decision_notes is required", error_code="DECISION_NOTES_REQUIRED")
    if approved_by is None:
        raise ValidationAppError(
            "Approval requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    existing_decision = db.execute(
        select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off_id)
    ).scalar_one_or_none()
    if existing_decision is not None:
        return get_write_off(db, write_off_id)

    write_off = db.execute(
        select(TransferWriteOff).where(TransferWriteOff.id == write_off_id).with_for_update()
    ).scalar_one_or_none()
    if write_off is None:
        raise NotFoundError(f"Write-off {write_off_id} not found")

    # Post-lock idempotency re-check (mirrors ship_transfer/
    # reverse_supplier_payment's own pattern): a racing decision may have
    # landed while this caller waited for the lock.
    existing_decision = db.execute(
        select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off_id)
    ).scalar_one_or_none()
    if existing_decision is not None:
        return write_off

    if write_off.status != "PENDING_APPROVAL":
        raise ConflictError(
            f"Write-off {write_off_id} is {write_off.status}, not PENDING_APPROVAL",
            error_code="INVALID_WRITE_OFF_STATE",
        )

    _check_approval_authority(db, write_off, approved_by)

    discrepancy = db.execute(
        select(ReceivingDiscrepancy)
        .where(ReceivingDiscrepancy.id == write_off.discrepancy_id)
        .with_for_update()
    ).scalar_one_or_none()
    if discrepancy is None:
        raise NotFoundError(f"Discrepancy {write_off.discrepancy_id} not found")

    already_written_off = (
        db.execute(
            select(TransferWriteOff.quantity).where(
                TransferWriteOff.discrepancy_id == discrepancy.id,
                TransferWriteOff.status == "POSTED",
            )
        )
        .scalars()
        .all()
    )
    remaining = (discrepancy.quantity_short + discrepancy.quantity_damaged) - sum(
        already_written_off, Decimal("0")
    )
    if write_off.quantity > remaining:
        raise ConflictError(
            f"Write-off {write_off_id}'s quantity {write_off.quantity} now exceeds the "
            f"discrepancy's remaining unresolved quantity {remaining}",
            error_code="QUANTITY_EXCEEDS_REMAINING_DISCREPANCY",
        )

    transfer_line = db.execute(
        select(InterStoreTransferLine)
        .where(InterStoreTransferLine.id == write_off.transfer_line_id)
        .with_for_update()
    ).scalar_one_or_none()
    if transfer_line is None:
        raise NotFoundError(f"Transfer line {write_off.transfer_line_id} not found")

    entry = accounting_service.post_transfer_write_off_journal(
        db,
        write_off=write_off,
        posting_date=posting_date if posting_date is not None else datetime.now(UTC).date(),
        created_by=approved_by,
    )
    write_off.journal_entry_id = entry.id
    write_off.status = "POSTED"
    transfer_line.written_off_quantity = transfer_line.written_off_quantity + write_off.quantity

    db.add(
        WriteOffApproval(
            write_off_id=write_off.id,
            initiated_by=write_off.initiated_by,
            approved_by=approved_by,
            decision="APPROVED",
            decided_at=datetime.now(UTC),
            decision_notes=decision_notes,
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Write-off {write_off_id} has already been decided", error_code="ALREADY_DECIDED"
        ) from exc

    audit_service.log_event(
        db,
        user_id=approved_by,
        action="WRITE_OFF_APPROVED_AND_POSTED",
        entity_type="transfer_write_off",
        entity_id=write_off.id,
        after={
            "status": "POSTED",
            "journal_entry_id": entry.id,
            "amount": str(write_off.amount),
        },
    )
    return write_off


def deny_write_off(
    db: Session,
    *,
    write_off_id: int,
    decision_notes: str,
    approved_by: int | None,
) -> TransferWriteOff:
    """PENDING_APPROVAL -> DENIED. No inventory or GL effect at all. The
    originating discrepancy reopens into INVESTIGATING (Section 20
    Scenario 11) via
    app.modules.discrepancies.service.reopen_discrepancy_after_write_off_denial
    -- a genuine fresh investigation/resolution is required before
    another write-off may be submitted against it."""
    if not decision_notes or not decision_notes.strip():
        raise ValidationAppError("decision_notes is required", error_code="DECISION_NOTES_REQUIRED")
    if approved_by is None:
        raise ValidationAppError(
            "A denial requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    existing_decision = db.execute(
        select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off_id)
    ).scalar_one_or_none()
    if existing_decision is not None:
        return get_write_off(db, write_off_id)

    write_off = db.execute(
        select(TransferWriteOff).where(TransferWriteOff.id == write_off_id).with_for_update()
    ).scalar_one_or_none()
    if write_off is None:
        raise NotFoundError(f"Write-off {write_off_id} not found")

    existing_decision = db.execute(
        select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off_id)
    ).scalar_one_or_none()
    if existing_decision is not None:
        return write_off

    if write_off.status != "PENDING_APPROVAL":
        raise ConflictError(
            f"Write-off {write_off_id} is {write_off.status}, not PENDING_APPROVAL",
            error_code="INVALID_WRITE_OFF_STATE",
        )

    _check_approval_authority(db, write_off, approved_by)

    write_off.status = "DENIED"
    db.add(
        WriteOffApproval(
            write_off_id=write_off.id,
            initiated_by=write_off.initiated_by,
            approved_by=approved_by,
            decision="DENIED",
            decided_at=datetime.now(UTC),
            decision_notes=decision_notes,
        )
    )
    discrepancy_service.reopen_discrepancy_after_write_off_denial(
        db, write_off.discrepancy_id, actor_id=approved_by
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Write-off {write_off_id} has already been decided", error_code="ALREADY_DECIDED"
        ) from exc

    audit_service.log_event(
        db,
        user_id=approved_by,
        action="WRITE_OFF_DENIED",
        entity_type="transfer_write_off",
        entity_id=write_off.id,
        after={"status": "DENIED"},
    )
    return write_off


# --- Reversal (Central/System Admin only) --------------------------------


def reverse_write_off(
    db: Session,
    *,
    write_off_id: int,
    reason: str,
    reversed_by: int | None,
) -> TransferWriteOff:
    """Admin-only correction path, mirrors
    app.modules.ap.service.reverse_supplier_payment's exact structure:
    idempotent-by-existence, locks the target row first, posts a NEW
    compensating journal entry (never edits/deletes the original), and
    decrements the transfer line's written_off_quantity back down so the
    Inventory-In-Transit reconciliation stays in agreement with the GL
    (the exact mirror of what approval incremented)."""
    if not reason or not reason.strip():
        raise ValidationAppError(
            "A reversal reason is required", error_code="REVERSAL_REASON_REQUIRED"
        )
    if reversed_by is None:
        raise ValidationAppError(
            "Reversal requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )
    role_name = get_user_role_name(db, reversed_by)
    if role_name != ADMIN:
        raise ForbiddenError(
            "Only a Central/System Admin may reverse a posted write-off",
            error_code="REVERSAL_REQUIRES_ADMIN",
        )

    write_off = db.execute(
        select(TransferWriteOff).where(TransferWriteOff.id == write_off_id).with_for_update()
    ).scalar_one_or_none()
    if write_off is None:
        raise NotFoundError(f"Write-off {write_off_id} not found")

    if write_off.reversal_journal_entry_id is not None:
        return write_off

    if write_off.status != "POSTED":
        raise ConflictError(
            f"Write-off {write_off_id} is {write_off.status}, not POSTED -- only a posted "
            "write-off can be reversed",
            error_code="INVALID_WRITE_OFF_STATE",
        )

    transfer_line = db.execute(
        select(InterStoreTransferLine)
        .where(InterStoreTransferLine.id == write_off.transfer_line_id)
        .with_for_update()
    ).scalar_one_or_none()
    if transfer_line is None:
        raise NotFoundError(f"Transfer line {write_off.transfer_line_id} not found")

    reversal_entry = accounting_service.post_transfer_write_off_reversal_journal(
        db, write_off=write_off, created_by=reversed_by
    )
    write_off.reversal_journal_entry_id = reversal_entry.id
    write_off.status = "REVERSED"
    transfer_line.written_off_quantity = transfer_line.written_off_quantity - write_off.quantity

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Write-off {write_off_id} has already been reversed", error_code="ALREADY_REVERSED"
        ) from exc

    audit_service.log_event(
        db,
        user_id=reversed_by,
        action="WRITE_OFF_REVERSED",
        entity_type="transfer_write_off",
        entity_id=write_off.id,
        after={
            "status": "REVERSED",
            "reversal_journal_entry_id": reversal_entry.id,
            "reason": reason,
        },
    )
    return write_off
