"""ORM models for the Accounts Payable module: supplier invoices, their
lines, persisted receipt-lot matches, supplier payments and their
allocations, and supplier credit notes.

See docs/M7_ADVANCED_AP_SETTLEMENT.md for the full M7 design and
docs/M6_AP_VENDOR_ACCOUNTING.md for the original M6 design. Summary of the
load-bearing M7 decisions layered on top of M6:

- `PurchaseInvoice.purchase_order_id` is nullable and no longer a
  validation boundary: an invoice's lines may span multiple purchase
  orders (each line's own `purchase_order_item_id` is what is validated).
  It is kept only as an optional "primary PO" for display/filtering.
- `PurchaseInvoiceReceiptMatch` persists exactly what
  `post_purchase_invoice`'s FIFO walk consumed from each `GoodsReceiptItem`
  lot — quantity, unit cost, and the resulting variance — so a void or an
  audit never needs to recompute FIFO (which is unsafe once other invoices
  have posted against the same PO items in between).
- `SupplierPayment` is now a payment header; `SupplierPaymentAllocation`
  rows (one per invoice it settles) replace the old single
  `purchase_invoice_id` FK. A payment's allocations must sum to exactly
  its `amount` — see `app.modules.ap.service.record_supplier_payment`.
- `SupplierCreditNote` (+ lines + allocations) is new: an immutable,
  single-step (no DRAFT) financial instrument that reduces AP. Allocations
  must sum to exactly the credit's `grand_total` at creation time — there
  is no unapplied-credit balance to race over later (see the design doc's
  "Deferred" section for why unapplied balances are rejected outright,
  not modeled).
- `PurchaseInvoice.amount_credited` mirrors `amount_paid` exactly (a
  maintained cache, `amount_paid + amount_credited <= grand_total`); an
  invoice's true outstanding balance everywhere in this module is
  `grand_total - amount_paid - amount_credited`.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin

PURCHASE_INVOICE_STATUSES = ("DRAFT", "POSTED", "PARTIALLY_PAID", "PAID", "VOIDED")
# Mutable-after-posting only via app.modules.ap.service.void_purchase_invoice,
# and only while amount_paid == 0 and amount_credited == 0 — see that
# function's docstring.
_POSTED_INVOICE_STATUSES = ("POSTED", "PARTIALLY_PAID", "PAID")

# Deliberately NOT app.modules.sales.models.PAYMENT_METHODS reused — CARD
# and MOBILE_MONEY describe how a *customer* paid *this* business, not how
# this business settles a *supplier*; CHEQUE (a very ordinary B2B vendor
# settlement instrument) is added instead. See
# accounting/constants.py SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE.
SUPPLIER_PAYMENT_METHODS = ("CASH", "BANK_TRANSFER", "CHEQUE", "OTHER")

# M7: the two supported, individually-accounted-for credit-note reasons
# (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 13 "Credit note accounting").
# GOODS_RETURN requires a purchase_return_id reference (goods physically
# left inventory, already recorded by the existing M3 PurchaseReturn
# workflow) and posts Dr AP / Cr Inventory. COMMERCIAL_DISCOUNT requires no
# physical-goods reference and posts Dr AP / Cr Purchase Discounts.
SUPPLIER_CREDIT_NOTE_REASONS = ("GOODS_RETURN", "COMMERCIAL_DISCOUNT")


class PurchaseInvoice(TimestampMixin, Base):
    __tablename__ = "purchase_invoices"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "invoice_number", name="uq_purchase_invoices_supplier_invoice_number"
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(PURCHASE_INVOICE_STATUSES) + "')",
            name="ck_purchase_invoices_status",
        ),
        CheckConstraint("subtotal >= 0", name="ck_purchase_invoices_subtotal_non_negative"),
        CheckConstraint("discount_total >= 0", name="ck_purchase_invoices_discount_non_negative"),
        CheckConstraint("tax_total >= 0", name="ck_purchase_invoices_tax_non_negative"),
        CheckConstraint(
            "grand_total = subtotal - discount_total + tax_total",
            name="ck_purchase_invoices_grand_total_consistent",
        ),
        CheckConstraint("grand_total >= 0", name="ck_purchase_invoices_grand_total_non_negative"),
        CheckConstraint("amount_paid >= 0", name="ck_purchase_invoices_amount_paid_non_negative"),
        CheckConstraint(
            "amount_credited >= 0", name="ck_purchase_invoices_amount_credited_non_negative"
        ),
        CheckConstraint(
            "amount_paid + amount_credited <= grand_total",
            name="ck_purchase_invoices_settlement_bounds",
        ),
        Index("ix_purchase_invoices_store_id", "store_id"),
        Index("ix_purchase_invoices_supplier_id", "supplier_id"),
        Index("ix_purchase_invoices_purchase_order_id", "purchase_order_id"),
        Index("ix_purchase_invoices_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    # M7: nullable — a "primary PO" display/filter convenience only. Lines
    # are validated against their OWN purchase_order_item_id, not this
    # column (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 1).
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Maintained caches, updated only under a lock on this row by
    # record_supplier_payment / apply_supplier_credit_note /
    # void_purchase_invoice. balance_due is deliberately NOT a stored
    # column (grand_total - amount_paid - amount_credited, computed at
    # read time) to avoid a second source of truth.
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    amount_credited: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["PurchaseInvoiceLine"]] = relationship(back_populates="purchase_invoice")


class PurchaseInvoiceLine(TimestampMixin, Base):
    __tablename__ = "purchase_invoice_lines"
    __table_args__ = (
        CheckConstraint("quantity_invoiced > 0", name="ck_purchase_invoice_lines_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_purchase_invoice_lines_price_non_negative"),
        CheckConstraint(
            "discount_amount >= 0", name="ck_purchase_invoice_lines_discount_non_negative"
        ),
        CheckConstraint("tax_amount >= 0", name="ck_purchase_invoice_lines_tax_non_negative"),
        CheckConstraint(
            "line_total = round(quantity_invoiced * unit_price - discount_amount + tax_amount, 2)",
            name="ck_purchase_invoice_lines_total_consistent",
        ),
        Index("ix_purchase_invoice_lines_purchase_invoice_id", "purchase_invoice_id"),
        Index("ix_purchase_invoice_lines_purchase_order_item_id", "purchase_order_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoices.id"), nullable=False
    )
    purchase_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order_items.id"), nullable=False
    )
    # Snapshot, not a live FK traversal (BR-2: a historical invoice must
    # reproduce identically even if the product is later renamed/deleted).
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity_invoiced: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    purchase_invoice: Mapped[PurchaseInvoice] = relationship(back_populates="lines")
    matches: Mapped[list["PurchaseInvoiceReceiptMatch"]] = relationship(
        back_populates="purchase_invoice_line"
    )


class PurchaseInvoiceReceiptMatch(TimestampMixin, Base):
    """One FIFO-consumed slice of a GoodsReceiptItem lot, persisted at
    `post_purchase_invoice` time (docs/M7_ADVANCED_AP_SETTLEMENT.md
    Section 1: "a matching record should preserve invoice line / receipt
    item / matched quantity / matched unit cost / variance / timestamps").

    This is what lets `void_purchase_invoice` undo an exact posting
    without recomputing FIFO (unsafe once other invoices have posted
    against the same PO item in between — the receipt-lot consumption
    order they'd see has shifted) and what lets an auditor answer "why
    does this invoice's Purchase Clearing debit have this value" by
    reading real rows instead of trusting a rederivation.
    """

    __tablename__ = "purchase_invoice_receipt_matches"
    __table_args__ = (
        CheckConstraint("matched_quantity > 0", name="ck_pi_receipt_matches_qty_positive"),
        CheckConstraint(
            "matched_unit_cost >= 0", name="ck_pi_receipt_matches_unit_cost_non_negative"
        ),
        Index(
            "ix_pi_receipt_matches_purchase_invoice_line_id",
            "purchase_invoice_line_id",
        ),
        Index("ix_pi_receipt_matches_goods_receipt_item_id", "goods_receipt_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_invoice_line_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoice_lines.id"), nullable=False
    )
    goods_receipt_item_id: Mapped[int] = mapped_column(
        ForeignKey("goods_receipt_items.id"), nullable=False
    )
    matched_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    matched_unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    # invoiced_value_for_slice - (matched_quantity * matched_unit_cost);
    # may be negative (favorable variance). The sum of this column across
    # one invoice's matches is exactly the total_variance
    # post_purchase_invoice_journal posts.
    variance_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    purchase_invoice_line: Mapped[PurchaseInvoiceLine] = relationship(back_populates="matches")


class SupplierPayment(TimestampMixin, Base):
    """A single real settlement event (one cheque, one bank transfer, one
    cash handover) — M7 payment header. `amount` is the total amount
    actually paid; `allocations` (SupplierPaymentAllocation) record which
    invoice(s) it settles and how much of each. Exactly one accounting
    journal is posted per payment regardless of how many invoices it
    allocates across (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 8/9)."""

    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint(
            "payment_method IN ('" + "', '".join(SUPPLIER_PAYMENT_METHODS) + "')",
            name="ck_supplier_payments_method",
        ),
        CheckConstraint("amount > 0", name="ck_supplier_payments_amount_positive"),
        Index("ix_supplier_payments_store_id", "store_id"),
        Index("ix_supplier_payments_supplier_id", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(255))
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    allocations: Mapped[list["SupplierPaymentAllocation"]] = relationship(
        back_populates="supplier_payment"
    )


class SupplierPaymentAllocation(TimestampMixin, Base):
    __tablename__ = "supplier_payment_allocations"
    __table_args__ = (
        UniqueConstraint(
            "supplier_payment_id",
            "purchase_invoice_id",
            name="uq_supplier_payment_allocations_payment_invoice",
        ),
        CheckConstraint("amount > 0", name="ck_supplier_payment_allocations_amount_positive"),
        Index(
            "ix_supplier_payment_allocations_supplier_payment_id",
            "supplier_payment_id",
        ),
        Index(
            "ix_supplier_payment_allocations_purchase_invoice_id",
            "purchase_invoice_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_payment_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_payments.id"), nullable=False
    )
    purchase_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoices.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    supplier_payment: Mapped[SupplierPayment] = relationship(back_populates="allocations")


class SupplierCreditNote(TimestampMixin, Base):
    """An immutable supplier credit note — single-step (no DRAFT state:
    there is no matching decision to make, unlike an invoice) reduction of
    Accounts Payable. `reason` determines the accounting treatment (see
    SUPPLIER_CREDIT_NOTE_REASONS above and
    docs/M7_ADVANCED_AP_SETTLEMENT.md Section 13). `allocations` must sum
    to exactly `grand_total` at creation — there is no unapplied-credit
    balance (design doc Section 14)."""

    __tablename__ = "supplier_credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "credit_number", name="uq_supplier_credit_notes_supplier_credit_number"
        ),
        CheckConstraint(
            "reason IN ('" + "', '".join(SUPPLIER_CREDIT_NOTE_REASONS) + "')",
            name="ck_supplier_credit_notes_reason",
        ),
        CheckConstraint("grand_total > 0", name="ck_supplier_credit_notes_grand_total_positive"),
        CheckConstraint(
            "amount_allocated >= 0 AND amount_allocated <= grand_total",
            name="ck_supplier_credit_notes_amount_allocated_bounds",
        ),
        # GOODS_RETURN must reference the physical return it corresponds
        # to; COMMERCIAL_DISCOUNT must not (nothing physically moved).
        CheckConstraint(
            "(reason = 'GOODS_RETURN' AND purchase_return_id IS NOT NULL) OR "
            "(reason = 'COMMERCIAL_DISCOUNT' AND purchase_return_id IS NULL)",
            name="ck_supplier_credit_notes_reason_reference_consistent",
        ),
        Index("ix_supplier_credit_notes_store_id", "store_id"),
        Index("ix_supplier_credit_notes_supplier_id", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    credit_number: Mapped[str] = mapped_column(String(100), nullable=False)
    credit_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    purchase_return_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_returns.id"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["SupplierCreditNoteLine"]] = relationship(back_populates="credit_note")
    allocations: Mapped[list["SupplierCreditAllocation"]] = relationship(
        back_populates="credit_note"
    )


class SupplierCreditNoteLine(TimestampMixin, Base):
    __tablename__ = "supplier_credit_note_lines"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_supplier_credit_note_lines_amount_positive"),
        CheckConstraint(
            "quantity IS NULL OR quantity > 0", name="ck_supplier_credit_note_lines_qty_positive"
        ),
        Index(
            "ix_supplier_credit_note_lines_supplier_credit_note_id",
            "supplier_credit_note_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_credit_notes.id"), nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    credit_note: Mapped[SupplierCreditNote] = relationship(back_populates="lines")


class SupplierPaymentReversal(TimestampMixin, Base):
    """Records that a SupplierPayment was reversed. See
    docs/M16_DESIGN.md "AP payment/credit-note correction path" for the
    full design; this mirrors app.modules.payroll.models.PayrollReversal
    exactly (same fields, same append-only-row-not-a-status-flip
    rationale) — `SupplierPayment` itself is never mutated on reversal,
    "was this reversed" is a derived fact answered by whether a row here
    references it. `ap.reverse` is the only permission that can create
    one."""

    __tablename__ = "supplier_payment_reversals"
    __table_args__ = (
        UniqueConstraint("supplier_payment_id", name="uq_supplier_payment_reversals_payment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_payment_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_payments.id"), nullable=False
    )
    reversal_journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reversed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SupplierCreditNoteReversal(TimestampMixin, Base):
    """Records that a SupplierCreditNote was reversed. Mirrors
    SupplierPaymentReversal exactly — see that model's docstring."""

    __tablename__ = "supplier_credit_note_reversals"
    __table_args__ = (
        UniqueConstraint(
            "supplier_credit_note_id", name="uq_supplier_credit_note_reversals_credit_note"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_credit_notes.id"), nullable=False
    )
    reversal_journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reversed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SupplierCreditAllocation(TimestampMixin, Base):
    __tablename__ = "supplier_credit_allocations"
    __table_args__ = (
        UniqueConstraint(
            "supplier_credit_note_id",
            "purchase_invoice_id",
            name="uq_supplier_credit_allocations_credit_invoice",
        ),
        CheckConstraint("amount > 0", name="ck_supplier_credit_allocations_amount_positive"),
        Index(
            "ix_supplier_credit_allocations_supplier_credit_note_id",
            "supplier_credit_note_id",
        ),
        Index(
            "ix_supplier_credit_allocations_purchase_invoice_id",
            "purchase_invoice_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_credit_notes.id"), nullable=False
    )
    purchase_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoices.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    credit_note: Mapped[SupplierCreditNote] = relationship(back_populates="allocations")
