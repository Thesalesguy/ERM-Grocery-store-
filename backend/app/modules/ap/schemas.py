"""API-contract schemas for the Accounts Payable module.

`PurchaseInvoiceLineCreate` carries only quantity/unit_price/discount/tax
per line — never a line_total or an invoice grand_total; every derived
dollar figure in a `PurchaseInvoiceRead` is computed server-side by
app.modules.ap.service, then frozen once posted (docs/M6_AP_VENDOR_ACCOUNTING.md
"Purchase invoice lines").
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.ap.models import SUPPLIER_PAYMENT_METHODS


class PurchaseInvoiceLineCreate(BaseModel):
    purchase_order_item_id: int
    quantity_invoiced: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0)
    description: str | None = Field(default=None, max_length=500)


class PurchaseInvoiceCreate(BaseModel):
    store_id: int
    supplier_id: int
    purchase_order_id: int
    invoice_number: str = Field(min_length=1, max_length=100)
    invoice_date: date
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[PurchaseInvoiceLineCreate] = Field(min_length=1, max_length=500)


class VoidPurchaseInvoiceRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class PurchaseInvoiceLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_order_item_id: int
    product_id: int
    description: str
    quantity_invoiced: Decimal
    unit_price: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal
    product_name: str | None = None
    product_sku: str | None = None


class PurchaseInvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    purchase_order_id: int
    invoice_number: str
    invoice_date: date
    due_date: date
    status: str
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal = Decimal("0")  # computed at read time, never stored
    client_transaction_id: str
    notes: str | None
    posted_at: datetime | None
    voided_at: datetime | None
    created_at: datetime
    supplier_name: str | None = None
    lines: list[PurchaseInvoiceLineRead] = Field(default_factory=list)


class PurchaseOrderItemMatchStatusRead(BaseModel):
    purchase_order_item_id: int
    product_id: int
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_invoiced: Decimal
    quantity_invoiceable: Decimal
    product_name: str | None = None
    product_sku: str | None = None


class PurchaseOrderMatchingStatusRead(BaseModel):
    purchase_order_id: int
    items: list[PurchaseOrderItemMatchStatusRead]


class SupplierPaymentCreate(BaseModel):
    store_id: int
    payment_date: date
    payment_method: str
    amount: Decimal = Field(gt=0)
    reference: str | None = Field(default=None, max_length=255)
    client_transaction_id: str = Field(min_length=1, max_length=100)

    @field_validator("payment_method")
    @classmethod
    def _valid_method(cls, value: str) -> str:
        if value not in SUPPLIER_PAYMENT_METHODS:
            raise ValueError(f"payment_method must be one of {SUPPLIER_PAYMENT_METHODS}")
        return value


class SupplierPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    purchase_invoice_id: int
    payment_date: date
    payment_method: str
    amount: Decimal
    reference: str | None
    client_transaction_id: str
    created_at: datetime


class SupplierApSummaryRead(BaseModel):
    supplier_id: int
    total_owed: Decimal
    total_overdue: Decimal
    total_current: Decimal
    total_paid: Decimal
    outstanding_purchase_clearing: Decimal


class SupplierTransactionRead(BaseModel):
    transaction_type: str
    id: int
    date: date
    reference: str
    amount: Decimal
    status: str


class ApAgingRowRead(BaseModel):
    supplier_id: int
    supplier_name: str | None = None
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_over_90: Decimal
    total: Decimal


class ApReconciliationRead(BaseModel):
    store_id: int | None
    gl_accounts_payable_balance: Decimal
    ap_subledger_total: Decimal
    discrepancy: Decimal


class PurchaseClearingReconciliationRead(BaseModel):
    store_id: int | None
    gl_purchase_clearing_balance: Decimal
    outstanding_clearing_total: Decimal
    discrepancy: Decimal
