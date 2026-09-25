"""m19: purchase order idempotency key

Revision ID: 3a0d50ccc909
Revises: 4a83c462dbff
Create Date: 2026-09-23 12:00:00.000000

Adds `purchase_orders.client_transaction_id` -- the same idempotency key
pattern already used by `goods_receipts`/`purchase_returns`/
`purchase_invoices`/`supplier_payments`/`supplier_credit_notes` (M19
discovery: PO creation was the one mutating write in this domain with no
such protection -- a retried/duplicated request could silently create
two separate DRAFT purchase orders). See
app.modules.purchasing.service._create_purchase_order_inner.

Added NOT NULL in three steps (nullable add -> backfill -> set NOT NULL)
rather than directly, since `purchase_orders` may already have rows in
any environment that ran the original M3 migration before this
hardening pass -- the exact same discipline 89a42dfbfaea (M2 hardening:
sale idempotency key) already established for this exact scenario.
Backfilled with a value derived from each row's own id, which is
already unique, so the backfill can never collide with a real client
key.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a0d50ccc909"
down_revision: Union[str, None] = "4a83c462dbff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT_NAME = "uq_purchase_orders_client_transaction_id"


def upgrade() -> None:
    op.add_column(
        "purchase_orders", sa.Column("client_transaction_id", sa.String(length=100), nullable=True)
    )
    op.execute(
        "UPDATE purchase_orders SET client_transaction_id = 'legacy-' || id::text "
        "WHERE client_transaction_id IS NULL"
    )
    op.alter_column("purchase_orders", "client_transaction_id", nullable=False)
    op.create_unique_constraint(_CONSTRAINT_NAME, "purchase_orders", ["client_transaction_id"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "purchase_orders", type_="unique")
    op.drop_column("purchase_orders", "client_transaction_id")
