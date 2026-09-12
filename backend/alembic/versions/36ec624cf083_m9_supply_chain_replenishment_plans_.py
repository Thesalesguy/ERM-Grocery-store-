"""m9 advanced supply chain: supplier product catalog, replenishment plans

Revision ID: 36ec624cf083
Revises: b7e3f1a29c5d
Create Date: 2026-09-12 13:00:00.000000

See docs/M9_SUPPLY_CHAIN_DESIGN.md. Adds:

- `supplier_products` — the supplier-product catalog/pricing table that
  did not exist anywhere before M9 (verified by grep in Phase 0), doubling
  as effective-dated price history (Design Decision 2).
- `replenishment_plans` — the persisted recommendation/approval/execution
  record (Design Decision 4/5).
- `products.target_stock_quantity` / `products.minimum_stock_quantity` —
  two new nullable columns on the EXISTING store-scoped Product row
  (Design Decision 10), not a new (store, product)-keyed policy table.
- `purchase_orders.replenishment_plan_id` / `inter_store_transfers.replenishment_plan_id`
  — nullable back-links from a generated document to the plan that
  created it (Design Decision 9). Both reference `replenishment_plans`,
  which is created in this same migration BEFORE these ALTER TABLEs run,
  so there is no circular-FK ordering problem despite `replenishment_plans`
  itself referencing the (pre-existing) `purchase_orders`/
  `inter_store_transfers` tables the other way.
- Four new permissions (supply_chain.read/plan/approve/execute) and role
  grants per docs/M9_SUPPLY_CHAIN_DESIGN.md design answer #19 / #17.

No changes to any M0-M8 accounting, purchasing, AP, or transfer table
beyond the two new nullable back-link columns above — every existing
table's own columns, constraints, and data are untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "36ec624cf083"
down_revision: Union[str, None] = "b7e3f1a29c5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PLAN_STATUSES = ("RECOMMENDED", "APPROVED", "EXECUTED", "STALE", "CANCELLED")
_SOURCE_TYPES = ("SUPPLIER", "TRANSFER")
_URGENCY_LEVELS = ("NORMAL", "URGENT")

_NEW_PERMISSIONS = [
    (
        "supply_chain.read",
        "View replenishment plans, supplier catalog, and supply-chain metrics",
    ),
    ("supply_chain.plan", "Generate replenishment recommendations and cancel a plan"),
    ("supply_chain.approve", "Approve a replenishment recommendation"),
    (
        "supply_chain.execute",
        "Execute an approved plan into a real purchase order or transfer",
    ),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
    "Inventory Clerk": ["supply_chain.read", "supply_chain.plan"],
    "Auditor": ["supply_chain.read"],
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- products: two new nullable policy columns -------------------------
    op.add_column(
        "products", sa.Column("target_stock_quantity", sa.Numeric(14, 3), nullable=True)
    )
    op.add_column(
        "products", sa.Column("minimum_stock_quantity", sa.Numeric(14, 3), nullable=True)
    )
    op.create_check_constraint(
        "ck_products_target_stock_non_negative",
        "products",
        "target_stock_quantity IS NULL OR target_stock_quantity >= 0",
    )
    op.create_check_constraint(
        "ck_products_minimum_stock_non_negative",
        "products",
        "minimum_stock_quantity IS NULL OR minimum_stock_quantity >= 0",
    )

    # --- supplier_products ---------------------------------------------------
    op.create_table(
        "supplier_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("supplier_sku", sa.String(length=64), nullable=True),
        sa.Column(
            "pack_size", sa.Numeric(precision=14, scale=3), nullable=False, server_default="1"
        ),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("minimum_order_quantity", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_supplier_products_effective",
        "supplier_products",
        ["supplier_id", "product_id", "effective_date"],
    )
    op.create_check_constraint(
        "ck_supplier_products_pack_size_positive", "supplier_products", "pack_size > 0"
    )
    op.create_check_constraint(
        "ck_supplier_products_unit_cost_non_negative", "supplier_products", "unit_cost >= 0"
    )
    op.create_check_constraint(
        "ck_supplier_products_moq_positive",
        "supplier_products",
        "minimum_order_quantity IS NULL OR minimum_order_quantity > 0",
    )
    op.create_check_constraint(
        "ck_supplier_products_lead_time_non_negative",
        "supplier_products",
        "lead_time_days IS NULL OR lead_time_days >= 0",
    )
    op.create_index("ix_supplier_products_supplier_id", "supplier_products", ["supplier_id"])
    op.create_index("ix_supplier_products_product_id", "supplier_products", ["product_id"])

    # --- replenishment_plans -------------------------------------------------
    # References purchase_orders/inter_store_transfers, both of which
    # already exist (M3/M8) — no circular-FK problem. The other direction
    # of the link (purchase_orders/inter_store_transfers -> this table) is
    # added as an ALTER TABLE further below, after this table exists.
    op.create_table(
        "replenishment_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation_batch_id", sa.String(length=36), nullable=False),
        sa.Column(
            "destination_store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("needed_quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("suggested_quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("source_store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=True),
        sa.Column("suggested_unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("urgency", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="RECOMMENDED"),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_client_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("executed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_quantity", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("executed_unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column(
            "generated_purchase_order_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id"),
            nullable=True,
        ),
        sa.Column(
            "generated_transfer_id",
            sa.Integer(),
            sa.ForeignKey("inter_store_transfers.id"),
            nullable=True,
        ),
        sa.Column("stale_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_replenishment_plans_execution_client_transaction_id",
        "replenishment_plans",
        ["execution_client_transaction_id"],
    )
    op.create_check_constraint(
        "ck_replenishment_plans_status",
        "replenishment_plans",
        "status IN ('" + "', '".join(_PLAN_STATUSES) + "')",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_source_type",
        "replenishment_plans",
        "source_type IN ('" + "', '".join(_SOURCE_TYPES) + "')",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_urgency",
        "replenishment_plans",
        "urgency IN ('" + "', '".join(_URGENCY_LEVELS) + "')",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_needed_positive", "replenishment_plans", "needed_quantity > 0"
    )
    op.create_check_constraint(
        "ck_replenishment_plans_suggested_positive",
        "replenishment_plans",
        "suggested_quantity > 0",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_executed_positive",
        "replenishment_plans",
        "executed_quantity IS NULL OR executed_quantity > 0",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_source_matches_type",
        "replenishment_plans",
        "(source_type = 'SUPPLIER' AND supplier_id IS NOT NULL AND source_store_id IS NULL) "
        "OR (source_type = 'TRANSFER' AND source_store_id IS NOT NULL AND supplier_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_replenishment_plans_source_distinct_from_destination",
        "replenishment_plans",
        "source_store_id IS NULL OR source_store_id <> destination_store_id",
    )
    # A structural guarantee that "plan says EXECUTED" and "a real
    # PO/transfer exists" can never disagree (task Section 20/29 self-audit
    # question 17/18) — enforced here, not only in application code.
    op.create_check_constraint(
        "ck_replenishment_plans_execution_link_matches_status",
        "replenishment_plans",
        "(status = 'EXECUTED' AND source_type = 'SUPPLIER' "
        " AND generated_purchase_order_id IS NOT NULL AND generated_transfer_id IS NULL) "
        "OR (status = 'EXECUTED' AND source_type = 'TRANSFER' "
        " AND generated_transfer_id IS NOT NULL AND generated_purchase_order_id IS NULL) "
        "OR (status <> 'EXECUTED' "
        " AND generated_purchase_order_id IS NULL AND generated_transfer_id IS NULL)",
    )
    op.create_index(
        "uq_replenishment_plans_generated_po",
        "replenishment_plans",
        ["generated_purchase_order_id"],
        unique=True,
        postgresql_where=sa.text("generated_purchase_order_id IS NOT NULL"),
    )
    op.create_index(
        "uq_replenishment_plans_generated_transfer",
        "replenishment_plans",
        ["generated_transfer_id"],
        unique=True,
        postgresql_where=sa.text("generated_transfer_id IS NOT NULL"),
    )
    op.create_index(
        "ix_replenishment_plans_destination_store_id",
        "replenishment_plans",
        ["destination_store_id"],
    )
    op.create_index("ix_replenishment_plans_product_id", "replenishment_plans", ["product_id"])
    op.create_index("ix_replenishment_plans_status", "replenishment_plans", ["status"])
    op.create_index(
        "ix_replenishment_plans_generation_batch_id",
        "replenishment_plans",
        ["generation_batch_id"],
    )

    # --- back-links from purchase_orders / inter_store_transfers ------------
    op.add_column(
        "purchase_orders",
        sa.Column(
            "replenishment_plan_id",
            sa.Integer(),
            sa.ForeignKey("replenishment_plans.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "inter_store_transfers",
        sa.Column(
            "replenishment_plan_id",
            sa.Integer(),
            sa.ForeignKey("replenishment_plans.id"),
            nullable=True,
        ),
    )

    # --- seed permissions and grant them -------------------------------------
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
    # a4f2c8e91b6d's / b7e3f1a29c5d's own downgrades) — real M9 data the
    # M8 schema cannot represent must refuse the downgrade loudly, not
    # crash partway through or silently destroy history.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM purchase_orders WHERE replenishment_plan_id IS NOT NULL) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more purchase_orders were generated from a replenishment "
        "plan — dropping that column would silently destroy real traceability, and the PO "
        "itself may already have real receipts/invoices against it.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM inter_store_transfers WHERE replenishment_plan_id IS NOT NULL) "
        "THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more inter_store_transfers were generated from a "
        "replenishment plan — dropping that column would silently destroy real "
        "traceability, and the transfer itself may already have shipped.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM replenishment_plans WHERE status <> 'RECOMMENDED') THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more replenishment_plans have been approved, executed, "
        "marked stale, or cancelled — dropping this table would destroy that real decision "
        "history. Only plans still in RECOMMENDED (never acted on) may be discarded.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM supplier_products) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: supplier_products has real supplier catalog/pricing data — "
        "dropping this table would destroy it with no other representation anywhere in "
        "the schema.'; "
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

    op.drop_column("inter_store_transfers", "replenishment_plan_id")
    op.drop_column("purchase_orders", "replenishment_plan_id")

    op.drop_table("replenishment_plans")
    op.drop_table("supplier_products")

    op.drop_constraint("ck_products_minimum_stock_non_negative", "products", type_="check")
    op.drop_constraint("ck_products_target_stock_non_negative", "products", type_="check")
    op.drop_column("products", "minimum_stock_quantity")
    op.drop_column("products", "target_stock_quantity")
