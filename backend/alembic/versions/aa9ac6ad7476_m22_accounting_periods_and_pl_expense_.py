"""m22 accounting periods

Revision ID: aa9ac6ad7476
Revises: 9c4c5a209aa9
Create Date: 2026-09-25 18:12:49.297972

See docs/M22_DISCOVERY.md Phase 2 for the full design. Creates
`accounting_periods`: one row per CLOSED date range for one store. A row
here is a denylist entry, not a lifecycle state -- there is no "OPEN" row
to store, since a posting_date not covered by any row is open by default.

Non-overlap per store is enforced at the DB level via an EXCLUDE (GiST)
constraint, the same mechanism app.modules.hr.models already uses for
EmploymentStatusPeriod/EmploymentAssignment/CompensationPeriod (M10). The
btree_gist extension it depends on was already created by that migration
(be26de9d9459), so it is not recreated here.

Rows are never updated or deleted (M22 deliberately does not build reopen
-- see docs/M22_DISCOVERY.md Phase 2/9): UPDATE, DELETE are revoked from
the app runtime role, the identical carve-out
8df037a45976_m4_accounting_core_chart_of_accounts_ already applies to
journal_entries/journal_lines for the same "the DB is the actual
backstop" reason.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ExcludeConstraint

# revision identifiers, used by Alembic.
revision: str = "aa9ac6ad7476"
down_revision: Union[str, None] = "9c4c5a209aa9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLE = "erp_app"


def upgrade() -> None:
    op.create_table(
        "accounting_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("closed_by", sa.Integer(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("period_end >= period_start", name="ck_accounting_periods_valid_range"),
        ExcludeConstraint(
            ("store_id", "="),
            (sa.text("daterange(period_start, period_end, '[]')"), "&&"),
            using="gist",
            name="excl_accounting_periods_no_overlap",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_accounting_periods_store_id_stores"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name="fk_accounting_periods_closed_by_users"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounting_periods_store_id", "accounting_periods", ["store_id"])

    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                EXECUTE 'REVOKE UPDATE, DELETE ON accounting_periods FROM {_APP_ROLE}';
            END IF;
        END
        $$;
        """)


def downgrade() -> None:
    op.drop_index("ix_accounting_periods_store_id", table_name="accounting_periods")
    op.drop_table("accounting_periods")
