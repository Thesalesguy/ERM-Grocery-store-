"""m20: fiscal integration boundary

Revision ID: c8337b04ca47
Revises: 3a0d50ccc909
Create Date: 2026-09-25 12:00:00.000000

Adds the jurisdiction-neutral fiscal-integration boundary from
docs/M20_DESIGN.md: `stores.legal_name`/`tax_registration_number`
(nullable legal/business identity), `fiscal_configs` (per-store, one row,
`is_enabled=False` by default so this is inert everywhere until an
operator explicitly configures a provider -- see
docs/M20_DISCOVERY.md Section 1: no tax jurisdiction is established in
this project), and `fiscal_submissions` (the durable per-sale submission
ledger, `sale_id` UNIQUE as its own idempotency enforcement).

No backfill needed anywhere in this migration: the two `Store` columns
are nullable with no default requiring one, and both new tables are
brand new with no prior data. This migration itself does not touch the
sale's own financial record (a `FiscalSubmission` is a status/attempt
ledger ABOUT a `Sale`, not the record itself), but any row a real
deployment later creates IS a compliance-relevant record (its request/
response payload and fiscal_reference) -- downgrade refuses if any
fiscal_submissions row, any enabled fiscal_configs row, or any store's
legal_name/tax_registration_number exists, exactly mirroring
e0d2359bb08a's own guard-before-destroy discipline.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8337b04ca47"
down_revision: Union[str, None] = "3a0d50ccc909"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("legal_name", sa.String(length=255), nullable=True))
    op.add_column(
        "stores", sa.Column("tax_registration_number", sa.String(length=100), nullable=True)
    )

    op.create_table(
        "fiscal_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider_name", sa.String(length=50), nullable=False, server_default="NULL"),
        sa.Column("credential_reference", sa.String(length=255), nullable=True),
        sa.Column("submission_endpoint", sa.String(length=500), nullable=True),
        sa.Column("retry_max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "retry_max_attempts > 0", name="ck_fiscal_configs_retry_max_attempts_positive"
        ),
    )
    op.create_index(
        "ix_fiscal_configs_store_id", "fiscal_configs", ["store_id"], unique=True
    )

    op.create_table(
        "fiscal_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("provider_name", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fiscal_reference", sa.String(length=255), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'FAILED', 'ACKNOWLEDGED', 'REJECTED', 'EXHAUSTED')",
            name="ck_fiscal_submissions_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_fiscal_submissions_attempt_count_non_negative"
        ),
        sa.CheckConstraint("max_attempts > 0", name="ck_fiscal_submissions_max_attempts_positive"),
    )
    op.create_index(
        "ix_fiscal_submissions_sale_id", "fiscal_submissions", ["sale_id"], unique=True
    )
    op.create_index("ix_fiscal_submissions_store_id", "fiscal_submissions", ["store_id"])
    op.create_index("ix_fiscal_submissions_status", "fiscal_submissions", ["status"])


def downgrade() -> None:
    # Guard FIRST, before any destructive step (same discipline as
    # e0d2359bb08a's own downgrade): a FiscalSubmission is a real
    # compliance-relevant record about an actual sale (its request/
    # response payload and fiscal_reference, if any) -- dropping the
    # table must never silently discard one that has ever been created,
    # even though this migration itself seeds none. Likewise for a
    # store's own legal identity fields and a live (is_enabled) fiscal
    # config -- real operator-entered configuration, not schema.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM fiscal_submissions) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more fiscal_submissions rows exist -- dropping this "
        "table would silently destroy real fiscal-submission history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM fiscal_configs WHERE is_enabled IS TRUE) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more stores have fiscal submission enabled -- dropping "
        "this table would silently discard real store configuration.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM stores WHERE legal_name IS NOT NULL "
        "OR tax_registration_number IS NOT NULL) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more stores have a legal_name/tax_registration_number set "
        "-- dropping these columns would silently discard real business-identity data.'; "
        "END IF; "
        "END $$;"
    )

    op.drop_index("ix_fiscal_submissions_status", table_name="fiscal_submissions")
    op.drop_index("ix_fiscal_submissions_store_id", table_name="fiscal_submissions")
    op.drop_index("ix_fiscal_submissions_sale_id", table_name="fiscal_submissions")
    op.drop_table("fiscal_submissions")

    op.drop_index("ix_fiscal_configs_store_id", table_name="fiscal_configs")
    op.drop_table("fiscal_configs")

    op.drop_column("stores", "tax_registration_number")
    op.drop_column("stores", "legal_name")
