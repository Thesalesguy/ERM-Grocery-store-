"""m25 phase4 approval gated write offs

Revision ID: a1b2c3d4e5f6
Revises: d2b3c4e5f6a7
Create Date: 2026-10-18 12:00:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/5/6.2/10/20 for the full
design. Adds three new tables (`approval_policy_rules`,
`transfer_write_offs`, `write_off_approvals` -- the approval-gated
write-off workflow for Phase 3's `ReceivingDiscrepancy` ledger), one new
column (`inter_store_transfer_lines.written_off_quantity`, the Section 5
quantity-ledger term this phase's write-off introduces), and widens the
existing `ck_journal_entries_source_type` CHECK to admit the two new
automated source types `TRANSFER_WRITE_OFF`/`TRANSFER_WRITE_OFF_REVERSAL`
(mirrors M10's own `ck_journal_entries_source_type` widening exactly).
Purely additive to every existing table besides that one CHECK -- no
existing column, row, or other constraint changes.

`write_off_approvals` gets UPDATE/DELETE revoked from erp_app, mirroring
M25 Phase 2's `inter_store_transfer_custody_acceptances` precedent
exactly: it is an attestation record, written once per write_off_id
(enforced by its own UNIQUE constraint) and never mutated afterward by
any application code path -- this is what "approval record/audit history
must not be silently editable through ordinary application permissions"
(the Phase 4 brief's own words) means concretely at the DB privilege
level, not merely as an application-code promise.
`transfer_write_offs` deliberately keeps the default grant (its `status`/
`journal_entry_id`/`reversal_journal_entry_id` columns are mutated in
place across its PENDING_APPROVAL -> POSTED/DENIED -> REVERSED
lifecycle); `approval_policy_rules` likewise keeps the default grant (
admin-managed configuration data, the same category store_settings-style
tables already fall into).

The downgrade is guarded (mirroring every prior milestone's precedent in
this chain): it refuses if any `transfer_write_offs` or
`write_off_approvals` row exists at all, or if any
`inter_store_transfer_lines.written_off_quantity` value is nonzero -- the
pre-Phase-4 schema has no representation for any of these, so
downgrading with data present would silently destroy write-off/approval
history.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d2b3c4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLE = "erp_app"

# The full post-Phase-4 SOURCE_TYPES list -- every pre-existing value
# plus the two this phase adds (TRANSFER_WRITE_OFF/
# TRANSFER_WRITE_OFF_REVERSAL), plus the always-present MANUAL value.
# Must stay byte-for-byte in sync with
# app.modules.accounting.models.SOURCE_TYPES.
_NEW_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
    "SUPPLIER_CREDIT_NOTE",
    "INTER_STORE_TRANSFER_SHIP",
    "INTER_STORE_TRANSFER_RECEIVE",
    "PAYROLL_POSTING",
    "PAYROLL_REVERSAL",
    "CASH_SHIFT_VARIANCE",
    "SUPPLIER_PAYMENT_REVERSAL",
    "SUPPLIER_CREDIT_NOTE_REVERSAL",
    "TRANSFER_WRITE_OFF",
    "TRANSFER_WRITE_OFF_REVERSAL",
    "MANUAL",
)

_OLD_SOURCE_TYPES = tuple(
    t for t in _NEW_SOURCE_TYPES if t not in ("TRANSFER_WRITE_OFF", "TRANSFER_WRITE_OFF_REVERSAL")
)


def upgrade() -> None:
    op.create_table(
        "approval_policy_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("scope_store_id", sa.Integer(), nullable=True),
        sa.Column("min_amount", sa.Numeric(14, 6), nullable=False),
        sa.Column("max_amount", sa.Numeric(14, 6), nullable=True),
        sa.Column("required_role_id", sa.Integer(), nullable=False),
        sa.Column("escalation_role_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["scope_store_id"], ["stores.id"], name="fk_approval_policy_rules_scope_store_id"
        ),
        sa.ForeignKeyConstraint(
            ["required_role_id"], ["roles.id"], name="fk_approval_policy_rules_required_role_id"
        ),
        sa.ForeignKeyConstraint(
            ["escalation_role_id"],
            ["roles.id"],
            name="fk_approval_policy_rules_escalation_role_id",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_approval_policy_rules_created_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action_type IN ('TRANSFER_WRITE_OFF')",
            name="ck_approval_policy_rules_action_type",
        ),
        sa.CheckConstraint(
            "min_amount >= 0", name="ck_approval_policy_rules_min_amount_non_negative"
        ),
        sa.CheckConstraint(
            "max_amount IS NULL OR max_amount > min_amount",
            name="ck_approval_policy_rules_max_greater_than_min",
        ),
    )
    op.create_index(
        "ix_approval_policy_rules_action_scope",
        "approval_policy_rules",
        ["action_type", "scope_store_id"],
    )

    op.create_table(
        "transfer_write_offs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("transfer_line_id", sa.Integer(), nullable=False),
        sa.Column("discrepancy_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 6), nullable=False),
        sa.Column("amount", sa.Numeric(14, 6), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("initiated_by", sa.Integer(), nullable=False),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required_role_id", sa.Integer(), nullable=False),
        sa.Column("matched_rule_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'PENDING_APPROVAL'"),
        ),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("reversal_journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["transfer_id"], ["inter_store_transfers.id"], name="fk_transfer_write_offs_transfer_id"
        ),
        sa.ForeignKeyConstraint(
            ["transfer_line_id"],
            ["inter_store_transfer_lines.id"],
            name="fk_transfer_write_offs_transfer_line_id",
        ),
        sa.ForeignKeyConstraint(
            ["discrepancy_id"],
            ["receiving_discrepancies.id"],
            name="fk_transfer_write_offs_discrepancy_id",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_transfer_write_offs_store_id"
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by"], ["users.id"], name="fk_transfer_write_offs_initiated_by"
        ),
        sa.ForeignKeyConstraint(
            ["required_role_id"], ["roles.id"], name="fk_transfer_write_offs_required_role_id"
        ),
        sa.ForeignKeyConstraint(
            ["matched_rule_id"],
            ["approval_policy_rules.id"],
            name="fk_transfer_write_offs_matched_rule_id",
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name="fk_transfer_write_offs_journal_entry_id",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_journal_entry_id"],
            ["journal_entries.id"],
            name="fk_transfer_write_offs_reversal_journal_entry_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_transaction_id", name="uq_transfer_write_offs_client_transaction_id"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_transfer_write_offs_quantity_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'DENIED', 'POSTED', 'REVERSED')",
            name="ck_transfer_write_offs_status",
        ),
    )
    op.create_index(
        "ix_transfer_write_offs_discrepancy_id", "transfer_write_offs", ["discrepancy_id"]
    )
    op.create_index(
        "ix_transfer_write_offs_transfer_line_id", "transfer_write_offs", ["transfer_line_id"]
    )
    op.create_index("ix_transfer_write_offs_store_id", "transfer_write_offs", ["store_id"])
    op.create_index("ix_transfer_write_offs_status", "transfer_write_offs", ["status"])

    op.create_table(
        "write_off_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("write_off_id", sa.Integer(), nullable=False),
        sa.Column("initiated_by", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_notes", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["write_off_id"],
            ["transfer_write_offs.id"],
            name="fk_write_off_approvals_write_off_id",
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by"], ["users.id"], name="fk_write_off_approvals_initiated_by"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name="fk_write_off_approvals_approved_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("write_off_id", name="uq_write_off_approvals_write_off_id"),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'DENIED')", name="ck_write_off_approvals_decision"
        ),
        sa.CheckConstraint(
            "initiated_by <> approved_by", name="ck_write_off_approvals_no_self_approval"
        ),
    )

    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                EXECUTE 'REVOKE UPDATE, DELETE ON write_off_approvals FROM {_APP_ROLE}';
            END IF;
        END
        $$;
        """)

    op.add_column(
        "inter_store_transfer_lines",
        sa.Column(
            "written_off_quantity", sa.Numeric(14, 3), nullable=False, server_default=sa.text("0")
        ),
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_written_off_bounds",
        "inter_store_transfer_lines",
        "written_off_quantity >= 0 AND written_off_quantity <= "
        "(damaged_quantity + declared_short_quantity)",
    )

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            write_off_rows integer;
            approval_rows integer;
            written_off_rows integer;
        BEGIN
            SELECT COUNT(*) INTO write_off_rows FROM transfer_write_offs;
            SELECT COUNT(*) INTO approval_rows FROM write_off_approvals;
            SELECT COUNT(*) INTO written_off_rows FROM inter_store_transfer_lines
                WHERE written_off_quantity <> 0;
            IF write_off_rows > 0 OR approval_rows > 0 OR written_off_rows > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || write_off_rows::text || ' transfer write-off(s), '
                    || approval_rows::text || ' write-off approval(s), and '
                    || written_off_rows::text || ' transfer line(s) with a nonzero '
                    'written_off_quantity exist -- the pre-Phase-4 schema has no '
                    'representation for any of them';
            END IF;
        END
        $$;
        """)

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )

    op.drop_constraint(
        "ck_inter_store_transfer_lines_written_off_bounds",
        "inter_store_transfer_lines",
        type_="check",
    )
    op.drop_column("inter_store_transfer_lines", "written_off_quantity")

    op.drop_table("write_off_approvals")

    op.drop_index("ix_transfer_write_offs_status", table_name="transfer_write_offs")
    op.drop_index("ix_transfer_write_offs_store_id", table_name="transfer_write_offs")
    op.drop_index("ix_transfer_write_offs_transfer_line_id", table_name="transfer_write_offs")
    op.drop_index("ix_transfer_write_offs_discrepancy_id", table_name="transfer_write_offs")
    op.drop_table("transfer_write_offs")

    op.drop_index("ix_approval_policy_rules_action_scope", table_name="approval_policy_rules")
    op.drop_table("approval_policy_rules")
