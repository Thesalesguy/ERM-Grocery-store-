"""m2 hardening: sale idempotency key

Revision ID: 89a42dfbfaea
Revises: e6180fca2ee0
Create Date: 2026-09-11 07:46:59.900015

Adds `sales.client_transaction_id` — the client-generated idempotency key
that lets app.modules.sales.service.finalize_sale recognize a retried
checkout request as the same attempt instead of creating a second sale
(M2 hardening audit Section 7). The UNIQUE constraint is the actual
enforcement; the application-level early-return check is only a fast
path — see that function's docstring.

Added NOT NULL in three steps (nullable add -> backfill -> set NOT NULL)
rather than directly, since this table may already have rows in any
environment that ran the original M2 migrations before this hardening
pass — a plain `nullable=False` add would fail against a non-empty table.
Backfilled with a value derived from each row's own id, which is already
unique, so the backfill can never collide with a real client key (which
this migration's own format never generates, and future ones use UUIDs).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "89a42dfbfaea"
down_revision: Union[str, None] = "e6180fca2ee0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT_NAME = "uq_sales_client_transaction_id"


def upgrade() -> None:
    op.add_column("sales", sa.Column("client_transaction_id", sa.String(length=100), nullable=True))
    op.execute(
        "UPDATE sales SET client_transaction_id = 'legacy-' || id::text "
        "WHERE client_transaction_id IS NULL"
    )
    op.alter_column("sales", "client_transaction_id", nullable=False)
    op.create_unique_constraint(_CONSTRAINT_NAME, "sales", ["client_transaction_id"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "sales", type_="unique")
    op.drop_column("sales", "client_transaction_id")
