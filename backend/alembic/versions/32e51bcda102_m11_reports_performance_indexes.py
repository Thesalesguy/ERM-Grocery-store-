"""m11 reports performance indexes

Revision ID: 32e51bcda102
Revises: 1e832b76969e
Create Date: 2026-09-13 18:46:28.142750

Two indexes identified during M11 Phase 11 (docs/M11_DESIGN.md Section
11) that the reports module's own queries need but nothing in M0-M10
ever required:

- `ix_sales_store_completed` (store_id, completed_at): every sales
  report filters by store_id and a completed_at range
  (app.modules.reports.service._apply_sale_scope). The existing
  `ix_sales_store_created` indexes created_at, which is set once on
  INSERT for every sale (including still-OPEN ones) and is NOT what
  these reports filter on -- completed_at is null until finalize_sale
  and is the column every date-ranged report query actually uses.

- `ix_payroll_periods_store_status` (store_id, status): every payroll
  report (payroll_cost_summary, headcount, the KPI dashboard) filters
  PayrollPeriod by store_id AND status='POSTED' (or the pending-status
  tuple) together. The existing ix_payroll_periods_store_id and
  ix_payroll_periods_status are single-column and cannot satisfy that
  combined filter as efficiently as one composite index.

Purely additive: no data is touched, no existing constraint is
widened or narrowed, nothing here changes any query result -- only
how fast Postgres can produce it. No downgrade guard is needed for
the same reason as 1e832b76969e's: dropping an index can never lose
data or relax an invariant.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32e51bcda102"
down_revision: str | None = "1e832b76969e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_sales_store_completed", "sales", ["store_id", "completed_at"], unique=False)
    op.create_index(
        "ix_payroll_periods_store_status",
        "payroll_periods",
        ["store_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_periods_store_status", table_name="payroll_periods")
    op.drop_index("ix_sales_store_completed", table_name="sales")
