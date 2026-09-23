"""API-contract schemas for the Accounts Payable module.

`PurchaseInvoiceLineCreate` carries only quantity/unit_price/discount/tax
per line — never a line_total or an invoice grand_total; every derived
dollar figure in a `PurchaseInvoiceRead` is computed server-side by
app.modules.ap.service, then frozen once posted.

See docs/M7_ADVANCED_AP_SETTLEMENT.md for the M7 additions: an optional
(not required) `purchase_order_id` on invoice creation, persisted
receipt-match detail, multi-invoice payment allocation, and supplier
credit notes.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.ap.models import SUPPLIER_CREDIT_NOTE_REASONS, SUPPLIER_PAYMENT_METHODS


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
    # M7: optional — an invoice's lines may span multiple purchase orders;
    # this is a display/filter convenience only, never a validation input
    # (see app.modules.ap.service.create_purchase_invoice's docstring).
    purchase_order_id: int | None = None
    invoice_number: str = Field(min_length=1, max_length=100)
    invoice_date: date
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)
    lines: list[PurchaseInvoiceLineCreate] = Field(min_length=1, max_length=500)


class VoidPurchaseInvoiceRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class SupplierReversalRequest(BaseModel):
    """M16: reason is mandatory (unlike a void's optional reason) —
    mirrors app.modules.payroll.schemas.PayrollPeriodReverseInput
    exactly, since reversing a settled payment/credit note is a
    correction action, not a routine cancellation."""

    reason: str = Field(min_length=1, max_length=2000)


class PurchaseInvoiceReceiptMatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_invoice_line_id: int
    goods_receipt_item_id: int
    matched_quantity: Decimal
    matched_unit_cost: Decimal
    variance_amount: Decimal


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
    matches: list[PurchaseInvoiceReceiptMatchRead] = Field(default_factory=list)


class PurchaseInvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    purchase_order_id: int | None
    invoice_number: str
    invoice_date: date
    due_date: date
    status: str
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    amount_paid: Decimal
    amount_credited: Decimal
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
    purchase_order_id: int
    product_id: int
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_invoiced: Decimal
    quantity_invoiceable: Decimal
    product_name: str | None = None
    product_sku: str | None = None


class PurchaseOrderMatchingStatusRead(BaseModel):
    purchase_order_ids: list[int]
    items: list[PurchaseOrderItemMatchStatusRead]


class PaymentAllocationCreate(BaseModel):
    purchase_invoice_id: int
    amount: Decimal = Field(gt=0)


class SupplierPaymentCreate(BaseModel):
    store_id: int
    supplier_id: int
    payment_date: date
    payment_method: str
    amount: Decimal = Field(gt=0)
    allocations: list[PaymentAllocationCreate] = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=255)
    client_transaction_id: str = Field(min_length=1, max_length=100)

    @field_validator("payment_method")
    @classmethod
    def _valid_method(cls, value: str) -> str:
        if value not in SUPPLIER_PAYMENT_METHODS:
            raise ValueError(f"payment_method must be one of {SUPPLIER_PAYMENT_METHODS}")
        return value


class PaymentAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_invoice_id: int
    amount: Decimal


class SupplierPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    payment_date: date
    payment_method: str
    amount: Decimal
    reference: str | None
    client_transaction_id: str
    created_at: datetime
    allocations: list[PaymentAllocationRead] = Field(default_factory=list)


class CreditAllocationCreate(BaseModel):
    purchase_invoice_id: int
    amount: Decimal = Field(gt=0)


class SupplierCreditNoteLineCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0)
    product_id: int | None = None
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)


class SupplierCreditNoteCreate(BaseModel):
    store_id: int
    supplier_id: int
    credit_number: str = Field(min_length=1, max_length=100)
    credit_date: date
    reason: str
    purchase_return_id: int | None = None
    lines: list[SupplierCreditNoteLineCreate] = Field(min_length=1, max_length=200)
    allocations: list[CreditAllocationCreate] = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)

    @field_validator("reason")
    @classmethod
    def _valid_reason(cls, value: str) -> str:
        if value not in SUPPLIER_CREDIT_NOTE_REASONS:
            raise ValueError(f"reason must be one of {SUPPLIER_CREDIT_NOTE_REASONS}")
        return value

    @model_validator(mode="after")
    def _reason_reference_consistent(self) -> "SupplierCreditNoteCreate":
        if self.reason == "GOODS_RETURN" and self.purchase_return_id is None:
            raise ValueError("reason=GOODS_RETURN requires purchase_return_id")
        if self.reason == "COMMERCIAL_DISCOUNT" and self.purchase_return_id is not None:
            raise ValueError("reason=COMMERCIAL_DISCOUNT must not set purchase_return_id")
        return self


class SupplierCreditNoteLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None
    description: str
    quantity: Decimal | None
    unit_cost: Decimal | None
    amount: Decimal


class SupplierCreditAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_invoice_id: int
    amount: Decimal


class SupplierCreditNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    supplier_id: int
    credit_number: str
    credit_date: date
    reason: str
    purchase_return_id: int | None
    grand_total: Decimal
    amount_allocated: Decimal
    client_transaction_id: str
    notes: str | None
    created_at: datetime
    voided_at: datetime | None
    supplier_name: str | None = None
    lines: list[SupplierCreditNoteLineRead] = Field(default_factory=list)
    allocations: list[SupplierCreditAllocationRead] = Field(default_factory=list)


class SupplierApSummaryRead(BaseModel):
    supplier_id: int
    total_owed: Decimal
    total_overdue: Decimal
    total_current: Decimal
    total_paid: Decimal
    total_credited: Decimal
    outstanding_purchase_clearing: Decimal


class SupplierTransactionRead(BaseModel):
    transaction_type: str
    id: int
    date: date
    reference: str
    amount: Decimal
    status: str


class SupplierStatementLineRead(BaseModel):
    date: date
    transaction_type: str
    reference: str
    amount: Decimal
    running_balance: Decimal


class SupplierStatementRead(BaseModel):
    supplier_id: int
    date_from: date | None
    date_to: date | None
    opening_balance: Decimal
    lines: list[SupplierStatementLineRead]
    closing_balance: Decimal


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
