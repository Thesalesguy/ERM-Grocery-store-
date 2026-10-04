"""m25 phase1 accounting entity foundation

Revision ID: 75d685966c98
Revises: aa9ac6ad7476
Create Date: 2026-10-04 00:00:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3/4/23 for the full design.
Creates `accounting_entities` (one row per legal/financial grouping of
stores -- CORPORATE_DIVISION or INDEPENDENT), seeds exactly one default
CORPORATE_DIVISION row, adds `stores.accounting_entity_id` (backfilled to
that default row for every existing store, then made NOT NULL).

This migration is purely additive and changes no existing accounting
behavior: nothing in this milestone reads `entity_type`, and every store
in a from-scratch or existing production database lands on the same
single entity, so every existing store-scoped posting, report, and
reconciliation produces byte-identical results before and after this
migration.

The downgrade is guarded (mirroring the M7/M8/M9/M10 precedents in this
same migration chain): it refuses if more than one `accounting_entities`
row exists, or if any store was ever reassigned off the default entity --
neither is possible yet (this phase ships no API to do either), so the
guard exists only to protect a LATER M25 phase's data from an
out-of-order downgrade, never this phase's own upgrade path.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "75d685966c98"
down_revision: Union[str, None] = "aa9ac6ad7476"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_ENTITY_NAME = "Default Entity"


def upgrade() -> None:
    op.create_table(
        "accounting_entities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "entity_type",
            sa.String(length=20),
            nullable=False,
            server_default="CORPORATE_DIVISION",
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "entity_type IN ('CORPORATE_DIVISION', 'INDEPENDENT')",
            name="ck_accounting_entities_entity_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_accounting_entities_name"),
    )
    op.create_index(
        "uq_accounting_entities_one_default",
        "accounting_entities",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.execute(
        f"""
        INSERT INTO accounting_entities (name, entity_type, is_default, created_at)
        VALUES ('{_DEFAULT_ENTITY_NAME}', 'CORPORATE_DIVISION', true, now())
        """
    )

    op.add_column("stores", sa.Column("accounting_entity_id", sa.Integer(), nullable=True))
    op.execute(
        f"""
        UPDATE stores
        SET accounting_entity_id = (
            SELECT id FROM accounting_entities WHERE name = '{_DEFAULT_ENTITY_NAME}'
        )
        WHERE accounting_entity_id IS NULL
        """
    )
    op.alter_column("stores", "accounting_entity_id", nullable=False)
    op.create_foreign_key(
        "fk_stores_accounting_entity_id_accounting_entities",
        "stores",
        "accounting_entities",
        ["accounting_entity_id"],
        ["id"],
    )
    op.create_index("ix_stores_accounting_entity_id", "stores", ["accounting_entity_id"])


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            entity_count integer;
            reassigned_count integer;
        BEGIN
            SELECT COUNT(*) INTO entity_count FROM accounting_entities;
            IF entity_count > 1 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || entity_count::text || ' accounting_entities '
                    'rows exist (expected exactly 1, the default seeded by this '
                    'migration) -- a later phase already created additional entities; '
                    'downgrade that phase first';
            END IF;

            SELECT COUNT(*) INTO reassigned_count
            FROM stores s
            JOIN accounting_entities e ON e.id = s.accounting_entity_id
            WHERE e.is_default IS NOT TRUE;
            IF reassigned_count > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade: ' || reassigned_count::text || ' store(s) are '
                    'assigned to a non-default accounting entity -- this migration '
                    'cannot represent that assignment';
            END IF;
        END
        $$;
        """
    )

    op.drop_index("ix_stores_accounting_entity_id", table_name="stores")
    op.drop_constraint(
        "fk_stores_accounting_entity_id_accounting_entities", "stores", type_="foreignkey"
    )
    op.drop_column("stores", "accounting_entity_id")
    op.drop_index("uq_accounting_entities_one_default", table_name="accounting_entities")
    op.drop_table("accounting_entities")
