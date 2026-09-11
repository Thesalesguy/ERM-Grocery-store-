"""API-contract schema for reading the inventory-movement ledger."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


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
