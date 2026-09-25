"""API-contract schemas for cashier/till shift sessions (M15).

`ShiftOpenCreate`/`ShiftCloseCreate`/`CashMovementCreate` carry only what
the client can legitimately supply (an opening float, a physical count, a
cash-movement amount/reason) — expected cash and variance are always
computed server-side from authoritative Sale/SaleReturn/CashMovement rows
(see app.modules.shifts.service._compute_expected_cash), never accepted
from the client, mirroring how SaleCreate never accepts a price/tax/total.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.shifts.models import CASH_MOVEMENT_TYPES


class ShiftOpenCreate(BaseModel):
    store_id: int
    opening_float: Decimal = Field(ge=0)
    # Idempotency key (mirrors SaleCreate.client_transaction_id exactly).
    client_transaction_id: str = Field(min_length=1, max_length=100)


class ShiftCloseCreate(BaseModel):
    closing_counted_amount: Decimal = Field(ge=0)
    # A SEPARATE idempotency key from the shift's own opening
    # client_transaction_id — see CashierShift.close_client_transaction_id's
    # docstring for why.
    client_transaction_id: str = Field(min_length=1, max_length=100)


class ShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    cashier_id: int
    status: str
    opening_float: Decimal
    opened_at: datetime
    closed_at: datetime | None
    closing_counted_amount: Decimal | None
    expected_cash_amount: Decimal | None
    variance_amount: Decimal | None
    closed_by: int | None
    created_at: datetime


class CashMovementCreate(BaseModel):
    movement_type: str
    amount: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)

    @field_validator("movement_type")
    @classmethod
    def _valid_movement_type(cls, value: str) -> str:
        if value not in CASH_MOVEMENT_TYPES:
            raise ValueError(f"movement_type must be one of {CASH_MOVEMENT_TYPES}")
        return value


class CashMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shift_id: int
    movement_type: str
    amount: Decimal
    reason: str
    created_by: int
    created_at: datetime
