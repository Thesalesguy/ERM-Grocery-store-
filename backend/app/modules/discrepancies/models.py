"""ORM model for the generalized receiving-discrepancy investigation ledger.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/12 for the full design.
Summary of the load-bearing decisions:

- `ReceivingDiscrepancy` is intentionally generalized (`source_type` +
  `source_id`, the same polymorphic-reference pattern
  `JournalEntry.source_type`/`source_id` already uses everywhere else in
  this codebase) so a future milestone can wire supplier goods-receiving
  discrepancies (`source_type = "GOODS_RECEIPT"`) through the identical
  table/state-machine without a schema change. M25 Phase 3 itself only
  ever inserts `source_type = "TRANSFER_RECEIPT"` rows, since the
  existing `GoodsReceiptItem` model has no damaged/short capture at all
  yet (see app.modules.transfers.service's wiring and the Phase 3 final
  report for why extending purchasing receiving itself is out of this
  phase's scope).
- `source_id` references `InterStoreTransferReceiptItem.id` for a
  `TRANSFER_RECEIPT`-sourced row — the single physical receiving event
  for one transfer line, which is exactly the granularity a custody
  acceptance already uses (Phase 2). One receipt item produces at most
  one discrepancy row: enforced by the `UniqueConstraint` below, which is
  this ledger's idempotency backstop (Section 17 of the contract) in
  addition to `receive_transfer`'s own idempotency (the actual creation
  call site never runs twice for the same client request).
- `quantity_short`/`quantity_damaged`/`unit_cost` are copied onto this
  row at creation time rather than re-derived by joining back to the
  receipt item on every read — they are frozen facts about the
  discrepancy exactly like `unit_cost_at_shipment` is frozen on the
  transfer line, and this is what "preserve the original factual
  discrepancy even after resolution" (the Phase 3 brief's central
  invariant) means concretely: nothing after this row is created can
  ever change what it says happened.
- The state machine is deliberately a strict subset of the contract's
  full one (Section 12.1): `RECORDED -> INVESTIGATING -> RESOLVED` only.
  The contract's three substantive outcome branches
  (`VENDOR_RESPONSIBILITY`, `STORE_RESPONSIBILITY`, `RETURN_REQUIRED`)
  each drive real economic/physical workflows (`TransferWriteOff`,
  `SupplierCreditNoteRequest`, a return transfer) that are explicitly
  out of M25 Phase 3's scope. `resolution_type` captures the
  INVESTIGATION FINDING only (vendor-caused, source-store-caused,
  destination-caused, transit damage, unknown) as plain data -- never a
  status, and never a trigger for any of those later workflows. No
  state overlapping a later phase's own vocabulary is introduced.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

DISCREPANCY_SOURCE_TYPES = ("TRANSFER_RECEIPT", "GOODS_RECEIPT")
DISCREPANCY_TYPES = ("SHORTAGE", "DAMAGE", "BOTH")
DISCREPANCY_STATUSES = ("RECORDED", "INVESTIGATING", "RESOLVED")
DISCREPANCY_RESOLUTION_TYPES = (
    "VENDOR_CAUSED",
    "SOURCE_STORE_CAUSED",
    "DESTINATION_CAUSED",
    "TRANSIT_DAMAGE",
    "UNKNOWN",
)


class ReceivingDiscrepancy(TimestampMixin, Base):
    __tablename__ = "receiving_discrepancies"
    __table_args__ = (
        # The idempotency backstop (see module docstring): one receiving
        # event/item can never produce two discrepancy rows, regardless
        # of how many times discrepancy creation is attempted against it.
        UniqueConstraint("source_type", "source_id", name="uq_receiving_discrepancies_source"),
        CheckConstraint(
            "source_type IN ('" + "', '".join(DISCREPANCY_SOURCE_TYPES) + "')",
            name="ck_receiving_discrepancies_source_type",
        ),
        CheckConstraint(
            "discrepancy_type IN ('" + "', '".join(DISCREPANCY_TYPES) + "')",
            name="ck_receiving_discrepancies_discrepancy_type",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(DISCREPANCY_STATUSES) + "')",
            name="ck_receiving_discrepancies_status",
        ),
        CheckConstraint(
            "resolution_type IS NULL OR resolution_type IN ('"
            + "', '".join(DISCREPANCY_RESOLUTION_TYPES)
            + "')",
            name="ck_receiving_discrepancies_resolution_type",
        ),
        # A discrepancy must represent a genuine, nonzero physical fact --
        # mirrors the identical rule on InterStoreTransferReceiptItem
        # (Phase 2) that a receipt item may not be all-zero.
        CheckConstraint(
            "quantity_short >= 0 AND quantity_damaged >= 0 AND "
            "(quantity_short + quantity_damaged) > 0",
            name="ck_receiving_discrepancies_qty_positive",
        ),
        # Resolution fields are set together, exactly once, only on the
        # terminal transition -- a single-table proxy for the state
        # machine's own "RESOLVED only via a real resolution" rule.
        CheckConstraint(
            "(status <> 'RESOLVED') OR "
            "(resolved_at IS NOT NULL AND resolution_type IS NOT NULL)",
            name="ck_receiving_discrepancies_resolved_consistency",
        ),
        Index("ix_receiving_discrepancies_store_id", "store_id"),
        Index("ix_receiving_discrepancies_status", "status"),
        Index("ix_receiving_discrepancies_transfer_id", "transfer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Polymorphic reference, no FK constraint -- see module docstring
    # (mirrors JournalEntry.source_type/source_id).
    source_id: Mapped[int] = mapped_column(nullable=False)
    # Denormalized convenience FK for the common TRANSFER_RECEIPT case
    # (query/filter without resolving the polymorphic reference) --
    # mirrors GoodsReceipt.store_id's own "redundant but indexed for
    # query" precedent. NULL for a future GOODS_RECEIPT-sourced row.
    transfer_id: Mapped[int | None] = mapped_column(ForeignKey("inter_store_transfers.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    # The store where the discrepancy was discovered -- the destination
    # store for a transfer receipt (docs/M24D_TECHNICAL_CONTRACT.md
    # Section 20 Scenario 3: "discrepancy store_id = destination"). This
    # is the authorization boundary (Section 16).
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # The OTHER store on a transfer-sourced discrepancy (the source
    # store); NULL for a future GOODS_RECEIPT-sourced row, which has no
    # second store at all.
    counterparty_store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    # Reserved for a future GOODS_RECEIPT-sourced row's vendor; always
    # NULL for a TRANSFER_RECEIPT-sourced row.
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))

    discrepancy_type: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity_short: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    quantity_damaged: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    # Frozen at discrepancy-creation time from the transfer line's own
    # frozen unit_cost_at_shipment -- never recomputed from either
    # store's current WAC (docs/M24D_TECHNICAL_CONTRACT.md Section 7).
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECORDED")

    raised_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    investigated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    investigated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_type: Mapped[str | None] = mapped_column(String(30))
    resolution_notes: Mapped[str | None] = mapped_column(Text)
