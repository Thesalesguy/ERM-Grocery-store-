"""ORM models for sales, sale line items, payments, and sale returns.

See docs/TECHNICAL_BLUEPRINT.md Section C.4 and docs/M1_DATABASE_DESIGN.md.

COGS/historical-reporting design (M1 task Section 7): `SaleItem` freezes
`unit_price_at_sale`, `unit_cost_at_sale`, `discount_amount`, and
`tax_amount`/`tax_rate_id` independently of the product catalog and tax
rate tables, so a sale remains fully reproducible even after later
purchases move the product's Weighted Average Cost or a tax rate changes.

`Sale.grand_total` and `SaleItem.line_total` are stored, not computed on
read (docs/TECHNICAL_BLUEPRINT.md BR-1/BR-5: frozen once at finalization),
but a CHECK constraint ties each to the arithmetic of its own row's other
columns, so the stored value can never silently drift from what it's
supposed to represent.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin

PAYMENT_METHODS = ("CASH", "CARD", "MOBILE_MONEY", "BANK_TRANSFER", "OTHER")


class Sale(TimestampMixin, Base):
    __tablename__ = "sales"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'COMPLETED', 'VOIDED', 'REFUNDED', 'PARTIALLY_REFUNDED')",
            name="ck_sales_status",
        ),
        CheckConstraint("subtotal >= 0", name="ck_sales_subtotal_non_negative"),
        CheckConstraint("discount_total >= 0", name="ck_sales_discount_non_negative"),
        CheckConstraint("tax_total >= 0", name="ck_sales_tax_non_negative"),
        CheckConstraint("grand_total >= 0", name="ck_sales_grand_total_non_negative"),
        # The stored grand_total must always equal the arithmetic of this
        # row's own other stored totals — belt-and-suspenders against a
        # future bug writing an inconsistent value (M1 task Section 19).
        CheckConstraint(
            "grand_total = subtotal - discount_total + tax_total",
            name="ck_sales_grand_total_consistent",
        ),
        Index("ix_sales_store_created", "store_id", "created_at"),
        Index("ix_sales_cashier_id", "cashier_id"),
        # M11 (docs/M11_DESIGN.md Section 11): every sales report filters
        # by store_id and a completed_at range -- distinct from
        # ix_sales_store_created's created_at, which is set on every sale
        # (including still-OPEN ones) and isn't what date-ranged reports
        # query on.
        Index("ix_sales_store_completed", "store_id", "completed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    sale_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Client-generated idempotency key (M2 hardening audit Section 7): the
    # POS generates one UUID per checkout attempt and resends the SAME
    # value on any retry (double-click, network retry after a dropped
    # response). The UNIQUE constraint is the actual enforcement — a
    # second INSERT with the same key is rejected by PostgreSQL even under
    # concurrent submission, not just detected by an application-level
    # SELECT-then-INSERT check (which has its own race). See
    # app.modules.sales.service.finalize_sale.
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    cashier_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")

    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    amount_tendered: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    change_due: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    voided_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["SaleItem"]] = relationship(back_populates="sale")
    payments: Mapped[list["Payment"]] = relationship(back_populates="sale")


class SaleItem(TimestampMixin, Base):
    __tablename__ = "sale_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sale_items_quantity_positive"),
        CheckConstraint("unit_price_at_sale >= 0", name="ck_sale_items_price_non_negative"),
        CheckConstraint("unit_cost_at_sale >= 0", name="ck_sale_items_cost_non_negative"),
        CheckConstraint("discount_amount >= 0", name="ck_sale_items_discount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="ck_sale_items_tax_non_negative"),
        CheckConstraint(
            "line_total = round(quantity * unit_price_at_sale - discount_amount + tax_amount, 2)",
            name="ck_sale_items_line_total_consistent",
        ),
        # M5: how much of this line has been returned so far (docs/
        # M5_RETURNS_VOIDS_REFUNDS.md) — a maintained cache, incremented
        # only by app.modules.sales.service.create_sale_return under a
        # lock on the parent Sale row, mirroring
        # PurchaseOrderItem.quantity_received's established pattern
        # (docs/M3_PURCHASING_RECEIVING_WAC.md). Never user-editable
        # directly; the DB constraint is the backstop against ever
        # returning more than was sold, not the only protection.
        CheckConstraint(
            "quantity_returned >= 0 AND quantity_returned <= quantity",
            name="ck_sale_items_quantity_returned_bounds",
        ),
        Index("ix_sale_items_sale_id", "sale_id"),
        Index("ix_sale_items_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)

    # Frozen at sale time (BR-2) — independent of later catalog/WAC/tax-rate
    # changes, so historical sales stay reproducible.
    unit_price_at_sale: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit_cost_at_sale: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_returned: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)

    sale: Mapped[Sale] = relationship(back_populates="items")


class Payment(TimestampMixin, Base):
    """One or more payments per sale (split tender is supported —
    docs/TECHNICAL_BLUEPRINT.md assumption #7)."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "payment_method IN ('" + "', '".join(PAYMENT_METHODS) + "')",
            name="ck_payments_method",
        ),
        Index("ix_payments_sale_id", "sale_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(255))

    sale: Mapped[Sale] = relationship(back_populates="payments")


class SaleReturn(TimestampMixin, Base):
    """Customer return/refund against a completed sale. The original sale
    is never edited (BR-6) — this is a new, separate record.

    A "void" (docs/M5_RETURNS_VOIDS_REFUNDS.md Section 1) is not a
    separate table or status: every Sale in this system is created
    already COMPLETED with inventory already moved (finalize_sale is
    all-or-nothing), so there is no state a void could bypass that a
    full return wouldn't already have to go through. A void is
    app.modules.sales.service.void_sale calling create_sale_return with
    every line's full remaining quantity — same row, same code path,
    distinguishable only by `reason` and by having returned 100% of the
    sale in one transaction.
    """

    __tablename__ = "sale_returns"
    __table_args__ = (
        CheckConstraint("refund_amount >= 0", name="ck_sale_returns_refund_amount_non_negative"),
        CheckConstraint(
            "refund_method IN ('" + "', '".join(PAYMENT_METHODS) + "')",
            name="ck_sale_returns_refund_method",
        ),
        Index("ix_sale_returns_sale_id", "sale_id"),
        Index("ix_sale_returns_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    return_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # M5 idempotency key (mirrors Sale.client_transaction_id /
    # GoodsReceipt.client_transaction_id exactly — same UNIQUE-constraint
    # enforcement, same early-lookup-then-IntegrityError-recovery pattern
    # in the service layer). Absent from this table since M1 because no
    # service ever wrote to it until M5.
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    refund_method: Mapped[str] = mapped_column(String(20), nullable=False)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    processed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # Manager/admin approval gate for returns above a configurable
    # threshold (docs/TECHNICAL_BLUEPRINT.md assumption #5). Deferred from
    # M5 through M13 (docs/M5_RETURNS_VOIDS_REFUNDS.md "Known
    # limitations") -- every pre-M14 return left this NULL. M14
    # (docs/M14_DESIGN.md) enforces it: this holds the verified approving
    # user's id whenever `approval_required` is true, and stays NULL when
    # it's false (the store had no threshold configured, or this return's
    # amount was below it).
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # M14: frozen at creation time, like every other financial fact on
    # this row (BR-2) -- whether THIS return, at the moment it was
    # created, was subject to the store's approval threshold. Recording
    # this (rather than re-deriving it later from the store's current
    # threshold, which can change) means a later threshold edit can never
    # retroactively make an already-approved return look unapproved, or
    # vice versa.
    approval_required: Mapped[bool] = mapped_column(nullable=False, default=False)

    items: Mapped[list["SaleReturnItem"]] = relationship(back_populates="sale_return")


class SaleReturnItem(TimestampMixin, Base):
    __tablename__ = "sale_return_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sale_return_items_quantity_positive"),
        CheckConstraint("unit_price_refunded >= 0", name="ck_sale_return_items_price_non_negative"),
        CheckConstraint(
            "discount_refunded >= 0", name="ck_sale_return_items_discount_non_negative"
        ),
        CheckConstraint("tax_refunded >= 0", name="ck_sale_return_items_tax_non_negative"),
        CheckConstraint("unit_cost_refunded >= 0", name="ck_sale_return_items_cost_non_negative"),
        Index("ix_sale_return_items_sale_return_id", "sale_return_id"),
        Index("ix_sale_return_items_sale_item_id", "sale_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_return_id: Mapped[int] = mapped_column(ForeignKey("sale_returns.id"), nullable=False)
    sale_item_id: Mapped[int] = mapped_column(ForeignKey("sale_items.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price_refunded: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # M5: the proportional share of the original line's discount_amount/
    # tax_amount this specific return line reverses, and the frozen COGS
    # basis (copied from SaleItem.unit_cost_at_sale, NEVER current WAC —
    # docs/M5_RETURNS_VOIDS_REFUNDS.md "COGS reversal") used for its
    # inventory/accounting effect. Stored (not recomputed on read) so the
    # return record itself is as immutable/reproducible as the sale it
    # reverses.
    discount_refunded: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_refunded: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    unit_cost_refunded: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    # Damaged returns may not go back to sellable stock — only restock=true
    # returns generate a SALE_RETURN inventory movement and a COGS/
    # Inventory accounting pair (docs/M5_RETURNS_VOIDS_REFUNDS.md
    # "Inventory behavior").
    restock: Mapped[bool] = mapped_column(nullable=False, default=True)

    sale_return: Mapped[SaleReturn] = relationship(back_populates="items")
