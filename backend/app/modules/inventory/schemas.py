"""API-contract schemas for the inventory-movement ledger, current stock
levels, and manual stock adjustments."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InventoryMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    product_id: int
    movement_type: str
    quantity_delta: Decimal
    unit_cost_at_movement: Decimal
    resulting_quantity_on_hand: Decimal
    reference_type: str
    reference_id: int | None
    reason: str | None
    created_by: int | None
    created_at: datetime


class StockLevelRead(BaseModel):
    """Current-stock-at-a-glance view — the product cache columns, not
    the ledger. See app.modules.inventory.service.get_quantity_on_hand_from_ledger
    for the authoritative (but slower) ledger-derived value."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    sku: str
    name: str
    current_qty_on_hand: Decimal
    current_cost: Decimal
    reorder_point: Decimal | None
    is_low_stock: bool
    is_active: bool


class StockAdjustmentCreate(BaseModel):
    product_id: int
    quantity_delta: Decimal = Field(
        description="Signed: positive increases stock, negative decreases it"
    )
    reason_code: str = Field(pattern="^(DAMAGE|THEFT|EXPIRY|STOCKTAKE_CORRECTION|OTHER)$")
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("quantity_delta")
    @classmethod
    def _delta_must_be_nonzero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("quantity_delta must not be zero")
        return value


class StockAdjustmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    product_id: int
    quantity_delta: Decimal
    reason_code: str
    notes: str | None
    created_by: int | None
    approved_by: int | None
    created_at: datetime
