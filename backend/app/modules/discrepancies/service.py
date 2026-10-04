"""Discrepancy-investigation business logic.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/12/16/17 and the module
docstring in app.modules.discrepancies.models for the full design.

The central invariant (the M25 Phase 3 brief's own words): a discrepancy
is a factual unresolved business event first. Creating one must never
itself change GL balances -- nothing in this module ever calls
app.modules.accounting.service, posts a JournalEntry, creates a
TransferWriteOff, or touches a SupplierCreditNote. Those all belong to a
later phase.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.audit import service as audit_service
from app.modules.discrepancies.models import (
    DISCREPANCY_RESOLUTION_TYPES,
    ReceivingDiscrepancy,
)


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access "
            f"{noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


def _discrepancy_type(quantity_short: Decimal, quantity_damaged: Decimal) -> str:
    if quantity_short > 0 and quantity_damaged > 0:
        return "BOTH"
    if quantity_short > 0:
        return "SHORTAGE"
    return "DAMAGE"


# --- Creation (the automatic RECORDED fact) ----------------------------------


def create_discrepancy_for_receipt_item(
    db: Session,
    *,
    receipt_item_id: int,
    transfer_id: int,
    product_id: int,
    store_id: int,
    counterparty_store_id: int | None,
    quantity_short: Decimal,
    quantity_damaged: Decimal,
    unit_cost: Decimal,
    raised_by: int | None,
    raised_at: datetime,
) -> ReceivingDiscrepancy | None:
    """Called from app.modules.transfers.service.receive_transfer, in the
    SAME transaction as the InterStoreTransferReceiptItem it describes --
    never on receive_transfer's idempotent-replay fast path, so under
    ordinary operation this never runs twice for the same receipt item.
    The UniqueConstraint on (source_type, source_id) is nonetheless the
    authoritative idempotency guarantee (Section 17): even a direct,
    out-of-band call with the same receipt_item_id can create at most one
    row, racing concurrent calls included (the second loses the unique
    constraint and returns the winner, mirroring every other
    client_transaction_id-guarded creation in this codebase).

    Returns None (creates nothing) when there is no discrepancy to
    record -- the ordinary, by-far-most-common case."""
    if quantity_short <= 0 and quantity_damaged <= 0:
        return None

    existing = db.execute(
        select(ReceivingDiscrepancy).where(
            ReceivingDiscrepancy.source_type == "TRANSFER_RECEIPT",
            ReceivingDiscrepancy.source_id == receipt_item_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    discrepancy = ReceivingDiscrepancy(
        source_type="TRANSFER_RECEIPT",
        source_id=receipt_item_id,
        transfer_id=transfer_id,
        product_id=product_id,
        store_id=store_id,
        counterparty_store_id=counterparty_store_id,
        discrepancy_type=_discrepancy_type(quantity_short, quantity_damaged),
        quantity_short=quantity_short,
        quantity_damaged=quantity_damaged,
        unit_cost=unit_cost,
        status="RECORDED",
        raised_by=raised_by,
        raised_at=raised_at,
    )
    db.add(discrepancy)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(ReceivingDiscrepancy).where(
                ReceivingDiscrepancy.source_type == "TRANSFER_RECEIPT",
                ReceivingDiscrepancy.source_id == receipt_item_id,
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    audit_service.log_event(
        db,
        user_id=raised_by,
        action="DISCREPANCY_RECORDED",
        entity_type="receiving_discrepancy",
        entity_id=discrepancy.id,
        after={
            "source_type": "TRANSFER_RECEIPT",
            "source_id": receipt_item_id,
            "transfer_id": transfer_id,
            "store_id": store_id,
            "discrepancy_type": discrepancy.discrepancy_type,
            "quantity_short": str(quantity_short),
            "quantity_damaged": str(quantity_damaged),
            "unit_cost": str(unit_cost),
        },
    )
    return discrepancy


# --- Reads --------------------------------------------------------------


def get_discrepancy(db: Session, discrepancy_id: int) -> ReceivingDiscrepancy:
    discrepancy = db.get(ReceivingDiscrepancy, discrepancy_id)
    if discrepancy is None:
        raise NotFoundError(f"Discrepancy {discrepancy_id} not found")
    return discrepancy


def list_discrepancies(
    db: Session,
    *,
    store_id: int | None = None,
    status: str | None = None,
    transfer_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReceivingDiscrepancy]:
    query = (
        select(ReceivingDiscrepancy)
        .order_by(ReceivingDiscrepancy.created_at.desc(), ReceivingDiscrepancy.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(ReceivingDiscrepancy.store_id == store_id)
    if status is not None:
        query = query.where(ReceivingDiscrepancy.status == status)
    if transfer_id is not None:
        query = query.where(ReceivingDiscrepancy.transfer_id == transfer_id)
    return list(db.execute(query).scalars().all())


# --- State transitions ---------------------------------------------------


def investigate_discrepancy(
    db: Session,
    discrepancy_id: int,
    *,
    caller_store_id: int | None,
    actor_id: int | None,
) -> ReceivingDiscrepancy:
    """RECORDED -> INVESTIGATING. Locks the row first so two concurrent
    callers racing to claim the SAME discrepancy serialize: exactly one
    sees status == "RECORDED" under the lock and transitions it; the
    other sees "INVESTIGATING" already and fails cleanly (Session K)."""
    discrepancy = db.execute(
        select(ReceivingDiscrepancy)
        .where(ReceivingDiscrepancy.id == discrepancy_id)
        .with_for_update()
    ).scalar_one_or_none()
    if discrepancy is None:
        raise NotFoundError(f"Discrepancy {discrepancy_id} not found")
    _enforce_store_access(caller_store_id, discrepancy.store_id, "investigating this discrepancy")

    if discrepancy.status != "RECORDED":
        raise ConflictError(
            f"Discrepancy {discrepancy_id} is {discrepancy.status}, not RECORDED -- it has "
            "already been claimed for investigation or resolved",
            error_code="INVALID_DISCREPANCY_STATE",
        )

    before_status = discrepancy.status
    discrepancy.status = "INVESTIGATING"
    discrepancy.investigated_by = actor_id
    discrepancy.investigated_at = datetime.now(UTC)
    db.flush()

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="DISCREPANCY_INVESTIGATION_STARTED",
        entity_type="receiving_discrepancy",
        entity_id=discrepancy.id,
        before={"status": before_status},
        after={"status": "INVESTIGATING"},
    )
    return discrepancy


def resolve_discrepancy(
    db: Session,
    discrepancy_id: int,
    *,
    resolution_type: str,
    resolution_notes: str,
    caller_store_id: int | None,
    actor_id: int | None,
) -> ReceivingDiscrepancy:
    """INVESTIGATING -> RESOLVED only -- never directly from RECORDED
    (docs/M24D_TECHNICAL_CONTRACT.md Section 12.1: "a discrepancy cannot
    be closed out of RECORDED directly"), and never from RESOLVED (no
    reopening in this phase -- that belongs to the write-off/vendor-claim
    denial-reopen paths of a later phase).

    `resolution_type` records the investigation's FINDING only -- plain
    data, never a trigger. No write-off, shrinkage posting, vendor claim,
    AP/AR entry, or physical return is created here, regardless of which
    finding is recorded; that is this function's entire scope boundary."""
    if resolution_type not in DISCREPANCY_RESOLUTION_TYPES:
        raise ValidationAppError(
            f"Invalid resolution_type {resolution_type!r}", error_code="INVALID_RESOLUTION_TYPE"
        )
    if not resolution_notes or not resolution_notes.strip():
        raise ValidationAppError(
            "resolution_notes is required", error_code="RESOLUTION_NOTES_REQUIRED"
        )

    discrepancy = db.execute(
        select(ReceivingDiscrepancy)
        .where(ReceivingDiscrepancy.id == discrepancy_id)
        .with_for_update()
    ).scalar_one_or_none()
    if discrepancy is None:
        raise NotFoundError(f"Discrepancy {discrepancy_id} not found")
    _enforce_store_access(caller_store_id, discrepancy.store_id, "resolving this discrepancy")

    if discrepancy.status != "INVESTIGATING":
        raise ConflictError(
            f"Discrepancy {discrepancy_id} is {discrepancy.status}, not INVESTIGATING -- only "
            "a discrepancy actively under investigation can be resolved",
            error_code="INVALID_DISCREPANCY_STATE",
        )

    before_status = discrepancy.status
    discrepancy.status = "RESOLVED"
    discrepancy.resolved_by = actor_id
    discrepancy.resolved_at = datetime.now(UTC)
    discrepancy.resolution_type = resolution_type
    discrepancy.resolution_notes = resolution_notes
    db.flush()

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="DISCREPANCY_RESOLVED",
        entity_type="receiving_discrepancy",
        entity_id=discrepancy.id,
        before={"status": before_status},
        after={
            "status": "RESOLVED",
            "resolution_type": resolution_type,
            "resolution_notes": resolution_notes,
        },
    )
    return discrepancy
