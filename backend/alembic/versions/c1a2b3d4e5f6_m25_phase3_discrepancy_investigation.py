"""m25 phase3 discrepancy investigation ledger

Revision ID: c1a2b3d4e5f6
Revises: b628fc058834
Create Date: 2026-10-04 12:00:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/12/16/17 for the full
design. Adds exactly one new table, `receiving_discrepancies`: the
generalized discrepancy-investigation ledger. Purely additive -- no
existing table's columns, constraints, or data change. No seed data of
its own; the one new permission this phase introduces
(`inventory.transfer.discrepancy.investigate`) is seeded by a dedicated
migration immediately after this one, mirroring the M16/M20 precedent of
keeping a schema migration and its permission-seed migration separate.

The downgrade is guarded (mirroring the M7/M8/M9/M10/M25-Phase-1/
M25-Phase-2 precedents in this migration chain): it refuses if any
discrepancy row exists at all -- the pre-Phase-3 schema has no table for
them, so downgrading with data present would silently destroy
investigation history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, None] = "b628fc058834"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "receiving_discrepancies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_store_id", sa.Integer(), nullable=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("discrepancy_type", sa.String(length=10), nullable=False),
        sa.Column(
            "quantity_short", sa.Numeric(14, 3), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "quantity_damaged", sa.Numeric(14, 3), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("unit_cost", sa.Numeric(14, 6), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'RECORDED'")
        ),
        sa.Column("raised_by", sa.Integer(), nullable=True),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("investigated_by", sa.Integer(), nullable=True),
        sa.Column("investigated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_type", sa.String(length=30), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["inter_store_transfers.id"],
            name="fk_receiving_discrepancies_transfer_id",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_receiving_discrepancies_product_id"
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_receiving_discrepancies_store_id"
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_store_id"],
            ["stores.id"],
            name="fk_receiving_discrepancies_counterparty_store_id",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name="fk_receiving_discrepancies_supplier_id"
        ),
        sa.ForeignKeyConstraint(
            ["raised_by"], ["users.id"], name="fk_receiving_discrepancies_raised_by"
        ),
        sa.ForeignKeyConstraint(
            ["investigated_by"], ["users.id"], name="fk_receiving_discrepancies_investigated_by"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], name="fk_receiving_discrepancies_resolved_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type", "source_id", name="uq_receiving_discrepancies_source"
        ),
        sa.CheckConstraint(
            "source_type IN ('TRANSFER_RECEIPT', 'GOODS_RECEIPT')",
            name="ck_receiving_discrepancies_source_type",
        ),
        sa.CheckConstraint(
            "discrepancy_type IN ('SHORTAGE', 'DAMAGE', 'BOTH')",
            name="ck_receiving_discrepancies_discrepancy_type",
        ),
        sa.CheckConstraint(
            "status IN ('RECORDED', 'INVESTIGATING', 'RESOLVED')",
            name="ck_receiving_discrepancies_status",
        ),
        sa.CheckConstraint(
            "resolution_type IS NULL OR resolution_type IN "
            "('VENDOR_CAUSED', 'SOURCE_STORE_CAUSED', 'DESTINATION_CAUSED', "
            "'TRANSIT_DAMAGE', 'UNKNOWN')",
            name="ck_receiving_discrepancies_resolution_type",
        ),
        sa.CheckConstraint(
            "quantity_short >= 0 AND quantity_damaged >= 0 AND "
            "(quantity_short + quantity_damaged) > 0",
            name="ck_receiving_discrepancies_qty_positive",
        ),
        sa.CheckConstraint(
            "(status <> 'RESOLVED') OR "
            "(resolved_at IS NOT NULL AND resolution_type IS NOT NULL)",
            name="ck_receiving_discrepancies_resolved_consistency",
        ),
    )
    op.create_index(
        "ix_receiving_discrepancies_store_id", "receiving_discrepancies", ["store_id"]
    )
    op.create_index(
        "ix_receiving_discrepancies_status", "receiving_discrepancies", ["status"]
    )
    op.create_index(
        "ix_receiving_discrepancies_transfer_id", "receiving_discrepancies", ["transfer_id"]
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            discrepancy_rows integer;
        BEGIN
            SELECT COUNT(*) INTO discrepancy_rows FROM receiving_discrepancies;
            IF discrepancy_rows > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || discrepancy_rows::text || ' receiving '
                    'discrepancy row(s) exist -- the pre-Phase-3 schema has no table '
                    'for them';
            END IF;
        END
        $$;
        """
    )

    op.drop_index("ix_receiving_discrepancies_transfer_id", table_name="receiving_discrepancies")
    op.drop_index("ix_receiving_discrepancies_status", table_name="receiving_discrepancies")
    op.drop_index("ix_receiving_discrepancies_store_id", table_name="receiving_discrepancies")
    op.drop_table("receiving_discrepancies")
