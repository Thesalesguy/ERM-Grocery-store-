"""ORM models for the inventory-movement ledger and manual stock
adjustments.

See docs/TECHNICAL_BLUEPRINT.md Section C.2 and docs/M1_DATABASE_DESIGN.md.

`InventoryMovement` is append-only by design: BR-6 forbids editing or
deleting historical financial/inventory records, and this table is the
system of record for stock quantity and cost. The database-level
enforcement of that (REVOKE UPDATE/DELETE from the application's runtime
role) lives in the M1 privilege-model migration, not in this model file —
see docs/M1_DATABASE_DESIGN.md Section "Audit-log & ledger write
protection".

Quantity representation: a single signed `quantity_delta` column (positive
increases stock, negative decreases it) rather than a separate
magnitude+direction pair. `movement_type` and the sign of `quantity_delta`
must agree — enforced by ck_inventory_movements_direction below — but
there is deliberately no blanket "quantity must be positive" constraint,
since direction is the whole point of the sign.

M8 (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 6/7") adds
TRANSFER_IN/TRANSFER_OUT — exactly the movement types this docstring
anticipated, now that inter-store transfers exist. `Product` remains
store-scoped (unchanged): a transfer moves value between two DISTINCT
product rows (the source store's row and the destination store's own,
pre-existing row for the same SKU), so a `TRANSFER_OUT` movement against
the source product and a `TRANSFER_IN` movement against the destination
product are two separate ledger rows, exactly like a sale and a receipt
are two separate rows today — never one row that somehow spans stores.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
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

_INCREASING_MOVEMENT_TYPES = (
    "PURCHASE_RECEIPT",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT_IN",
    "TRANSFER_IN",
)
_DECREASING_MOVEMENT_TYPES = (
    "SALE",
    "PURCHASE_RETURN",
    "STOCK_ADJUSTMENT_OUT",
    "TRANSFER_OUT",
)
MOVEMENT_TYPES = _INCREASING_MOVEMENT_TYPES + _DECREASING_MOVEMENT_TYPES

REFERENCE_TYPES = (
    "purchase_order",
    "sale",
    "sale_return",
    "purchase_return",
    "stock_adjustment",
    "inter_store_transfer",
)


class InventoryMovement(TimestampMixin, Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="ck_inventory_movements_delta_nonzero"),
        CheckConstraint(
            "unit_cost_at_movement >= 0", name="ck_inventory_movements_unit_cost_non_negative"
        ),
        CheckConstraint(
            "movement_type IN ('" + "', '".join(MOVEMENT_TYPES) + "')",
            name="ck_inventory_movements_type",
        ),
        CheckConstraint(
            "reference_type IN ('" + "', '".join(REFERENCE_TYPES) + "')",
            name="ck_inventory_movements_reference_type",
        ),
        CheckConstraint(
            "(movement_type IN ('" + "', '".join(_INCREASING_MOVEMENT_TYPES) + "') "
            "AND quantity_delta > 0) OR "
            "(movement_type IN ('" + "', '".join(_DECREASING_MOVEMENT_TYPES) + "') "
            "AND quantity_delta < 0)",
            name="ck_inventory_movements_direction",
        ),
        Index("ix_inventory_movements_product_created", "product_id", "created_at"),
        Index("ix_inventory_movements_reference", "reference_type", "reference_id"),
        Index("ix_inventory_movements_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Weighted Average Cost frozen at the moment of this movement. For a
    # SALE this is the COGS basis; for a PURCHASE_RECEIPT it is the newly
    # received unit cost (the input to the WAC recompute, not its output).
    unit_cost_at_movement: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    # Snapshot of on-hand quantity immediately after this movement was
    # applied — redundant with SUM() over the ledger, kept for fast
    # point-in-time audit/debug reads without re-aggregating history.
    resulting_quantity_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Polymorphic pointer (not an enforced FK: it points at a different
    # table depending on reference_type). This is an intentional,
    # documented denormalization — see docs/M1_DATABASE_DESIGN.md.
    reference_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reference_id: Mapped[int | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class StockAdjustment(TimestampMixin, Base):
    """The human-facing record behind a manual correction (shrinkage,
    damage, stocktake). Creating one produces exactly one
    InventoryMovement row (STOCK_ADJUSTMENT_IN or _OUT depending on sign).
    """

    __tablename__ = "stock_adjustments"
    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="ck_stock_adjustments_delta_nonzero"),
        CheckConstraint(
            "reason_code IN ('DAMAGE', 'THEFT', 'EXPIRY', 'STOCKTAKE_CORRECTION', 'OTHER')",
            name="ck_stock_adjustments_reason_code",
        ),
        Index("ix_stock_adjustments_store_id", "store_id"),
        Index("ix_stock_adjustments_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # M8 (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 1"): set
    # only for a StockAdjustment created by post_stock_count — traces a
    # STOCKTAKE_CORRECTION adjustment back to the count that produced it.
    # NULL for every other reason_code and for pre-M8 rows.
    stock_count_id: Mapped[int | None] = mapped_column(ForeignKey("stock_counts.id"))


# --- Stock counts / physical inventory (M8) ---------------------------------

STOCK_COUNT_STATUSES = ("DRAFT", "OPEN", "COUNTED", "REVIEWED", "POSTED", "CANCELLED")
# Statuses from which a count may still be cancelled — never POSTED (the
# whole point of posting is to be the point of no return, same rule every
# other financial document in this codebase follows).
_CANCELLABLE_STOCK_COUNT_STATUSES = ("DRAFT", "OPEN", "COUNTED", "REVIEWED")
# Statuses in which a count entry (or recount) may still be recorded —
# see docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 3".
_COUNTABLE_STOCK_COUNT_STATUSES = ("OPEN", "COUNTED")


class StockCount(TimestampMixin, Base):
    """A physical inventory count scoped to exactly one store. See
    docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 2" for the full
    lifecycle and "Design Decision 5" for why posting recomputes variance
    against CURRENT on-hand quantity rather than blindly trusting the
    `expected_quantity` snapshot on each line."""

    __tablename__ = "stock_counts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(STOCK_COUNT_STATUSES) + "')",
            name="ck_stock_counts_status",
        ),
        Index("ix_stock_counts_store_id", "store_id"),
        Index("ix_stock_counts_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    count_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    # Optional scoping convenience captured at creation — expands into
    # concrete StockCountLine rows at DRAFT time and is never
    # re-evaluated later (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design
    # Decision 1": a product added to the category after the count
    # started is never silently swept in).
    category_id: Mapped[int | None] = mapped_column(ForeignKey("product_categories.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    opened_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["StockCountLine"]] = relationship(back_populates="stock_count")


class StockCountLine(TimestampMixin, Base):
    """One product's expected/counted quantity within a StockCount.
    Unique per (stock_count_id, product_id) — there is no separate
    "recount" row type; a recount overwrites this same row's
    counted_quantity/counted_by/counted_at under a row lock, with the
    prior value preserved in the audit log (Design Decision 3)."""

    __tablename__ = "stock_count_lines"
    __table_args__ = (
        UniqueConstraint("stock_count_id", "product_id", name="uq_stock_count_lines_count_product"),
        CheckConstraint(
            "expected_quantity IS NULL OR expected_quantity >= 0",
            name="ck_stock_count_lines_expected_non_negative",
        ),
        CheckConstraint(
            "counted_quantity IS NULL OR counted_quantity >= 0",
            name="ck_stock_count_lines_counted_non_negative",
        ),
        Index("ix_stock_count_lines_stock_count_id", "stock_count_id"),
        Index("ix_stock_count_lines_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_count_id: Mapped[int] = mapped_column(ForeignKey("stock_counts.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    # NULL at DRAFT (scope is fixed by this row's mere existence, but the
    # quantity snapshot is not taken yet — Design Decision 2). Captured
    # exactly once at DRAFT -> OPEN, or re-snapshotted during a
    # drift-triggered recount (Design Decision 3) — never recomputed
    # silently at posting.
    expected_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    expected_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # NULL means "not yet counted" — deliberately distinct from 0, which
    # means "counted and found to be zero" (Design Decision 4).
    counted_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    counted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many times this line has been (re)counted — incremented on
    # every count-entry call, including the first. Purely informational
    # (the audit log is the real history); lets a reviewer see "this line
    # was recounted" at a glance without reading audit_logs.
    recount_number: Mapped[int] = mapped_column(nullable=False, default=0)

    stock_count: Mapped[StockCount] = relationship(back_populates="lines")
