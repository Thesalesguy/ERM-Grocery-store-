"""m12 grant erp_app select on alembic_version

Revision ID: 17fb9afe8d39
Revises: 4708fb75ace5
Create Date: 2026-09-14 07:06:47.247680

M12 Phase 11 (health checks): the new GET /health/migration endpoint
needs to read the current schema version out of `alembic_version` so it
can compare it against this deployed code's own migration head and
surface a 503 on a stale-app/mismatched-schema condition (M12 Phase 14
scenario I) instead of a confusing 500 on the first query that touches
a changed column.

4708fb75ace5 (M12 Phase 8) revoked ALL privileges on this table from
erp_app after an adversarial test proved it could DELETE/UPDATE it --
a genuine gap, since only Alembic (running as erp_user) should ever be
able to CHANGE the recorded schema version. Reading it for a health
check is a different, legitimate need with no write risk: this
migration grants back SELECT only, leaving INSERT/UPDATE/DELETE
revoked. The security property that actually mattered (the running
application cannot tamper with recorded migration state) is fully
preserved; tests/test_backup_restore.py's adversarial UPDATE/DELETE
checks continue to hold unchanged.

No downgrade guard needed for the same reason as 4708fb75ace5 itself --
a privilege-only change, nothing irreplaceable to lose.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "17fb9afe8d39"
down_revision: str | None = "4708fb75ace5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "erp_app"


def upgrade() -> None:
    op.execute(f"GRANT SELECT ON alembic_version TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON alembic_version FROM {_APP_ROLE}")
