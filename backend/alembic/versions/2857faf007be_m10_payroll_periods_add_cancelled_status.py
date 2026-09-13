"""m10 payroll periods: add CANCELLED status

Revision ID: 2857faf007be
Revises: be26de9d9459
Create Date: 2026-09-13 06:38:26.170911

docs/M10_DESIGN.md Section 8's lifecycle diagram includes a `cancel`
transition (DRAFT/OPEN/CALCULATED/APPROVED -> CANCELLED, never from
POSTED) that the original M10 migration's `PAYROLL_PERIOD_STATUSES`
tuple omitted — discovered while building the Phase 4 lifecycle
service, before any real payroll data exists, so this widens the
existing CHECK constraint rather than being folded back into the
already-pushed migration (never rewrite a migration once it has run
against a real database - the same discipline this project applies to
git history).

No other constraint needs adjustment: `ck_payroll_periods_posting_consistency`
already requires `journal_entry_id IS NULL` for any non-POSTED status
(CANCELLED included), and `ck_payroll_periods_approval_consistency`
already permits (without requiring) `approved_by` to be set for any
non-POSTED, non-APPROVED status — a period cancelled after having been
approved keeps its approval audit trail, which is correct.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2857faf007be"
down_revision: Union[str, None] = "be26de9d9459"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_STATUSES = ("DRAFT", "OPEN", "CALCULATED", "APPROVED", "POSTED")
_NEW_STATUSES = _OLD_STATUSES + ("CANCELLED",)


def upgrade() -> None:
    op.drop_constraint("ck_payroll_periods_status", "payroll_periods", type_="check")
    op.create_check_constraint(
        "ck_payroll_periods_status",
        "payroll_periods",
        "status IN ('" + "', '".join(_NEW_STATUSES) + "')",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM payroll_periods WHERE status = 'CANCELLED') THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more payroll_periods are CANCELLED — the M10 base "
        "schema cannot represent this status, and discarding it would silently turn a "
        "cancelled period into an invalid row.'; "
        "END IF; "
        "END $$;"
    )
    op.drop_constraint("ck_payroll_periods_status", "payroll_periods", type_="check")
    op.create_check_constraint(
        "ck_payroll_periods_status",
        "payroll_periods",
        "status IN ('" + "', '".join(_OLD_STATUSES) + "')",
    )
