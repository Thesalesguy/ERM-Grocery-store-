"""m10 payroll periods: add calculation-consistency CHECK constraint

Revision ID: 1e832b76969e
Revises: 2857faf007be
Create Date: 2026-09-13 06:50:00.000000

The original M10 migration added `ck_payroll_periods_approval_consistency`
(APPROVED/POSTED requires `approved_by`/`approved_at`) and
`ck_payroll_periods_posting_consistency` (POSTED requires
`journal_entry_id`), but never a matching invariant for CALCULATED —
discovered while building Phase 4's `approve_payroll_period` (which
needs to trust that a CALCULATED period always has `calculated_at` set,
exactly the kind of invariant this milestone's own EXCLUDE-constraint
philosophy says belongs in the database, not an application-level
`assert`).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1e832b76969e"
down_revision: Union[str, None] = "2857faf007be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT_NAME = "ck_payroll_periods_calculation_consistency"
_CONSTRAINT_SQL = (
    "(status IN ('CALCULATED', 'APPROVED', 'POSTED') AND calculated_by IS NOT NULL "
    "AND calculated_at IS NOT NULL) OR (status NOT IN ('CALCULATED', 'APPROVED', 'POSTED'))"
)


def upgrade() -> None:
    op.create_check_constraint(_CONSTRAINT_NAME, "payroll_periods", _CONSTRAINT_SQL)


def downgrade() -> None:
    # No populated-data guard needed here (unlike every other M10 status/
    # data-widening migration): while this CHECK constraint is active, no
    # row violating it can exist by construction — Postgres enforces it on
    # every INSERT/UPDATE — so there is nothing a downgrade could silently
    # lose. Dropping it merely stops enforcing an invariant that no
    # existing row could ever have escaped in the first place.
    op.drop_constraint(_CONSTRAINT_NAME, "payroll_periods", type_="check")
