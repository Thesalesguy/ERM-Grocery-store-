"""m25 phase2 extended receiving and custody acceptance

Revision ID: b628fc058834
Revises: 75d685966c98
Create Date: 2026-10-04 00:00:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/5/11/23 for the full
design. Extends inter-store transfer receiving to capture the actual
physical result (good / damaged / declared-short) instead of a single
"received" quantity, and adds the immutable custody-acceptance record
(one row per receiving event).

Purely additive and backward compatible: the two new columns on each
table default to 0 for every existing row, and the CHECK constraints
they replace are each a strict widening (every row that satisfied the
old, narrower constraint still satisfies the new one, since the new
terms are all non-negative). No existing accounting posting, inventory
movement, or GL formula changes -- only the "good" quantity has ever
moved inventory or posted to the GL, and that remains true after this
migration (see `app.modules.transfers.service.receive_transfer`,
unchanged in this respect).

The downgrade is guarded (mirroring the M7/M8/M9/M10/M25-Phase-1
precedents in this migration chain): it refuses if any row carries data
the pre-Phase-2 schema cannot represent -- a nonzero damaged/declared-short
quantity on a line or receipt item, or any custody-acceptance row at all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b628fc058834"
down_revision: Union[str, None] = "75d685966c98"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLE = "erp_app"


def upgrade() -> None:
    op.add_column(
        "inter_store_transfer_lines",
        sa.Column(
            "damaged_quantity", sa.Numeric(14, 3), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "inter_store_transfer_lines",
        sa.Column(
            "declared_short_quantity",
            sa.Numeric(14, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.drop_constraint(
        "ck_inter_store_transfer_lines_received_bounds",
        "inter_store_transfer_lines",
        type_="check",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_received_bounds",
        "inter_store_transfer_lines",
        "received_quantity >= 0 AND damaged_quantity >= 0 AND "
        "declared_short_quantity >= 0 AND "
        "(received_quantity + damaged_quantity + declared_short_quantity) <= shipped_quantity",
    )

    op.add_column(
        "inter_store_transfer_receipt_items",
        sa.Column(
            "quantity_damaged", sa.Numeric(14, 3), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "inter_store_transfer_receipt_items",
        sa.Column(
            "quantity_declared_short",
            sa.Numeric(14, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column(
        "inter_store_transfer_receipt_items",
        "quantity_received",
        existing_type=sa.Numeric(14, 3),
        server_default=sa.text("0"),
    )
    op.drop_constraint(
        "ck_inter_store_transfer_receipt_items_qty_positive",
        "inter_store_transfer_receipt_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_receipt_items_qty_positive",
        "inter_store_transfer_receipt_items",
        "quantity_received >= 0 AND quantity_damaged >= 0 AND "
        "quantity_declared_short >= 0 AND "
        "(quantity_received + quantity_damaged + quantity_declared_short) > 0",
    )

    op.create_table(
        "inter_store_transfer_custody_acceptances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("inter_store_transfer_receipt_id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("accepted_by", sa.Integer(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_digital_acknowledgment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["inter_store_transfers.id"],
            name="fk_inter_store_transfer_custody_acceptances_transfer_id",
        ),
        sa.ForeignKeyConstraint(
            ["inter_store_transfer_receipt_id"],
            ["inter_store_transfer_receipts.id"],
            name="fk_inter_store_transfer_custody_acceptances_receipt_id",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
            name="fk_inter_store_transfer_custody_acceptances_store_id",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by"],
            ["users.id"],
            name="fk_inter_store_transfer_custody_acceptances_accepted_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inter_store_transfer_receipt_id",
            name="uq_inter_store_transfer_custody_acceptances_receipt_id",
        ),
    )
    op.create_index(
        "ix_inter_store_transfer_custody_acceptances_transfer_id",
        "inter_store_transfer_custody_acceptances",
        ["transfer_id"],
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                EXECUTE 'REVOKE UPDATE, DELETE ON '
                    'inter_store_transfer_custody_acceptances FROM {_APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            damaged_or_short_lines integer;
            damaged_or_short_items integer;
            custody_rows integer;
        BEGIN
            SELECT COUNT(*) INTO damaged_or_short_lines
            FROM inter_store_transfer_lines
            WHERE damaged_quantity > 0 OR declared_short_quantity > 0;
            IF damaged_or_short_lines > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || damaged_or_short_lines::text || ' transfer '
                    'line(s) carry a nonzero damaged/declared-short quantity the '
                    'pre-Phase-2 schema cannot represent';
            END IF;

            SELECT COUNT(*) INTO damaged_or_short_items
            FROM inter_store_transfer_receipt_items
            WHERE quantity_damaged > 0 OR quantity_declared_short > 0 OR quantity_received = 0;
            IF damaged_or_short_items > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || damaged_or_short_items::text || ' receipt '
                    'item(s) carry data the pre-Phase-2 schema cannot represent '
                    '(nonzero damaged/declared-short, or a zero good quantity)';
            END IF;

            SELECT COUNT(*) INTO custody_rows
            FROM inter_store_transfer_custody_acceptances;
            IF custody_rows > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || custody_rows::text || ' custody-acceptance '
                    'row(s) exist -- the pre-Phase-2 schema has no table for them';
            END IF;
        END
        $$;
        """
    )

    op.drop_index(
        "ix_inter_store_transfer_custody_acceptances_transfer_id",
        table_name="inter_store_transfer_custody_acceptances",
    )
    op.drop_table("inter_store_transfer_custody_acceptances")

    op.drop_constraint(
        "ck_inter_store_transfer_receipt_items_qty_positive",
        "inter_store_transfer_receipt_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_receipt_items_qty_positive",
        "inter_store_transfer_receipt_items",
        "quantity_received > 0",
    )
    op.alter_column(
        "inter_store_transfer_receipt_items",
        "quantity_received",
        existing_type=sa.Numeric(14, 3),
        server_default=None,
    )
    op.drop_column("inter_store_transfer_receipt_items", "quantity_declared_short")
    op.drop_column("inter_store_transfer_receipt_items", "quantity_damaged")

    op.drop_constraint(
        "ck_inter_store_transfer_lines_received_bounds",
        "inter_store_transfer_lines",
        type_="check",
    )
    op.create_check_constraint(
        "ck_inter_store_transfer_lines_received_bounds",
        "inter_store_transfer_lines",
        "received_quantity >= 0 AND received_quantity <= shipped_quantity",
    )
    op.drop_column("inter_store_transfer_lines", "declared_short_quantity")
    op.drop_column("inter_store_transfer_lines", "damaged_quantity")
