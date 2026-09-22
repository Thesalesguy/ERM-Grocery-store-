"""pre-m15 hardening: stock adjustment idempotency

Revision ID: 1a4bae98d246
Revises: e0d2359bb08a
Create Date: 2026-09-22 00:00:00.000000

See docs/M15_DESIGN.md "Pre-M15 hardening". Adds a nullable, unique
`client_transaction_id` column to `stock_adjustments` — the one financial-
posting flow in the codebase (sales, sale returns/voids, AP invoices/
payments/credit notes, payroll postings, inter-store transfers) that
lacked this idempotency key before now. Mirrors `sales.client_transaction_id`
exactly: NULLable (a caller with no natural key, e.g. post_stock_count's
internal per-variance-line calls, is unaffected — multiple NULLs are
allowed under a UNIQUE constraint in PostgreSQL), UNIQUE (the real
enforcement against a double-submitted request creating two adjustments/
movements/journal entries, not just an application-level check).

This is a pure prerequisite hardening fix, not M15 functionality itself —
kept as its own migration/commit so M15's own migration stays scoped to
the cashier/till/shift feature.

No changes to any other table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1a4bae98d246"
down_revision: Union[str, None] = "e0d2359bb08a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_adjustments",
        sa.Column("client_transaction_id", sa.String(length=100), nullable=True),
    )
    op.create_unique_constraint(
        "uq_stock_adjustments_client_transaction_id",
        "stock_adjustments",
        ["client_transaction_id"],
    )


def downgrade() -> None:
    # No guard needed: this column is purely additive/optional and every
    # pre-M15 caller already omits it, so a downgrade never discards real
    # client-supplied idempotency data that anything depends on for
    # correctness — the worst case is a future retry after downgrade
    # briefly loses the idempotency guarantee for stock adjustments only,
    # not that any recorded financial fact is destroyed.
    op.drop_constraint(
        "uq_stock_adjustments_client_transaction_id", "stock_adjustments", type_="unique"
    )
    op.drop_column("stock_adjustments", "client_transaction_id")
