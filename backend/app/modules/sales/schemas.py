"""API-contract schemas for POS sale finalization and receipts.

`SaleCreate` is the untrusted client input (M2 task Section 14: "the
server must recalculate the transaction from authoritative product/tax
data... if the client submits a total, treat it as untrusted input") — it
carries only product IDs, quantities, and an optional per-line discount,
never a price, a tax amount, or a grand total. Every dollar figure in a
`SaleRead` response is computed server-side by
app.modules.sales.service.finalize_sale from the product catalog and tax
tables, then frozen.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.sales.models import PAYMENT_METHODS


class SaleLineCreate(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)


class PaymentCreate(BaseModel):
    payment_method: str
    amount: Decimal = Field(gt=0)
    reference: str | None = Field(default=None, max_length=255)

    @field_validator("payment_method")
    @classmethod
    def _valid_method(cls, value: str) -> str:
        if value not in PAYMENT_METHODS:
            raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")
        return value


class SaleCreate(BaseModel):
    store_id: int
    lines: list[SaleLineCreate] = Field(min_length=1)
    payments: list[PaymentCreate] = Field(min_length=1)


class SaleItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: Decimal
    unit_price_at_sale: Decimal
    unit_cost_at_sale: Decimal
    discount_amount: Decimal
    tax_rate_id: int | None
    tax_amount: Decimal
    line_total: Decimal
    # Denormalized for receipt display — resolved at read time from the
    # product catalog. NOT frozen (a renamed product shows its new name
    # on an old receipt) since a product's *name* isn't one of the
    # financial fields BR-2 requires to stay historically frozen; only
    # price/cost/tax/discount are.
    product_name: str | None = None
    product_sku: str | None = None


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_method: str
    amount: Decimal
    reference: str | None


class SaleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    sale_number: str
    cashier_id: int
    status: str
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    amount_tendered: Decimal | None
    change_due: Decimal | None
    completed_at: datetime | None
    created_at: datetime
    items: list[SaleItemRead]
    payments: list[PaymentRead]
