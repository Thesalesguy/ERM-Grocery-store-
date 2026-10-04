"""API-contract schemas for inter-store transfers: draft creation,
shipment, receipt, cancellation, and in-transit reconciliation."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransferLineCreate(BaseModel):
    source_product_id: int
    requested_quantity: Decimal = Field(gt=0)
    # Explicit override for a deliberate SKU remap; when omitted, the
    # destination product is resolved by matching SKU in the destination
    # store (never auto-created — see Design Decision 6).
    destination_product_id: int | None = None


class TransferCreate(BaseModel):
    from_store_id: int
    to_store_id: int
    requested_date: date
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[TransferLineCreate] = Field(min_length=1, max_length=500)


class TransferLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transfer_id: int
    source_product_id: int
    destination_product_id: int
    requested_quantity: Decimal
    shipped_quantity: Decimal
    received_quantity: Decimal
    damaged_quantity: Decimal
    declared_short_quantity: Decimal
    unit_cost_at_shipment: Decimal | None


class TransferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_store_id: int
    to_store_id: int
    transfer_number: str
    status: str
    requested_date: date
    notes: str | None
    requested_by: int | None
    shipped_by: int | None
    shipped_at: datetime | None
    cancelled_by: int | None
    cancelled_at: datetime | None
    created_at: datetime


class TransferWithLinesRead(TransferRead):
    lines: list[TransferLineRead]


class ShipTransferLineInput(BaseModel):
    transfer_line_id: int
    quantity_to_ship: Decimal = Field(gt=0)


class ShipTransferRequest(BaseModel):
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[ShipTransferLineInput] = Field(min_length=1, max_length=500)


class ReceiveTransferLineInput(BaseModel):
    """M25 Phase 2 (docs/M24D_TECHNICAL_CONTRACT.md Section 5): the actual
    physical result for one line of one receiving event. `quantity_received`
    means "good" (kept unrenamed for compatibility; the field has always
    meant "the quantity made available at the destination"). Existing
    callers that send only `quantity_received` are unaffected -- the two
    new fields default to zero, reproducing today's exact behavior.
    At least one of the three must be positive: an all-zero line is
    rejected as malformed, never silently accepted."""

    transfer_line_id: int
    quantity_received: Decimal = Field(ge=0)
    quantity_damaged: Decimal = Field(ge=0, default=Decimal("0"))
    quantity_declared_short: Decimal = Field(ge=0, default=Decimal("0"))

    @model_validator(mode="after")
    def _at_least_one_quantity_positive(self) -> "ReceiveTransferLineInput":
        if self.quantity_received + self.quantity_damaged + self.quantity_declared_short <= 0:
            raise ValueError(
                "At least one of quantity_received, quantity_damaged, "
                "quantity_declared_short must be positive"
            )
        return self


class ReceiveTransferRequest(BaseModel):
    received_date: date
    notes: str | None = Field(default=None, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[ReceiveTransferLineInput] = Field(min_length=1, max_length=500)


class TransferReceiptItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    inter_store_transfer_line_id: int
    quantity_received: Decimal
    quantity_damaged: Decimal
    quantity_declared_short: Decimal


class TransferReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transfer_id: int
    store_id: int
    client_transaction_id: str
    received_date: date
    received_by: int | None
    notes: str | None
    created_at: datetime


class TransferCustodyAcceptanceRead(BaseModel):
    """M25 Phase 2 (docs/M24D_TECHNICAL_CONTRACT.md Section 11): an
    internal ERP custody acknowledgment, not a legally compliant
    electronic signature."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    transfer_id: int
    inter_store_transfer_receipt_id: int
    store_id: int
    accepted_by: int | None
    accepted_at: datetime
    is_digital_acknowledgment: bool


class TransferReceiptWithItemsRead(TransferReceiptRead):
    items: list[TransferReceiptItemRead]
    custody_acceptance: TransferCustodyAcceptanceRead | None


class TransferCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class InventoryInTransitReconciliationRead(BaseModel):
    gl_in_transit_balance: Decimal
    outstanding_in_transit_total: Decimal
    discrepancy: Decimal
