"""m4 hardening: allow MANUAL journal source type

Revision ID: 581d2a07f38c
Revises: 8df037a45976
Create Date: 2026-09-11 15:46:49.402517

See docs/M4_HARDENING_AUDIT.md Section 1. Widens
`ck_journal_entries_source_type` to also allow `'MANUAL'` — the one
source_type value application code does not post automatically, added
so `accounting.service.reverse_journal_entry`'s new automated-source
block (docs/M4_HARDENING_AUDIT.md: reversing a SALE/PURCHASE_RECEIPT/
PURCHASE_RETURN/STOCK_ADJUSTMENT/SALE_RETURN journal is now refused,
since none of them have a corresponding operational undo, and an
accounting-only reversal of one silently diverges the operational and
accounting ledgers) has a legitimate target to permit. No endpoint
creates a MANUAL entry in this migration or milestone — this only
widens what the CHECK constraint accepts, so the reversal code path
stays exercised by a real (if not yet API-reachable) entry type instead
of becoming untestable dead code.

A plain DROP/ADD CONSTRAINT is safe here — this widens an existing CHECK
constraint (allows one more value), it doesn't need to validate any
existing row against a new, stricter rule.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "581d2a07f38c"
down_revision: Union[str, None] = "8df037a45976"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + ("MANUAL",)


def upgrade() -> None:
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )


def downgrade() -> None:
    # Any MANUAL rows would violate the narrower constraint being
    # restored — there should never be any (no endpoint creates them),
    # but fail loudly rather than silently locking out a downgrade that
    # would otherwise succeed against corrupted data.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_entries WHERE source_type = 'MANUAL') THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_entries has MANUAL rows that would violate the "
        "narrower ck_journal_entries_source_type constraint'; "
        "END IF; "
        "END $$;"
    )
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )
