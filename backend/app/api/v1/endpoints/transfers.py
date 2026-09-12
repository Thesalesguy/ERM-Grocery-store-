"""Inter-store transfer endpoints: draft creation, shipment, receipt,
cancellation, and in-transit reconciliation.

Three permission tiers — `inventory.transfer.write` (create/cancel a
draft), `inventory.transfer.ship`, `inventory.transfer.receive` — see
app.modules.auth.permissions and docs/M8_ADVANCED_INVENTORY_DESIGN.md
"Design Decision 10".
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import (
    INVENTORY_READ,
    INVENTORY_TRANSFER_RECEIVE,
    INVENTORY_TRANSFER_SHIP,
    INVENTORY_TRANSFER_WRITE,
)
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.transfers import service
from app.modules.transfers.schemas import (
    InventoryInTransitReconciliationRead,
    ReceiveTransferRequest,
    ShipTransferRequest,
    TransferCancelRequest,
    TransferCreate,
    TransferRead,
    TransferReceiptWithItemsRead,
    TransferWithLinesRead,
)
from app.modules.transfers.schemas import (
    TransferLineCreate as _TransferLineCreateSchema,
)
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput

router = APIRouter(prefix="/transfers", tags=["transfers"])

_read_permission = require_permission(INVENTORY_READ)
_write_permission = require_permission(INVENTORY_TRANSFER_WRITE)
_ship_permission = require_permission(INVENTORY_TRANSFER_SHIP)
_receive_permission = require_permission(INVENTORY_TRANSFER_RECEIVE)


def _to_line_input(line: _TransferLineCreateSchema) -> TransferLineInput:
    return TransferLineInput(
        source_product_id=line.source_product_id,
        requested_quantity=line.requested_quantity,
        destination_product_id=line.destination_product_id,
    )


@router.post("", response_model=TransferWithLinesRead, status_code=201)
def create_transfer(
    payload: TransferCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> TransferWithLinesRead:
    transfer = service.create_transfer(
        db,
        from_store_id=payload.from_store_id,
        to_store_id=payload.to_store_id,
        requested_date=payload.requested_date,
        lines=[_to_line_input(line) for line in payload.lines],
        notes=payload.notes,
        requested_by=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return TransferWithLinesRead.model_validate(transfer)


@router.get("", response_model=list[TransferRead])
def list_transfers(
    from_store_id: int | None = None,
    to_store_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[TransferRead]:
    # Transfers are inherently two-store documents, so a store-scoped user
    # is shown any transfer touching their store on EITHER side — a
    # single-sided filter would hide their own outbound or inbound
    # transfers depending on which side they're scoped to.
    if current_user.store_id is not None:
        transfers = [
            t
            for t in service.list_transfers(db, status=status, limit=limit, offset=offset)
            if current_user.store_id in (t.from_store_id, t.to_store_id)
        ]
    else:
        transfers = service.list_transfers(
            db,
            from_store_id=from_store_id,
            to_store_id=to_store_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    return [TransferRead.model_validate(t) for t in transfers]


@router.get("/{transfer_id}", response_model=TransferWithLinesRead)
def get_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> TransferWithLinesRead:
    transfer = service.get_transfer(db, transfer_id)
    if current_user.store_id is not None and current_user.store_id not in (
        transfer.from_store_id,
        transfer.to_store_id,
    ):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    return TransferWithLinesRead.model_validate(transfer)


@router.post("/{transfer_id}/ship", response_model=TransferWithLinesRead)
def ship_transfer(
    transfer_id: int,
    payload: ShipTransferRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_ship_permission),
) -> TransferWithLinesRead:
    transfer = service.ship_transfer(
        db,
        transfer_id=transfer_id,
        lines=[
            ShipLineInput(
                transfer_line_id=line.transfer_line_id,
                quantity_to_ship=line.quantity_to_ship,
            )
            for line in payload.lines
        ],
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        shipped_by=current_user.id,
    )
    db.commit()
    db.refresh(transfer)
    return TransferWithLinesRead.model_validate(transfer)


@router.post(
    "/{transfer_id}/receipts", response_model=TransferReceiptWithItemsRead, status_code=201
)
def receive_transfer(
    transfer_id: int,
    payload: ReceiveTransferRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_receive_permission),
) -> TransferReceiptWithItemsRead:
    receipt = service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=payload.received_date,
        lines=[
            ReceiveLineInput(
                transfer_line_id=line.transfer_line_id,
                quantity_received=line.quantity_received,
            )
            for line in payload.lines
        ],
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        received_by=current_user.id,
        notes=payload.notes,
    )
    db.commit()
    db.refresh(receipt)
    return TransferReceiptWithItemsRead.model_validate(receipt)


@router.get("/{transfer_id}/receipts", response_model=list[TransferReceiptWithItemsRead])
def list_transfer_receipts(
    transfer_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[TransferReceiptWithItemsRead]:
    transfer = service.get_transfer(db, transfer_id)
    if current_user.store_id is not None and current_user.store_id not in (
        transfer.from_store_id,
        transfer.to_store_id,
    ):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    receipts = service.list_transfer_receipts(db, transfer_id=transfer_id)
    return [TransferReceiptWithItemsRead.model_validate(r) for r in receipts]


@router.post("/{transfer_id}/cancel", response_model=TransferRead)
def cancel_transfer(
    transfer_id: int,
    payload: TransferCancelRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> TransferRead:
    transfer = service.cancel_transfer(
        db,
        transfer_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        reason=payload.reason,
    )
    return TransferRead.model_validate(transfer)


@router.get(
    "/reports/inventory-in-transit-reconciliation",
    response_model=InventoryInTransitReconciliationRead,
)
def inventory_in_transit_reconciliation(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> InventoryInTransitReconciliationRead:
    row = service.inventory_in_transit_reconciliation(db)
    return InventoryInTransitReconciliationRead(**row.__dict__)
