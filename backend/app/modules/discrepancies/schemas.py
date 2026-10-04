"""Pydantic schemas for the receiving-discrepancy investigation ledger."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResolutionType = Literal[
    "VENDOR_CAUSED", "SOURCE_STORE_CAUSED", "DESTINATION_CAUSED", "TRANSIT_DAMAGE", "UNKNOWN"
]


class ReceivingDiscrepancyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_type: str
    source_id: int
    transfer_id: int | None
    product_id: int
    store_id: int
    counterparty_store_id: int | None
    supplier_id: int | None
    discrepancy_type: str
    quantity_short: Decimal
    quantity_damaged: Decimal
    unit_cost: Decimal
    status: str
    raised_by: int | None
    raised_at: datetime
    investigated_by: int | None
    investigated_at: datetime | None
    resolved_by: int | None
    resolved_at: datetime | None
    resolution_type: str | None
    resolution_notes: str | None


class DiscrepancyResolveRequest(BaseModel):
    resolution_type: ResolutionType
    # Mandatory reason, matching every other irreversible-correction
    # transition in this codebase (docs/M24D_TECHNICAL_CONTRACT.md
    # Section 12.1) -- the service layer re-validates this; the schema
    # bound only rejects an obviously-empty request earlier.
    resolution_notes: str = Field(min_length=1, max_length=2000)
