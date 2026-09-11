"""API-contract schemas for products.

These are deliberately separate from app.modules.products.models — the
SQLAlchemy models are never returned directly from a route handler. Note
what's absent from ProductCreate: `current_cost` and `current_qty_on_hand`
are not client-settable. They start at zero and move only through the
inventory ledger (app.modules.inventory.service), never a catalog edit.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    store_id: int
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    category_id: int | None = None
    default_supplier_id: int | None = None
    unit_of_measure: str = Field(default="each", pattern="^(each|kg|g|l|ml)$")
    is_weighed: bool = False
    current_price: Decimal = Field(ge=0)
    tax_rate_id: int | None = None
    reorder_point: Decimal | None = Field(default=None, ge=0)
    allow_negative_stock: bool = False


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    sku: str
    name: str
    description: str | None
    category_id: int | None
    default_supplier_id: int | None
    unit_of_measure: str
    is_weighed: bool
    current_price: Decimal
    current_cost: Decimal
    tax_rate_id: int | None
    reorder_point: Decimal | None
    current_qty_on_hand: Decimal
    allow_negative_stock: bool
    is_active: bool
