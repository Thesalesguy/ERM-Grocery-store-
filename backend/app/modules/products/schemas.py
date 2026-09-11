"""API-contract schemas for products and their barcodes.

These are deliberately separate from app.modules.products.models — the
SQLAlchemy models are never returned directly from a route handler. Note
what's absent from ProductCreate/ProductUpdate: `current_cost` and
`current_qty_on_hand` are never client-settable. They start at zero and
move only through the inventory ledger (app.modules.inventory.service),
never a catalog edit. `sku` and `store_id` are also absent from
ProductUpdate — they're stable identifiers, not editable catalog fields.
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


class ProductUpdate(BaseModel):
    """Every field optional: only fields actually present in the request
    body are changed (see service.update_product's exclude_unset use)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    category_id: int | None = None
    default_supplier_id: int | None = None
    unit_of_measure: str | None = Field(default=None, pattern="^(each|kg|g|l|ml)$")
    is_weighed: bool | None = None
    current_price: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: int | None = None
    reorder_point: Decimal | None = Field(default=None, ge=0)
    allow_negative_stock: bool | None = None

    model_config = ConfigDict(extra="forbid")


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


class ProductBarcodeCreate(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    barcode_type: str = Field(
        default="EAN13", pattern="^(EAN13|UPC_A|CODE128|WEIGHT_EMBEDDED|INTERNAL)$"
    )
    pack_quantity: Decimal = Field(default=Decimal("1"), gt=0)
    is_primary: bool = False


class ProductBarcodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    barcode: str
    barcode_type: str
    pack_quantity: Decimal
    is_primary: bool


class ProductWithBarcodesRead(ProductRead):
    barcodes: list[ProductBarcodeRead] = []
