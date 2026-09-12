"""ORM models for inter-store inventory transfers.

See docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decisions 6-8" for the
full design. Summary of the load-bearing decisions:

- `Product` stays store-scoped (unchanged from M0-M7): a transfer line
  references TWO distinct product rows — `source_product_id` (in
  `from_store_id`) and `destination_product_id` (in `to_store_id`, must
  already exist — never auto-created).
- No separate REQUESTED/APPROVED states: DRAFT -> SHIPPED -> RECEIVED
  (RECEIVED is a derived fact, not a stored status — see below).
- Shipping is a single event per transfer (`InterStoreTransfer` itself
  carries the ship-time fields); receiving may happen across several
  events (`InterStoreTransferReceipt`, mirroring `GoodsReceipt` exactly).
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

# RECEIVED is deliberately not a stored status — see the module docstring
# and docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 7": "fully
# received" is the derived fact that every line's received_quantity has
# reached its shipped_quantity, computed at read time, never stored
# redundantly (the same reasoning PurchaseInvoice.balance_due follows).
TRANSFER_STATUSES = ("DRAFT", "SHIPPED", "CANCELLED")
_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)


class InterStoreTransfer(TimestampMixin, Base):
    __tablename__ = "inter_store_transfers"
    __table_args__ = (
        UniqueConstraint("transfer_number", name="uq_inter_store_transfers_number"),
        CheckConstraint(
            "status IN ('" + "', '".join(TRANSFER_STATUSES) + "')",
            name="ck_inter_store_transfers_status",
        ),
        CheckConstraint(
            "from_store_id <> to_store_id", name="ck_inter_store_transfers_distinct_stores"
        ),
        Index("ix_inter_store_transfers_from_store_id", "from_store_id"),
        Index("ix_inter_store_transfers_to_store_id", "to_store_id"),
        Index("ix_inter_store_transfers_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    from_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    to_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    transfer_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    requested_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # Ship-time fields — a transfer ships exactly once (Design Decision 8).
    # `ship_client_transaction_id` is the idempotency key for that one
    # event, unique so a retried request is safely recoverable exactly
    # like every other mutating endpoint in this codebase.
    ship_client_transaction_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    shipped_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["InterStoreTransferLine"]] = relationship(back_populates="transfer")
    receipts: Mapped[list["InterStoreTransferReceipt"]] = relationship(back_populates="transfer")


class InterStoreTransferLine(TimestampMixin, Base):
    __tablename__ = "inter_store_transfer_lines"
    __table_args__ = (
        CheckConstraint(
            "requested_quantity > 0", name="ck_inter_store_transfer_lines_requested_positive"
        ),
        CheckConstraint(
            "shipped_quantity >= 0 AND shipped_quantity <= requested_quantity",
            name="ck_inter_store_transfer_lines_shipped_bounds",
        ),
        CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= shipped_quantity",
            name="ck_inter_store_transfer_lines_received_bounds",
        ),
        Index("ix_inter_store_transfer_lines_transfer_id", "transfer_id"),
        Index("ix_inter_store_transfer_lines_source_product_id", "source_product_id"),
        Index("ix_inter_store_transfer_lines_destination_product_id", "destination_product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("inter_store_transfers.id"), nullable=False)
    source_product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    destination_product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    shipped_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    received_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    # Frozen at ship time from the SOURCE product's current_cost — never
    # recomputed from the destination's WAC (Design Decision 4/9). NULL
    # until shipped.
    unit_cost_at_shipment: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))

    transfer: Mapped[InterStoreTransfer] = relationship(back_populates="lines")


class InterStoreTransferReceipt(TimestampMixin, Base):
    """One physical receiving event against a transfer — a transfer may
    have several, mirroring GoodsReceipt/GoodsReceiptItem exactly (Design
    Decision 8)."""

    __tablename__ = "inter_store_transfer_receipts"
    __table_args__ = (Index("ix_inter_store_transfer_receipts_transfer_id", "transfer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("inter_store_transfers.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    transfer: Mapped[InterStoreTransfer] = relationship(back_populates="receipts")
    items: Mapped[list["InterStoreTransferReceiptItem"]] = relationship(back_populates="receipt")


class InterStoreTransferReceiptItem(TimestampMixin, Base):
    __tablename__ = "inter_store_transfer_receipt_items"
    __table_args__ = (
        CheckConstraint(
            "quantity_received > 0", name="ck_inter_store_transfer_receipt_items_qty_positive"
        ),
        Index(
            "ix_inter_store_transfer_receipt_items_receipt_id",
            "inter_store_transfer_receipt_id",
        ),
        Index("ix_inter_store_transfer_receipt_items_line_id", "inter_store_transfer_line_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inter_store_transfer_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("inter_store_transfer_receipts.id"), nullable=False
    )
    inter_store_transfer_line_id: Mapped[int] = mapped_column(
        ForeignKey("inter_store_transfer_lines.id"), nullable=False
    )
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)

    receipt: Mapped[InterStoreTransferReceipt] = relationship(back_populates="items")
