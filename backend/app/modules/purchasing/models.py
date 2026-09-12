"""ORM models for suppliers, purchase orders, goods receiving, and purchase
returns.

See docs/TECHNICAL_BLUEPRINT.md Sections C.2/C.3 and docs/M1_DATABASE_DESIGN.md.

Naming note: the blueprint originally called the order header `purchases`/
`purchase_items`; the M1 task instructions asked for `purchase_orders`/
`purchase_order_items` to make the order/receipt distinction unambiguous
in the schema itself. This module uses the M1 task's naming — the
blueprint has been updated to match (Section C.3).

Key invariant: creating a PurchaseOrder or PurchaseOrderItem never touches
inventory. Only a GoodsReceiptItem (via app.modules.purchasing.service)
creates an inventory_movements row and recomputes Weighted Average Cost.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin


class Supplier(TimestampMixin, Base):
    """Shared/global reference data (M3 decision, documented rather than
    silently assumed): a supplier is a business-wide vendor relationship,
    not one store's data — a chain's multiple stores buy from the same
    supplier, and the blueprint's "Supplier totals" report already
    assumes a global join. This matches the existing precedent of
    `ProductCategory`/`TaxRate` (shared reference data), as opposed to
    `Product`/`Sale`/`PurchaseOrder` (store-scoped operational data).
    RBAC for suppliers reuses `purchasing.read`/`purchasing.write` rather
    than a store filter — see docs/M3_PURCHASING_RECEIVING_WAC.md.

    No `created_by`/`updated_by` columns, matching `ProductCategory`'s
    precedent (reference/master data doesn't track an acting user as a
    column here; "who changed this supplier" is answered by the audit
    log, same as product catalog changes since the M2 hardening fix) —
    unlike transactional/event tables (PurchaseOrder, GoodsReceipt,
    StockAdjustment), which do.
    """

    __tablename__ = "suppliers"
    __table_args__ = (
        # Optional short reference code, unique when actually set (NULLs
        # are never compared equal in a plain UNIQUE constraint, so this
        # partial index is what actually enforces "unique if present").
        Index(
            "uq_suppliers_code",
            "code",
            unique=True,
            postgresql_where=text("code IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    tax_id: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    # M6 (docs/M6_AP_VENDOR_ACCOUNTING.md "Supplier master"): the only new
    # field AP actually needs on Supplier — contact info, tax_id, and
    # active status already existed and cover the rest of the task's
    # checklist. NULL means "no standing term" (due immediately/on
    # receipt); a purchase invoice may still override its own due_date
    # explicitly. Deliberately NOT adding default currency (this system
    # has no multi-currency support anywhere — see PurchaseInvoice's own
    # currency-assumption note) or a payment account/reference (would be
    # either unused metadata or the exact "banking credentials" the task
    # says not to store).
    default_payment_terms_days: Mapped[int | None] = mapped_column()


class PurchaseOrder(TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("purchase_number", name="uq_purchase_orders_number"),
        CheckConstraint(
            "status IN ('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED')",
            name="ck_purchase_orders_status",
        ),
        Index("ix_purchase_orders_store_id", "store_id"),
        Index("ix_purchase_orders_supplier_id", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    purchase_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # M9 (docs/M9_SUPPLY_CHAIN_DESIGN.md "Design Decision 9"): set only
    # when this PO was generated by executing a replenishment plan — NULL
    # for every manually-created PO (unchanged from M3/M6). The only
    # column distinguishing a generated PO from a hand-entered one; every
    # other column and lifecycle transition is identical.
    replenishment_plan_id: Mapped[int | None] = mapped_column(ForeignKey("replenishment_plans.id"))

    items: Mapped[list["PurchaseOrderItem"]] = relationship(back_populates="purchase_order")
    goods_receipts: Mapped[list["GoodsReceipt"]] = relationship(back_populates="purchase_order")


class PurchaseOrderItem(TimestampMixin, Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        CheckConstraint(
            "quantity_ordered > 0", name="ck_purchase_order_items_qty_ordered_positive"
        ),
        CheckConstraint(
            "quantity_received >= 0", name="ck_purchase_order_items_qty_received_non_negative"
        ),
        # M6: the three-way-match ceiling is RECEIVED, not ORDERED — you owe
        # a supplier for what physically arrived, even if that's more than
        # you ordered (over-receipt is allowed above); you can never owe
        # for more than arrived (docs/M6_AP_VENDOR_ACCOUNTING.md "Three-way
        # matching policy"). Enforced here as a same-row DB CHECK, not only
        # in application code — the exact belt-and-suspenders pattern M5
        # used for quantity_returned <= quantity.
        CheckConstraint(
            "quantity_invoiced >= 0 AND quantity_invoiced <= quantity_received",
            name="ck_purchase_order_items_qty_invoiced_bounds",
        ),
        CheckConstraint("unit_cost >= 0", name="ck_purchase_order_items_unit_cost_non_negative"),
        Index("ix_purchase_order_items_purchase_order_id", "purchase_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    # Over-receipt is intentionally allowed (docs/TECHNICAL_BLUEPRINT.md
    # Section F: "over-receipt allowed, flagged"), so there is deliberately
    # no CHECK tying quantity_received <= quantity_ordered.
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    # M6: a maintained running-total cache column, exact same pattern as
    # quantity_received itself (and M5's SaleItem.quantity_returned) —
    # incremented only by app.modules.ap.service.post_purchase_invoice
    # under a lock on the parent PurchaseOrder row (the SAME lock
    # receive_goods already takes), so a receipt and an invoice-posting
    # racing against the same PO's items always serialize correctly.
    quantity_invoiced: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    # The cost expected at order time — frozen (BR-2). The cost actually
    # used for WAC comes from GoodsReceiptItem.unit_cost, which may differ.
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="items")


class GoodsReceipt(TimestampMixin, Base):
    """One physical receiving event against a purchase order. A purchase
    order can have several (partial deliveries).

    `store_id` is redundant with `purchase_order.store_id` (same pattern
    already used by `PurchaseReturn` below) — kept as its own column so
    store-scoped queries/authorization checks (M2 hardening audit Section
    12; M3 extends the same rule to purchasing) don't require a join.

    `client_transaction_id` is the idempotency key (M2 hardening audit
    Section 7's pattern, extended to M3): a goods receipt changes
    inventory and cost, so it must be as safe against a retried/duplicated
    request as a sale is. See app.modules.purchasing.service.receive_goods.
    """

    __tablename__ = "goods_receipts"
    __table_args__ = (
        Index("ix_goods_receipts_purchase_order_id", "purchase_order_id"),
        Index("ix_goods_receipts_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="goods_receipts")
    items: Mapped[list["GoodsReceiptItem"]] = relationship(back_populates="goods_receipt")


class GoodsReceiptItem(TimestampMixin, Base):
    __tablename__ = "goods_receipt_items"
    __table_args__ = (
        CheckConstraint("quantity_received > 0", name="ck_goods_receipt_items_qty_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_goods_receipt_items_unit_cost_non_negative"),
        Index("ix_goods_receipt_items_goods_receipt_id", "goods_receipt_id"),
        Index("ix_goods_receipt_items_purchase_order_item_id", "purchase_order_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    goods_receipt_id: Mapped[int] = mapped_column(ForeignKey("goods_receipts.id"), nullable=False)
    purchase_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order_items.id"), nullable=False
    )
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # The actual invoiced cost for this receipt — this, not the PO's
    # estimated unit_cost, is what feeds the Weighted Average Cost
    # recompute (docs/TECHNICAL_BLUEPRINT.md Section F).
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    condition_notes: Mapped[str | None] = mapped_column(String(500))

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="items")


class PurchaseReturn(TimestampMixin, Base):
    """Goods sent back to a supplier (damaged/wrong item).

    Cost basis limitation (M3, documented rather than silently assumed):
    there is no per-receipt-lot tracking in this schema (the same
    limitation the blueprint already accepts for WAC purchase-returns,
    Section D edge case 6), so a return's `unit_cost` is the product's
    *current* Weighted Average Cost at return time, not necessarily the
    exact cost of the specific lot being physically sent back. This
    mirrors how record_movement() already treats every other removal
    (SALE, STOCK_ADJUSTMENT_OUT): removing stock never changes WAC, only
    receiving does — a return is architecturally identical to those, not
    a special case.
    """

    __tablename__ = "purchase_returns"
    __table_args__ = (
        Index("ix_purchase_returns_purchase_order_id", "purchase_order_id"),
        Index("ix_purchase_returns_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    items: Mapped[list["PurchaseReturnItem"]] = relationship(back_populates="purchase_return")


class PurchaseReturnItem(TimestampMixin, Base):
    __tablename__ = "purchase_return_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_return_items_qty_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_purchase_return_items_unit_cost_non_negative"),
        Index("ix_purchase_return_items_purchase_return_id", "purchase_return_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_return_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_returns.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # The cost the returned stock was received at (the specific
    # goods_receipt_item lot being reversed) — required to correctly back
    # this quantity out of WAC (docs/TECHNICAL_BLUEPRINT.md Section D,
    # edge case 6).
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)

    purchase_return: Mapped[PurchaseReturn] = relationship(back_populates="items")
