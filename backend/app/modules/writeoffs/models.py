"""ORM models for the approval-gated transfer write-off workflow.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/10/20 for the full
design. Summary of the load-bearing decisions:

- `ApprovalPolicyRule` rows are admin-managed data, never code constants
  (Section 10.1) -- no illustrative threshold is ever hard-coded in this
  module. An amount with no matching rule is the explicit "unconfigured
  gap" case the service layer fails closed on (Admin-only), not
  something this schema tries to prevent structurally.
- `TransferWriteOff.required_role_id`/`matched_rule_id` are FROZEN at
  creation time from whatever `ApprovalPolicyRule` applied THEN -- a
  policy change after a write-off is already pending must never silently
  change who may approve an in-flight request (Section 17's "approval-
  policy changes during a pending request" hazard). This is why they
  live on the write-off itself, not re-derived from the current policy
  table at decision time.
- `TransferWriteOff.discrepancy_id` is NOT NULL: this phase's write-offs
  always reference a real, persisted `ReceivingDiscrepancy` (the M25
  Phase 4 brief's own explicit requirement) -- the contract's more
  general, nullable field (for a future aging-alert-triggered write-off
  with no discrepancy, M25 Phase 8, not yet built) is deliberately
  narrowed here rather than implemented speculatively.
- `TransferWriteOff.store_id` is the ATTRIBUTED store (Section 6.2's
  pre-custody-acceptance rule: the transfer's `from_store_id`/source
  store bears a loss discovered as a receiving discrepancy, since that
  quantity never became on-hand stock anywhere) -- always equal to the
  originating discrepancy's `counterparty_store_id`, never its own
  (destination) `store_id`. Getting this backwards would attribute the
  loss to the wrong store's books.
- `WriteOffApproval.write_off_id` is UNIQUE: at most one decision per
  write-off, ever (a denied write-off's discrepancy reopens into
  INVESTIGATING -- Section 12.1/Scenario 11 -- and a FRESH write-off,
  with its own new approval row, may be submitted after a fresh
  investigation conclusion; the original denial is never overwritten).
  `CHECK (initiated_by <> approved_by)` is the DB-level self-approval
  prohibition D2 requires unconditionally (Section 10.2) -- structural,
  not merely a permission check a future code change could weaken.
- `status` deliberately collapses the contract's `APPROVED` step into
  the same transaction as `POSTED`: Section 4 of the M25 Phase 4 brief
  ("Implement the economic consequence only after the required approval
  succeeds") and Session I's own wording treat "approved" and "posted"
  as one atomic outcome, with no separate hand-off actor or step
  described anywhere the brief specifies. `APPROVED` is kept in the
  status vocabulary (matching the contract's own Section 3.1 enum)
  purely so a future phase could reintroduce a real two-step flow
  without a schema change -- nothing in this phase's service layer
  currently produces a row that sits at `APPROVED` without also being
  `POSTED` in the same commit.
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

APPROVAL_ACTION_TYPES = ("TRANSFER_WRITE_OFF",)
WRITE_OFF_STATUSES = ("PENDING_APPROVAL", "APPROVED", "DENIED", "POSTED", "REVERSED")
APPROVAL_DECISIONS = ("APPROVED", "DENIED")


class ApprovalPolicyRule(TimestampMixin, Base):
    __tablename__ = "approval_policy_rules"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('" + "', '".join(APPROVAL_ACTION_TYPES) + "')",
            name="ck_approval_policy_rules_action_type",
        ),
        CheckConstraint("min_amount >= 0", name="ck_approval_policy_rules_min_amount_non_negative"),
        CheckConstraint(
            "max_amount IS NULL OR max_amount > min_amount",
            name="ck_approval_policy_rules_max_greater_than_min",
        ),
        Index("ix_approval_policy_rules_action_scope", "action_type", "scope_store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # NULL = applies to every store (the store-agnostic fallback row --
    # Section 10.1: a store-specific row, when one exists, takes
    # precedence over the store-agnostic row for the same amount range).
    scope_store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    min_amount: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    # NULL = unbounded (the top tier).
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    required_role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    # Reserved for a future escalation path (e.g. no decision within N
    # hours); the contract names this field (Section 3.1) but specifies
    # no trigger semantics for it, so no active escalation behavior is
    # implemented against it in this phase -- stored only.
    escalation_role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class TransferWriteOff(TimestampMixin, Base):
    __tablename__ = "transfer_write_offs"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_transfer_write_offs_quantity_positive"),
        CheckConstraint(
            "status IN ('" + "', '".join(WRITE_OFF_STATUSES) + "')",
            name="ck_transfer_write_offs_status",
        ),
        UniqueConstraint(
            "client_transaction_id", name="uq_transfer_write_offs_client_transaction_id"
        ),
        Index("ix_transfer_write_offs_discrepancy_id", "discrepancy_id"),
        Index("ix_transfer_write_offs_transfer_line_id", "transfer_line_id"),
        Index("ix_transfer_write_offs_store_id", "store_id"),
        Index("ix_transfer_write_offs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("inter_store_transfers.id"), nullable=False)
    transfer_line_id: Mapped[int] = mapped_column(
        ForeignKey("inter_store_transfer_lines.id"), nullable=False
    )
    discrepancy_id: Mapped[int] = mapped_column(
        ForeignKey("receiving_discrepancies.id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Frozen from the discrepancy's own frozen unit_cost (Section 7) --
    # never recomputed from either store's current WAC.
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    # The attributed (source) store -- see module docstring. NOT the
    # discrepancy's own store_id (the destination, where it was found).
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    initiated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Frozen at creation time -- see module docstring ("approval-policy
    # changes during a pending request").
    required_role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    matched_rule_id: Mapped[int | None] = mapped_column(ForeignKey("approval_policy_rules.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_APPROVAL")
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    reversal_journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False)


class WriteOffApproval(TimestampMixin, Base):
    __tablename__ = "write_off_approvals"
    __table_args__ = (
        UniqueConstraint("write_off_id", name="uq_write_off_approvals_write_off_id"),
        CheckConstraint(
            "decision IN ('" + "', '".join(APPROVAL_DECISIONS) + "')",
            name="ck_write_off_approvals_decision",
        ),
        # The structural self-approval prohibition (Section 10.2/22) --
        # D2's unconditional rule, enforced at the DB level so no future
        # application-layer change can accidentally weaken it.
        CheckConstraint(
            "initiated_by <> approved_by", name="ck_write_off_approvals_no_self_approval"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    write_off_id: Mapped[int] = mapped_column(ForeignKey("transfer_write_offs.id"), nullable=False)
    # Redundant copy of TransferWriteOff.initiated_by, kept here so the
    # self-approval CHECK is enforceable on this row alone, without a
    # cross-table constraint (Postgres cannot CHECK against another
    # table).
    initiated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_notes: Mapped[str] = mapped_column(Text, nullable=False)
