"""ORM models for the M9 supply-chain layer: the supplier-product catalog/
pricing table, and the replenishment plan (recommendation → approval →
execution) itself.

See docs/M9_SUPPLY_CHAIN_DESIGN.md for the full design — in particular
"Design Decision 2" (why `SupplierProduct` is both catalog and
effective-dated price history in one table) and "Design Decision 4/5/9"
(the plan lifecycle, sibling plans for multi-source splitting, and why a
generated PO/transfer is linked back but otherwise indistinguishable from
a manually created one).
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

REPLENISHMENT_PLAN_STATUSES = ("RECOMMENDED", "APPROVED", "EXECUTED", "STALE", "CANCELLED")
_CANCELLABLE_PLAN_STATUSES = ("RECOMMENDED", "APPROVED", "STALE")
SOURCE_TYPES = ("SUPPLIER", "TRANSFER")
URGENCY_LEVELS = ("NORMAL", "URGENT")


class SupplierProduct(TimestampMixin, Base):
    """One row per (supplier, product, effective_date) — see Design
    Decision 2. Never mutated in place to change a price: a price change
    is a new row with a later effective_date. The "current" price/terms
    for a (supplier, product) pair is deterministically the `is_active`
    row with the greatest `effective_date` not after today (see
    app.modules.replenishment.service.get_current_supplier_product)."""

    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "product_id", "effective_date", name="uq_supplier_products_effective"
        ),
        CheckConstraint("pack_size > 0", name="ck_supplier_products_pack_size_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_supplier_products_unit_cost_non_negative"),
        CheckConstraint(
            "minimum_order_quantity IS NULL OR minimum_order_quantity > 0",
            name="ck_supplier_products_moq_positive",
        ),
        CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_supplier_products_lead_time_non_negative",
        ),
        Index("ix_supplier_products_supplier_id", "supplier_id"),
        Index("ix_supplier_products_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    supplier_sku: Mapped[str | None] = mapped_column(String(64))
    pack_size: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=1)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    minimum_order_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    lead_time_days: Mapped[int | None] = mapped_column()
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class ReplenishmentPlan(TimestampMixin, Base):
    """A single, specific, persisted replenishment recommendation — see
    docs/M9_SUPPLY_CHAIN_DESIGN.md design answer #1. `created_at`
    (TimestampMixin) IS the "generated timestamp" the task asks for; no
    separate column duplicates it.

    Exactly one source is ever set, matching `source_type`
    (`ck_replenishment_plans_source_matches_type` below) — a shortfall
    needing multiple sources is represented as multiple SIBLING plan rows
    sharing `generation_batch_id`, never one plan with several sources
    (Design Decision 5)."""

    __tablename__ = "replenishment_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(REPLENISHMENT_PLAN_STATUSES) + "')",
            name="ck_replenishment_plans_status",
        ),
        CheckConstraint(
            "source_type IN ('" + "', '".join(SOURCE_TYPES) + "')",
            name="ck_replenishment_plans_source_type",
        ),
        CheckConstraint(
            "urgency IN ('" + "', '".join(URGENCY_LEVELS) + "')",
            name="ck_replenishment_plans_urgency",
        ),
        CheckConstraint("needed_quantity > 0", name="ck_replenishment_plans_needed_positive"),
        CheckConstraint("suggested_quantity > 0", name="ck_replenishment_plans_suggested_positive"),
        CheckConstraint(
            "executed_quantity IS NULL OR executed_quantity > 0",
            name="ck_replenishment_plans_executed_positive",
        ),
        # Exactly one source reference set, matching source_type — the
        # structural guarantee behind "a plan always has exactly one
        # source" (Design Decision 5), enforced in the database, not only
        # in application code.
        CheckConstraint(
            "(source_type = 'SUPPLIER' AND supplier_id IS NOT NULL AND source_store_id IS NULL) "
            "OR (source_type = 'TRANSFER' AND source_store_id IS NOT NULL AND supplier_id IS NULL)",
            name="ck_replenishment_plans_source_matches_type",
        ),
        CheckConstraint(
            "source_store_id IS NULL OR source_store_id <> destination_store_id",
            name="ck_replenishment_plans_source_distinct_from_destination",
        ),
        # A structural guarantee that "plan says EXECUTED" and "a real
        # PO/transfer exists" can never disagree.
        CheckConstraint(
            "(status = 'EXECUTED' AND source_type = 'SUPPLIER' "
            " AND generated_purchase_order_id IS NOT NULL AND generated_transfer_id IS NULL) "
            "OR (status = 'EXECUTED' AND source_type = 'TRANSFER' "
            " AND generated_transfer_id IS NOT NULL AND generated_purchase_order_id IS NULL) "
            "OR (status <> 'EXECUTED' "
            " AND generated_purchase_order_id IS NULL AND generated_transfer_id IS NULL)",
            name="ck_replenishment_plans_execution_link_matches_status",
        ),
        # A generated PO/transfer can be linked back from at most one
        # plan — the DB-level half of duplicate-execution prevention
        # (design answer #16(c)); partial indexes because most rows have
        # NULL here and NULLs are never compared equal in a plain UNIQUE
        # constraint (same pattern as Supplier.code elsewhere).
        Index(
            "uq_replenishment_plans_generated_po",
            "generated_purchase_order_id",
            unique=True,
            postgresql_where=text("generated_purchase_order_id IS NOT NULL"),
        ),
        Index(
            "uq_replenishment_plans_generated_transfer",
            "generated_transfer_id",
            unique=True,
            postgresql_where=text("generated_transfer_id IS NOT NULL"),
        ),
        Index("ix_replenishment_plans_destination_store_id", "destination_store_id"),
        Index("ix_replenishment_plans_product_id", "product_id"),
        Index("ix_replenishment_plans_status", "status"),
        Index("ix_replenishment_plans_generation_batch_id", "generation_batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    generation_batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    destination_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    needed_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    suggested_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    source_store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    # Informational snapshot at generation time only — see design answer
    # #18: execution always re-reads the CURRENT price/position fresh,
    # never blindly trusts this column.
    suggested_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    urgency: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECOMMENDED")

    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    execution_client_transaction_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    executed_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    generated_purchase_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id")
    )
    generated_transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("inter_store_transfers.id")
    )

    stale_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_reason: Mapped[str | None] = mapped_column(Text)

    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
