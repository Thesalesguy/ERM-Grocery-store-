"""m3 purchasing: supplier code, goods receipt and purchase return idempotency

Revision ID: c82162efb3af
Revises: 89a42dfbfaea
Create Date: 2026-09-11 12:26:05.743699

Three additive changes for M3 (see docs/M3_PURCHASING_RECEIVING_WAC.md):

1. `suppliers.code` — optional short reference code, unique when set.
2. `goods_receipts.store_id` — redundant with `purchase_order.store_id`
   (same pattern `purchase_returns.store_id` already uses) so
   store-scoped authorization checks don't require a join. Backfilled
   from the parent purchase order.
3. `goods_receipts.client_transaction_id` / `purchase_returns
   .client_transaction_id` — idempotency keys (the same pattern
   `sales.client_transaction_id` already uses), required going forward.

`goods_receipts`/`purchase_returns` may already have rows in any
environment that exercised M1's `receive_goods`/manual return testing,
so both NOT NULL columns are added nullable, backfilled, then set NOT
NULL — never added directly NOT NULL against a possibly-populated table.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c82162efb3af"
down_revision: Union[str, None] = "89a42dfbfaea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GOODS_RECEIPTS_STORE_FK = "fk_goods_receipts_store_id_stores"
_GOODS_RECEIPTS_TXN_UQ = "uq_goods_receipts_client_transaction_id"
_PURCHASE_RETURNS_TXN_UQ = "uq_purchase_returns_client_transaction_id"
_SUPPLIERS_CODE_INDEX = "uq_suppliers_code"


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("code", sa.String(length=64), nullable=True))
    op.create_index(
        _SUPPLIERS_CODE_INDEX,
        "suppliers",
        ["code"],
        unique=True,
        postgresql_where=sa.text("code IS NOT NULL"),
    )

    op.add_column("goods_receipts", sa.Column("store_id", sa.Integer(), nullable=True))
    op.add_column(
        "goods_receipts",
        sa.Column("client_transaction_id", sa.String(length=100), nullable=True),
    )
    op.execute(
        "UPDATE goods_receipts gr SET store_id = po.store_id "
        "FROM purchase_orders po WHERE po.id = gr.purchase_order_id AND gr.store_id IS NULL"
    )
    op.execute(
        "UPDATE goods_receipts SET client_transaction_id = 'legacy-' || id::text "
        "WHERE client_transaction_id IS NULL"
    )
    op.alter_column("goods_receipts", "store_id", nullable=False)
    op.alter_column("goods_receipts", "client_transaction_id", nullable=False)
    op.create_index("ix_goods_receipts_store_id", "goods_receipts", ["store_id"], unique=False)
    op.create_unique_constraint(
        _GOODS_RECEIPTS_TXN_UQ, "goods_receipts", ["client_transaction_id"]
    )
    op.create_foreign_key(
        _GOODS_RECEIPTS_STORE_FK, "goods_receipts", "stores", ["store_id"], ["id"]
    )

    op.add_column(
        "purchase_returns",
        sa.Column("client_transaction_id", sa.String(length=100), nullable=True),
    )
    op.execute(
        "UPDATE purchase_returns SET client_transaction_id = 'legacy-' || id::text "
        "WHERE client_transaction_id IS NULL"
    )
    op.alter_column("purchase_returns", "client_transaction_id", nullable=False)
    op.create_unique_constraint(
        _PURCHASE_RETURNS_TXN_UQ, "purchase_returns", ["client_transaction_id"]
    )


def downgrade() -> None:
    op.drop_constraint(_PURCHASE_RETURNS_TXN_UQ, "purchase_returns", type_="unique")
    op.drop_column("purchase_returns", "client_transaction_id")

    op.drop_constraint(_GOODS_RECEIPTS_STORE_FK, "goods_receipts", type_="foreignkey")
    op.drop_constraint(_GOODS_RECEIPTS_TXN_UQ, "goods_receipts", type_="unique")
    op.drop_index("ix_goods_receipts_store_id", table_name="goods_receipts")
    op.drop_column("goods_receipts", "client_transaction_id")
    op.drop_column("goods_receipts", "store_id")

    op.drop_index(_SUPPLIERS_CODE_INDEX, table_name="suppliers", postgresql_where=sa.text("code IS NOT NULL"))
    op.drop_column("suppliers", "code")
