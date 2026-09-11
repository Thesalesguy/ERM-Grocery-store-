"""API-contract schemas for suppliers, purchase orders, goods receiving,
and purchase returns.

Same untrusted-input discipline as app.modules.sales.schemas: a client
never submits a total, a WAC, or a resulting quantity — only the raw
inputs (product IDs, quantities, costs) the server needs to compute
everything else from authoritative data.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    tax_id: str | None = Field(default=None, max_length=100)


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    tax_id: str | None = Field(default=None, max_length=100)

    model_config = ConfigDict(extra="forbid")


class SupplierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str | None
    contact_name: str | None
    phone: str | None
    email: str | None
    address: str | None
    tax_id: str | None
    is_active: bool


class PurchaseOrderItemCreate(BaseModel):
    product_id: int
    quantity_ordered: Decimal = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)


class PurchaseOrderCreate(BaseModel):
    store_id: int
    supplier_id: int
    order_date: date
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    # Bounded (M2 hardening audit Section 13 precedent applied here too):
    # generous for any real order, bounded against an abusive payload.
    lines: list[PurchaseOrderItemCreate] = Field(min_length=1, max_length=500)


class PurchaseOrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity_ordered: Decimal
    quantity_received: Decimal
    unit_cost: Decimal
    # Derived, not stored (docs/M3_PURCHASING_RECEIVING_WAC.md
    # "over-receipt policy") — never a stored column trusted at read
    # time, always computed fresh from the two quantities above.
    quantity_remaining: Decimal = Decimal("0")
    is_over_received: bool = False
    product_name: str | None = None
    product_sku: str | None = None


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    purchase_number: str
    status: str
    order_date: date
    expected_date: date | None
    notes: str | None
    created_by: int | None
    created_at: datetime
    items: list[PurchaseOrderItemRead]
    supplier_name: str | None = None


class PurchaseOrderCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class GoodsReceiptLineCreate(BaseModel):
    purchase_order_item_id: int
    quantity_received: Decimal = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)
    condition_notes: str | None = Field(default=None, max_length=500)


class GoodsReceiptCreate(BaseModel):
    received_date: date
    notes: str | None = Field(default=None, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[GoodsReceiptLineCreate] = Field(min_length=1, max_length=500)


class GoodsReceiptItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_order_item_id: int
    quantity_received: Decimal
    unit_cost: Decimal
    condition_notes: str | None


class GoodsReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_order_id: int
    store_id: int
    client_transaction_id: str
    received_date: date
    received_by: int | None
    notes: str | None
    created_at: datetime
    items: list[GoodsReceiptItemRead]


class PurchaseReturnLineCreate(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0)


class PurchaseReturnCreate(BaseModel):
    store_id: int
    return_date: date
    reason: str | None = Field(default=None, max_length=1000)
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[PurchaseReturnLineCreate] = Field(min_length=1, max_length=500)


class PurchaseReturnItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: Decimal
    unit_cost: Decimal


class PurchaseReturnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_order_id: int
    store_id: int
    client_transaction_id: str
    return_date: date
    reason: str | None
    created_by: int | None
    created_at: datetime
    items: list[PurchaseReturnItemRead]
