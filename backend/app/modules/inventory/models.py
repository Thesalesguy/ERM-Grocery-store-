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

Multi-store transfers (TRANSFER_IN/TRANSFER_OUT) are NOT included in the
movement_type set. The blueprint's assumption #1 scopes v1 to a single
store per product row (products.store_id), so there is nothing to
transfer between yet; adding transfer types now would be unused surface
area. Revisit when multi-store is actually implemented.
"""

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

_INCREASING_MOVEMENT_TYPES = ("PURCHASE_RECEIPT", "SALE_RETURN", "STOCK_ADJUSTMENT_IN")
_DECREASING_MOVEMENT_TYPES = ("SALE", "PURCHASE_RETURN", "STOCK_ADJUSTMENT_OUT")
MOVEMENT_TYPES = _INCREASING_MOVEMENT_TYPES + _DECREASING_MOVEMENT_TYPES

REFERENCE_TYPES = ("purchase_order", "sale", "sale_return", "purchase_return", "stock_adjustment")


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
