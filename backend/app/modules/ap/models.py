"""ORM models for the Accounts Payable module: supplier invoices, their
lines, and supplier payments.

See docs/M6_AP_VENDOR_ACCOUNTING.md for the full design. Summary of the
load-bearing decisions:

- `PurchaseInvoice.invoice_number` is the SUPPLIER's own document number
  (operator-entered, never system-generated) — a business cannot invent
  its supplier's invoice numbering. Unique per-supplier
  (`uq_purchase_invoices_supplier_invoice_number`), NOT globally: two
  different suppliers routinely reuse the same invoice-number sequence.
  `client_transaction_id` is the SEPARATE idempotency key protecting
  against a retried *create* request (M2/M3/M5 pattern); the
  per-supplier uniqueness protects against genuinely re-entering the
  same real-world document later under a fresh key — both matter, for
  different failure modes.
- One invoice always references exactly one `purchase_order_id` (an
  invoice spanning multiple purchase orders is deferred — see the design
  doc's "known limitations"). This still supports every matching scenario
  the M6 task requires: one invoice against multiple receipts (its lines
  reference `purchase_order_item_id`s that may span several
  `GoodsReceipt`s) and multiple invoices against one PO (no exclusivity
  constraint prevents a second `PurchaseInvoice` against the same PO).
- Posted invoices are financially immutable *by application discipline*,
  not by revoking DB privileges the way `journal_entries`/`journal_lines`
  are — `amount_paid`/`status` legitimately still change after posting as
  payments arrive, the same way `Sale.status`/`PurchaseOrder.status`
  remain ordinary mutable columns after their own point of no return.
  `app.modules.ap.service` is the only code path allowed to touch these
  fields; no API endpoint exposes a raw field-level edit once DRAFT.
- No `currency` column: this system has no multi-currency support
  anywhere (Sale, JournalLine, PurchaseOrder all assume one implicit
  system currency) — adding a currency field with no FX-rate handling
  behind it would misstate accounting rather than genuinely support
  multi-currency, so it is deliberately omitted and documented as
  deferred, not silently assumed.
- `PurchaseInvoiceLine.line_total`/`PurchaseInvoice.grand_total` follow
  the exact same "stored, DB-CHECK-verified arithmetic" pattern as
  `SaleItem.line_total`/`Sale.grand_total` (M1 BR-2/BR-19) — never
  computed ad hoc by a report.
- `SupplierPayment` is applied to exactly one `PurchaseInvoice` — a
  payment split across multiple invoices, or an unapplied supplier
  credit/advance, is deferred (see the design doc). Overpayment beyond
  the invoice's own remaining balance is rejected outright at the
  service layer and backstopped by `PurchaseInvoice.amount_paid <=
  grand_total` here.
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
# and only while amount_paid == 0 — see that function's docstring.
_POSTED_INVOICE_STATUSES = ("POSTED", "PARTIALLY_PAID", "PAID")

# Deliberately NOT app.modules.sales.models.PAYMENT_METHODS reused — CARD
# and MOBILE_MONEY describe how a *customer* paid *this* business, not how
# this business settles a *supplier*; CHEQUE (a very ordinary B2B vendor
# settlement instrument) is added instead. See
# accounting/constants.py SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE.
SUPPLIER_PAYMENT_METHODS = ("CASH", "BANK_TRANSFER", "CHEQUE", "OTHER")


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
        CheckConstraint(
            "amount_paid >= 0 AND amount_paid <= grand_total",
            name="ck_purchase_invoices_amount_paid_bounds",
        ),
        Index("ix_purchase_invoices_store_id", "store_id"),
        Index("ix_purchase_invoices_supplier_id", "supplier_id"),
        Index("ix_purchase_invoices_purchase_order_id", "purchase_order_id"),
        Index("ix_purchase_invoices_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # A maintained cache, updated only by record_supplier_payment/
    # void_purchase_invoice under a lock on this row — balance_due is
    # deliberately NOT a stored column (grand_total - amount_paid,
    # computed at read time) to avoid a second source of truth.
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["PurchaseInvoiceLine"]] = relationship(back_populates="purchase_invoice")
    payments: Mapped[list["SupplierPayment"]] = relationship(back_populates="purchase_invoice")


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


class SupplierPayment(TimestampMixin, Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint(
            "payment_method IN ('" + "', '".join(SUPPLIER_PAYMENT_METHODS) + "')",
            name="ck_supplier_payments_method",
        ),
        CheckConstraint("amount > 0", name="ck_supplier_payments_amount_positive"),
        Index("ix_supplier_payments_store_id", "store_id"),
        Index("ix_supplier_payments_supplier_id", "supplier_id"),
        Index("ix_supplier_payments_purchase_invoice_id", "purchase_invoice_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    purchase_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoices.id"), nullable=False
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(255))
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    purchase_invoice: Mapped[PurchaseInvoice] = relationship(back_populates="payments")
