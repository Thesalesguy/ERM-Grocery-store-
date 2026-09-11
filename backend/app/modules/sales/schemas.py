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

from datetime import date, datetime
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
    # Idempotency key (M2 hardening audit Section 7): the POS generates
    # one UUID per checkout attempt (crypto.randomUUID() client-side) and
    # resends the SAME value on any retry of that same attempt. Required,
    # not optional — "disable the button after one click" alone does not
    # protect against a network-level retry, so the server enforces this
    # via a UNIQUE database constraint, not just goodwill from the client.
    client_transaction_id: str = Field(min_length=1, max_length=100)
    # Upper bounds (M2 hardening audit Section 13): generous enough for
    # any real POS cart or split-tender payment, but bounded so a
    # malicious or malformed request can't force the server to lock and
    # process an unbounded number of rows in one request.
    lines: list[SaleLineCreate] = Field(min_length=1, max_length=500)
    payments: list[PaymentCreate] = Field(min_length=1, max_length=50)


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
    quantity_returned: Decimal
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
    client_transaction_id: str
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


# --- Sale returns / voids (M5) -----------------------------------------
#
# SaleReturnLineCreate/SaleReturnCreate/VoidSaleCreate deliberately carry
# NO price/discount/tax/cost/refund_amount fields — a return's dollar
# values are always derived server-side from the ORIGINAL sale's frozen
# data (docs/M5_RETURNS_VOIDS_REFUNDS.md "Return pricing"), never
# accepted from the client. Only identifiers and quantities.


class SaleReturnLineCreate(BaseModel):
    sale_item_id: int
    quantity: Decimal = Field(gt=0)
    restock: bool = True


class SaleReturnCreate(BaseModel):
    store_id: int
    return_date: date
    client_transaction_id: str = Field(min_length=1, max_length=100)
    refund_method: str
    reason: str | None = Field(default=None, max_length=2000)
    lines: list[SaleReturnLineCreate] = Field(min_length=1, max_length=500)

    @field_validator("refund_method")
    @classmethod
    def _valid_refund_method(cls, value: str) -> str:
        if value not in PAYMENT_METHODS:
            raise ValueError(f"refund_method must be one of {PAYMENT_METHODS}")
        return value


class VoidSaleCreate(BaseModel):
    store_id: int
    return_date: date
    client_transaction_id: str = Field(min_length=1, max_length=100)
    refund_method: str
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("refund_method")
    @classmethod
    def _valid_refund_method(cls, value: str) -> str:
        if value not in PAYMENT_METHODS:
            raise ValueError(f"refund_method must be one of {PAYMENT_METHODS}")
        return value


class SaleReturnItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sale_item_id: int
    quantity: Decimal
    unit_price_refunded: Decimal
    discount_refunded: Decimal
    tax_refunded: Decimal
    unit_cost_refunded: Decimal
    restock: bool
    product_id: int | None = None
    product_name: str | None = None
    product_sku: str | None = None


class SaleReturnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sale_id: int
    store_id: int
    return_number: str
    client_transaction_id: str
    reason: str | None
    refund_method: str
    refund_amount: Decimal
    processed_by: int | None
    created_at: datetime
    items: list[SaleReturnItemRead] = Field(default_factory=list)


class SaleItemReturnEligibilityRead(BaseModel):
    sale_item_id: int
    product_id: int
    quantity: Decimal
    quantity_returned: Decimal
    quantity_returnable: Decimal
    product_name: str | None = None
    product_sku: str | None = None


class SaleReturnEligibilityRead(BaseModel):
    sale_id: int
    sale_status: str
    items: list[SaleItemReturnEligibilityRead]
