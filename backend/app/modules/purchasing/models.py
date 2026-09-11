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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin


class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    tax_id: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


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
    # The cost expected at order time — frozen (BR-2). The cost actually
    # used for WAC comes from GoodsReceiptItem.unit_cost, which may differ.
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="items")


class GoodsReceipt(TimestampMixin, Base):
    """One physical receiving event against a purchase order. A purchase
    order can have several (partial deliveries)."""

    __tablename__ = "goods_receipts"
    __table_args__ = (Index("ix_goods_receipts_purchase_order_id", "purchase_order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
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
    """Goods sent back to a supplier (damaged/wrong item)."""

    __tablename__ = "purchase_returns"
    __table_args__ = (
        Index("ix_purchase_returns_purchase_order_id", "purchase_order_id"),
        Index("ix_purchase_returns_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
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
