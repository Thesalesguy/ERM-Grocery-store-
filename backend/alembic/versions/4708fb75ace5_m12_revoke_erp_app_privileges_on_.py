"""m12 revoke erp_app privileges on alembic_version

Revision ID: 4708fb75ace5
Revises: 32e51bcda102
Create Date: 2026-09-14 06:24:06.671059

M12 Phase 8 (DB privilege audit): `alembic_version` inherited the broad
default grant every new table gets via `ALTER DEFAULT PRIVILEGES`
(m1_runtime_role_privileges_restrict_erp_) because nothing has ever
explicitly REVOKEd it -- unlike journal_entries/audit_logs/
inventory_movements/etc., which each got an explicit REVOKE in their
own owning migration. The running application (erp_app) never
legitimately reads or writes this table; only Alembic, running as the
schema-owning role (erp_user), ever touches it. Found live via a direct
privilege query against erp_dev during M12 Phase 0 baseline
verification, and confirmed as a real, exploitable gap by
tests/test_backup_restore.py's adversarial check (`DELETE FROM
alembic_version` as erp_app succeeded before this migration).

Purely a privilege change: no data is touched, no existing constraint
is altered. No downgrade guard is needed for the same reason as
32e51bcda102's own index migration -- nothing irreplaceable is created
here, so there is nothing a downgrade could lose. The downgrade DOES
restore the previous (broader) grant, for exact reversibility, even
though restoring a privilege gap is not itself something to actually
want operationally.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4708fb75ace5"
down_revision: str | None = "32e51bcda102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "erp_app"


def upgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON alembic_version FROM {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON alembic_version TO {_APP_ROLE}")
