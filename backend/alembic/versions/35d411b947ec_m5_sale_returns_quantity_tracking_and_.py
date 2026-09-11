"""m5 sale returns: quantity tracking and idempotency

Revision ID: 35d411b947ec
Revises: 581d2a07f38c
Create Date: 2026-09-11 17:21:24.890996

See docs/M5_RETURNS_VOIDS_REFUNDS.md. Adds:

- `sale_items.quantity_returned` — a maintained cache (mirrors
  `purchase_order_items.quantity_received`'s M3 pattern), incremented
  only by `create_sale_return` under a lock on the parent `Sale` row.
  Nullable -> backfill 0 -> NOT NULL (safe pattern for a populated
  table), plus the bounds CHECK the model declares.
- `sale_returns.client_transaction_id` — the M5 idempotency key, same
  shape/enforcement as `sales.client_transaction_id`/
  `goods_receipts.client_transaction_id`. Nullable -> backfill a
  synthetic `'legacy-' || id` value for any pre-existing rows (there
  should be none, since no service wrote to this table before M5, but
  the safe pattern costs nothing and doesn't assume that) -> NOT NULL.
- `sale_return_items.discount_refunded` / `tax_refunded` /
  `unit_cost_refunded` — nullable -> backfill 0 -> NOT NULL, same
  reasoning (table should be empty; backfill anyway).

All constraints named, per repository convention.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35d411b947ec"
down_revision: Union[str, None] = "581d2a07f38c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SALE_RETURNS_TXN_UQ = "uq_sale_returns_client_transaction_id"

_NEW_PERMISSIONS = [
    ("sales.return.read", "View sale returns and return eligibility"),
    ("sales.return.write", "Process a merchandise return against a completed sale"),
    ("sales.void", "Void an entire completed sale (a full return of every line in one action)"),
]

# role name -> permission codes granted, from this migration's new set
# only (docs/M5_RETURNS_VOIDS_REFUNDS.md "RBAC"). Same ON CONFLICT
# reasoning as the M4 accounting-permissions migration: e6180fca2ee0 (the
# M2 seed migration) reads app.modules.auth.permissions live at
# migration-run time, so a fresh-DB replay of it already includes these
# M5 codes; an existing DB that ran e6180fca2ee0 before M5 existed needs
# them inserted here. ON CONFLICT makes this correct either way.
_ROLE_GRANTS = {
    "Admin": ["sales.return.read", "sales.return.write", "sales.void"],
    "Manager": ["sales.return.read", "sales.return.write", "sales.void"],
    "Cashier": ["sales.return.read", "sales.return.write"],
    "Auditor": ["sales.return.read"],
}


def upgrade() -> None:
    # --- sale_items.quantity_returned -----------------------------------
    op.add_column(
        "sale_items", sa.Column("quantity_returned", sa.Numeric(precision=14, scale=3), nullable=True)
    )
    op.execute("UPDATE sale_items SET quantity_returned = 0 WHERE quantity_returned IS NULL")
    op.alter_column("sale_items", "quantity_returned", nullable=False)
    op.create_check_constraint(
        "ck_sale_items_quantity_returned_bounds",
        "sale_items",
        "quantity_returned >= 0 AND quantity_returned <= quantity",
    )

    # --- sale_return_items new columns -----------------------------------
    op.add_column(
        "sale_return_items",
        sa.Column("discount_refunded", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "sale_return_items",
        sa.Column("tax_refunded", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "sale_return_items",
        sa.Column("unit_cost_refunded", sa.Numeric(precision=14, scale=6), nullable=True),
    )
    op.execute(
        "UPDATE sale_return_items SET discount_refunded = 0 WHERE discount_refunded IS NULL"
    )
    op.execute("UPDATE sale_return_items SET tax_refunded = 0 WHERE tax_refunded IS NULL")
    op.execute(
        "UPDATE sale_return_items SET unit_cost_refunded = 0 WHERE unit_cost_refunded IS NULL"
    )
    op.alter_column("sale_return_items", "discount_refunded", nullable=False)
    op.alter_column("sale_return_items", "tax_refunded", nullable=False)
    op.alter_column("sale_return_items", "unit_cost_refunded", nullable=False)
    op.create_check_constraint(
        "ck_sale_return_items_discount_non_negative", "sale_return_items", "discount_refunded >= 0"
    )
    op.create_check_constraint(
        "ck_sale_return_items_tax_non_negative", "sale_return_items", "tax_refunded >= 0"
    )
    op.create_check_constraint(
        "ck_sale_return_items_cost_non_negative", "sale_return_items", "unit_cost_refunded >= 0"
    )
    op.create_index(
        "ix_sale_return_items_sale_item_id", "sale_return_items", ["sale_item_id"], unique=False
    )

    # --- sale_returns.client_transaction_id -------------------------------
    op.add_column(
        "sale_returns", sa.Column("client_transaction_id", sa.String(length=100), nullable=True)
    )
    op.execute(
        "UPDATE sale_returns SET client_transaction_id = 'legacy-' || id::text "
        "WHERE client_transaction_id IS NULL"
    )
    op.alter_column("sale_returns", "client_transaction_id", nullable=False)
    op.create_unique_constraint(
        _SALE_RETURNS_TXN_UQ, "sale_returns", ["client_transaction_id"]
    )

    # --- Seed the new sales.return.*/sales.void permissions -------------
    conn = op.get_bind()
    permission_ids: dict[str, int] = {}
    for code, description in _NEW_PERMISSIONS:
        permission_ids[code] = conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description, created_at) "
                "VALUES (:code, :description, now()) "
                "ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code "
                "RETURNING id"
            ),
            {"code": code, "description": description},
        ).scalar_one()

    role_ids: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            sa.text("SELECT name, id FROM roles WHERE name = ANY(:names)"),
            {"names": list(_ROLE_GRANTS)},
        )
    }
    for role_name, codes in _ROLE_GRANTS.items():
        role_id = role_ids.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id) "
                    "ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"role_id": role_id, "permission_id": permission_ids[code]},
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": [code for code, _ in _NEW_PERMISSIONS]},
    )
    conn.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": [code for code, _ in _NEW_PERMISSIONS]},
    )

    op.drop_constraint(_SALE_RETURNS_TXN_UQ, "sale_returns", type_="unique")
    op.drop_column("sale_returns", "client_transaction_id")

    op.drop_index("ix_sale_return_items_sale_item_id", table_name="sale_return_items")
    op.drop_constraint(
        "ck_sale_return_items_cost_non_negative", "sale_return_items", type_="check"
    )
    op.drop_constraint(
        "ck_sale_return_items_tax_non_negative", "sale_return_items", type_="check"
    )
    op.drop_constraint(
        "ck_sale_return_items_discount_non_negative", "sale_return_items", type_="check"
    )
    op.drop_column("sale_return_items", "unit_cost_refunded")
    op.drop_column("sale_return_items", "tax_refunded")
    op.drop_column("sale_return_items", "discount_refunded")

    op.drop_constraint("ck_sale_items_quantity_returned_bounds", "sale_items", type_="check")
    op.drop_column("sale_items", "quantity_returned")
