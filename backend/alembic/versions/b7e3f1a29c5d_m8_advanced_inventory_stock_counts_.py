"""m8 advanced inventory: stock counts, inter-store transfers, replenishment

Revision ID: b7e3f1a29c5d
Revises: a4f2c8e91b6d
Create Date: 2026-09-12 12:00:00.000000

See docs/M8_ADVANCED_INVENTORY_DESIGN.md. Adds:

- `stock_adjustments.stock_count_id` — nullable, traces a
  STOCKTAKE_CORRECTION adjustment back to the count that produced it.
- Two new tables: `stock_counts`, `stock_count_lines`.
- Four new tables: `inter_store_transfers`, `inter_store_transfer_lines`,
  `inter_store_transfer_receipts`, `inter_store_transfer_receipt_items`.
- Widens `inventory_movements`'s type/direction/reference-type CHECK
  constraints to accept TRANSFER_IN/TRANSFER_OUT and the
  'inter_store_transfer' reference type.
- Widens `ck_journal_entries_source_type` to accept
  INTER_STORE_TRANSFER_SHIP / INTER_STORE_TRANSFER_RECEIVE.
- One new Chart-of-Accounts entry: Inventory In Transit (1520).
- Seeds six new permissions (inventory.count.write/review/post,
  inventory.transfer.write/ship/receive) and grants them per
  docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 2/7".

No changes to `products.store_id` or any other M0-M7 table beyond the
one new nullable FK column on `stock_adjustments` — Product stays
store-scoped (see the design doc's Phase 0 section for why).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e3f1a29c5d"
down_revision: Union[str, None] = "a4f2c8e91b6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_MOVEMENT_TYPES = (
    "PURCHASE_RECEIPT",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT_IN",
    "SALE",
    "PURCHASE_RETURN",
    "STOCK_ADJUSTMENT_OUT",
)
_NEW_INCREASING = ("PURCHASE_RECEIPT", "SALE_RETURN", "STOCK_ADJUSTMENT_IN", "TRANSFER_IN")
_NEW_DECREASING = ("SALE", "PURCHASE_RETURN", "STOCK_ADJUSTMENT_OUT", "TRANSFER_OUT")
_NEW_MOVEMENT_TYPES = _NEW_INCREASING + _NEW_DECREASING

_OLD_REFERENCE_TYPES = (
    "purchase_order",
    "sale",
    "sale_return",
    "purchase_return",
    "stock_adjustment",
)
_NEW_REFERENCE_TYPES = _OLD_REFERENCE_TYPES + ("inter_store_transfer",)

_OLD_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "MANUAL",
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
    "SUPPLIER_CREDIT_NOTE",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + (
    "INTER_STORE_TRANSFER_SHIP",
    "INTER_STORE_TRANSFER_RECEIVE",
)

_NEW_ACCOUNT = (
    "1520",
    "Inventory In Transit",
    "ASSET",
    "DEBIT",
    "Goods shipped from a source store but not yet received at the destination store "
    "(docs/M8_ADVANCED_INVENTORY_DESIGN.md 'Transfer accounting'); a pure balance-sheet "
    "reclassification with no P&L impact.",
)

_NEW_PERMISSIONS = [
    ("inventory.count.write", "Create, open, count, recount, and cancel a stock count"),
    ("inventory.count.review", "Review a counted stock count before posting"),
    (
        "inventory.count.post",
        "Post a reviewed stock count, committing its variance to the GL",
    ),
    ("inventory.transfer.write", "Create and cancel a draft inter-store transfer"),
    ("inventory.transfer.ship", "Ship an inter-store transfer from its source store"),
    (
        "inventory.transfer.receive",
        "Receive an inter-store transfer at its destination store",
    ),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
    "Inventory Clerk": [
        "inventory.count.write",
        "inventory.transfer.write",
        "inventory.transfer.ship",
        "inventory.transfer.receive",
    ],
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- Widen inventory_movements' CHECK constraints --------------------
    op.drop_constraint("ck_inventory_movements_type", "inventory_movements", type_="check")
    op.create_check_constraint(
        "ck_inventory_movements_type",
        "inventory_movements",
        "movement_type IN ('" + "', '".join(_NEW_MOVEMENT_TYPES) + "')",
    )
    op.drop_constraint(
        "ck_inventory_movements_reference_type", "inventory_movements", type_="check"
    )
    op.create_check_constraint(
        "ck_inventory_movements_reference_type",
        "inventory_movements",
        "reference_type IN ('" + "', '".join(_NEW_REFERENCE_TYPES) + "')",
    )
    op.drop_constraint("ck_inventory_movements_direction", "inventory_movements", type_="check")
    op.create_check_constraint(
        "ck_inventory_movements_direction",
        "inventory_movements",
        "(movement_type IN ('" + "', '".join(_NEW_INCREASING) + "') AND quantity_delta > 0) OR "
        "(movement_type IN ('" + "', '".join(_NEW_DECREASING) + "') AND quantity_delta < 0)",
    )

    # --- Widen journal_entries' source_type CHECK -------------------------
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )

    # --- Seed the new account ---------------------------------------------
    code, name, account_type, normal_balance, description = _NEW_ACCOUNT
    conn.execute(
        sa.text(
            "INSERT INTO accounts "
            "(code, name, account_type, normal_balance, is_system, is_active, "
            " description, created_at) "
            "VALUES (:code, :name, :account_type, :normal_balance, true, true, "
            " :description, now())"
        ),
        {
            "code": code,
            "name": name,
            "account_type": account_type,
            "normal_balance": normal_balance,
            "description": description,
        },
    )

    # --- stock_counts -------------------------------------------------------
    op.create_table(
        "stock_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("count_number", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column(
            "category_id", sa.Integer(), sa.ForeignKey("product_categories.id"), nullable=True
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("opened_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_stock_counts_count_number", "stock_counts", ["count_number"]
    )
    op.create_check_constraint(
        "ck_stock_counts_status",
        "stock_counts",
        "status IN ('DRAFT', 'OPEN', 'COUNTED', 'REVIEWED', 'POSTED', 'CANCELLED')",
    )
    op.create_index("ix_stock_counts_store_id", "stock_counts", ["store_id"])
    op.create_index("ix_stock_counts_status", "stock_counts", ["status"])

    # --- stock_count_lines ---------------------------------------------------
    op.create_table(
        "stock_count_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stock_count_id", sa.Integer(), sa.ForeignKey("stock_counts.id"), nullable=False
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("expected_quantity", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("expected_unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("counted_quantity", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("counted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recount_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_stock_count_lines_count_product",
        "stock_count_lines",
        ["stock_count_id", "product_id"],
    )
    op.create_check_constraint(
        "ck_stock_count_lines_expected_non_negative",
        "stock_count_lines",
        "expected_quantity IS NULL OR expected_quantity >= 0",
    )
    op.create_check_constraint(
        "ck_stock_count_lines_counted_non_negative",
        "stock_count_lines",
        "counted_quantity IS NULL OR counted_quantity >= 0",
    )
    op.create_index(
        "ix_stock_count_lines_stock_count_id", "stock_count_lines", ["stock_count_id"]
    )
    op.create_index("ix_stock_count_lines_product_id", "stock_count_lines", ["product_id"])

    # --- stock_adjustments.stock_count_id ------------------------------------
    op.add_column(
        "stock_adjustments",
        sa.Column(
            "stock_count_id", sa.Integer(), sa.ForeignKey("stock_counts.id"), nullable=True
        ),
    )

    # --- inter_store_transfers ------------------------------------------------
    op.create_table(
        "inter_store_transfers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("to_store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("transfer_number", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("ship_client_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("shipped_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_inter_store_transfers_number", "inter_store_transfers", ["transfer_number"]
    )
    op.create_unique_constraint(
        "uq_inter_store_transfers_ship_client_transaction_id",
        "inter_store_transfers",
        ["ship_client_transaction_id"],
    )
    op.create_check_constraint(
        "ck_inter_store_transfers_status",
        "inter_store_transfers",
        "status IN ('DRAFT', 'SHIPPED', 'CANCELLED')",
    )
    op.create_check_constraint(
        "ck_inter_store_transfers_distinct_stores",
        "inter_store_transfers",
        "from_store_id <> to_store_id",
    )
    op.create_index(
        "ix_inter_store_transfers_from_store_id", "inter_store_transfers", ["from_store_id"]
    )
    op.create_index(
        "ix_inter_store_transfers_to_store_id", "inter_store_transfers", ["to_store_id"]
    )
    op.create_index("ix_inter_store_transfers_status", "inter_store_transfers", ["status"])

    # --- inter_store_transfer_lines --------------------------------------------
    op.create_table(
        "inter_store_transfer_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "transfer_id", sa.Integer(), sa.ForeignKey("inter_store_transfers.id"), nullable=False
        ),
        sa.Column("source_product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column(
            "destination_product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False
        ),
        sa.Column("requested_quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column(
            "shipped_quantity", sa.Numeric(precision=14, scale=3), nullable=False, server_default="0"
        ),
        sa.Column(
            "received_quantity",
            sa.Numeric(precision=14, scale=3),
            nullable=False,
            server_default="0",
        ),
        sa.Column("unit_cost_at_shipment", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_requested_positive",
        "inter_store_transfer_lines",
        "requested_quantity > 0",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_shipped_bounds",
        "inter_store_transfer_lines",
        "shipped_quantity >= 0 AND shipped_quantity <= requested_quantity",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_received_bounds",
        "inter_store_transfer_lines",
        "received_quantity >= 0 AND received_quantity <= shipped_quantity",
    )
    op.create_index(
        "ix_inter_store_transfer_lines_transfer_id", "inter_store_transfer_lines", ["transfer_id"]
    )
    op.create_index(
        "ix_inter_store_transfer_lines_source_product_id",
        "inter_store_transfer_lines",
        ["source_product_id"],
    )
    op.create_index(
        "ix_inter_store_transfer_lines_destination_product_id",
        "inter_store_transfer_lines",
        ["destination_product_id"],
    )

    # --- inter_store_transfer_receipts --------------------------------------
    op.create_table(
        "inter_store_transfer_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "transfer_id", sa.Integer(), sa.ForeignKey("inter_store_transfers.id"), nullable=False
        ),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("received_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_inter_store_transfer_receipts_client_transaction_id",
        "inter_store_transfer_receipts",
        ["client_transaction_id"],
    )
    op.create_index(
        "ix_inter_store_transfer_receipts_transfer_id",
        "inter_store_transfer_receipts",
        ["transfer_id"],
    )

    # --- inter_store_transfer_receipt_items -----------------------------------
    op.create_table(
        "inter_store_transfer_receipt_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "inter_store_transfer_receipt_id",
            sa.Integer(),
            sa.ForeignKey("inter_store_transfer_receipts.id"),
            nullable=False,
        ),
        sa.Column(
            "inter_store_transfer_line_id",
            sa.Integer(),
            sa.ForeignKey("inter_store_transfer_lines.id"),
            nullable=False,
        ),
        sa.Column("quantity_received", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_receipt_items_qty_positive",
        "inter_store_transfer_receipt_items",
        "quantity_received > 0",
    )
    op.create_index(
        "ix_inter_store_transfer_receipt_items_receipt_id",
        "inter_store_transfer_receipt_items",
        ["inter_store_transfer_receipt_id"],
    )
    op.create_index(
        "ix_inter_store_transfer_receipt_items_line_id",
        "inter_store_transfer_receipt_items",
        ["inter_store_transfer_line_id"],
    )

    # --- Seed the new permissions and grant them ---------------------------
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

    # --- Guard FIRST, before any destructive step (same discipline as
    # 36173e29a9f0's and a4f2c8e91b6d's own downgrades) — real data this
    # M7 schema cannot represent must refuse the downgrade loudly, not
    # crash partway through or silently destroy history.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM inventory_movements WHERE movement_type IN "
        "('TRANSFER_IN', 'TRANSFER_OUT')) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: inventory_movements already has TRANSFER_IN/TRANSFER_OUT rows "
        "from real inter-store transfers — the M7 schema this downgrades to cannot "
        "represent them and they can never be deleted (the ledger is append-only).'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM stock_adjustments WHERE stock_count_id IS NOT NULL) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more stock_adjustments trace back to a posted stock "
        "count — dropping that column would silently destroy real financial history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_lines jl JOIN accounts a ON a.id = jl.account_id "
        "WHERE a.code = '1520') THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_lines already reference the Inventory In Transit "
        "account (1520) from real posted transfer transactions.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_entries WHERE source_type IN "
        "('INTER_STORE_TRANSFER_SHIP', 'INTER_STORE_TRANSFER_RECEIVE')) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_entries has M8 transfer rows that would violate the "
        "narrower ck_journal_entries_source_type constraint.'; "
        "END IF; "
        "END $$;"
    )

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

    op.drop_table("inter_store_transfer_receipt_items")
    op.drop_table("inter_store_transfer_receipts")
    op.drop_table("inter_store_transfer_lines")
    op.drop_table("inter_store_transfers")

    op.drop_column("stock_adjustments", "stock_count_id")
    op.drop_table("stock_count_lines")
    op.drop_table("stock_counts")

    conn.execute(sa.text("DELETE FROM accounts WHERE code = '1520'"))

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )

    op.drop_constraint("ck_inventory_movements_direction", "inventory_movements", type_="check")
    op.drop_constraint(
        "ck_inventory_movements_reference_type", "inventory_movements", type_="check"
    )
    op.drop_constraint("ck_inventory_movements_type", "inventory_movements", type_="check")
    _old_increasing = ("PURCHASE_RECEIPT", "SALE_RETURN", "STOCK_ADJUSTMENT_IN")
    _old_decreasing = ("SALE", "PURCHASE_RETURN", "STOCK_ADJUSTMENT_OUT")
    op.create_check_constraint(
        "ck_inventory_movements_type",
        "inventory_movements",
        "movement_type IN ('" + "', '".join(_OLD_MOVEMENT_TYPES) + "')",
    )
    op.create_check_constraint(
        "ck_inventory_movements_reference_type",
        "inventory_movements",
        "reference_type IN ('" + "', '".join(_OLD_REFERENCE_TYPES) + "')",
    )
    op.create_check_constraint(
        "ck_inventory_movements_direction",
        "inventory_movements",
        "(movement_type IN ('" + "', '".join(_old_increasing) + "') AND quantity_delta > 0) OR "
        "(movement_type IN ('" + "', '".join(_old_decreasing) + "') AND quantity_delta < 0)",
    )
