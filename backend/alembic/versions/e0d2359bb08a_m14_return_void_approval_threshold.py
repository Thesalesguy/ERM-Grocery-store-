"""m14 return/void approval threshold

Revision ID: e0d2359bb08a
Revises: 17fb9afe8d39
Create Date: 2026-09-22 00:00:00.000000

See docs/M14_DESIGN.md. Adds:

- `stores.return_approval_threshold_amount` — a nullable
  `NUMERIC(12, 2)` column on the existing `stores` table, following the
  exact precedent `attendance_day_boundary_hour` (M10) set for per-store
  configuration: a typed column on `Store`, never a generic settings
  table (no `store_settings` table exists anywhere in this repository —
  `TECHNICAL_BLUEPRINT.md` Section C.7's sketch of one was never built).
  NULL (left NULL by this migration, on purpose — no seeded default) means
  the M14 approval gate is inactive for that store; every return/void
  behaves exactly as it did before M14 until an operator explicitly sets
  a threshold. No business evidence exists in this repository for what a
  default dollar amount should be, so none is invented here.
- `sale_returns.approval_required` — `BOOLEAN NOT NULL DEFAULT false`.
  Freezes, at creation time, whether THIS return/void was subject to its
  store's threshold (mirrors every other frozen financial fact on this
  row — BR-2). `DEFAULT false` backfills every pre-M14 row correctly:
  none of them were ever subject to a threshold, since the gate did not
  exist.
- One new permission, `sales.return.approve` (docs/M14_DESIGN.md
  "Authorization model"), granted to Admin and Manager only — mirrors
  this migration's own precedent (36ec624cf083's `_NEW_PERMISSIONS`/
  `_ROLE_GRANTS` pattern) for adding RBAC incrementally after the M2 seed
  migration has already run.

No changes to any M0-M13 table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0d2359bb08a"
down_revision: Union[str, None] = "17fb9afe8d39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERMISSIONS = [
    (
        "sales.return.approve",
        "Approve another user's return/void once its amount reaches the store's "
        "configured approval threshold",
    ),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
}


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("return_approval_threshold_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_stores_return_approval_threshold_non_negative",
        "stores",
        "return_approval_threshold_amount IS NULL OR return_approval_threshold_amount >= 0",
    )
    op.add_column(
        "sale_returns",
        sa.Column(
            "approval_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("sale_returns", "approval_required", server_default=None)

    conn = op.get_bind()

    # ON CONFLICT DO UPDATE ... RETURNING (mirrors every incremental
    # permission migration since M4, e.g. 8df037a45976/36ec624cf083):
    # e6180fca2ee0 (the M2 seed migration) reads app.modules.auth.
    # permissions.ALL_PERMISSIONS/ROLE_PERMISSIONS LIVE at migration-run
    # time, not a frozen historical snapshot. On a fresh database (a full
    # downgrade-to-base-then-upgrade cycle, e.g. tests/test_migrations.py),
    # replaying e6180fca2ee0 with today's permissions.py -- which now
    # includes sales.return.approve -- already seeds this row before this
    # migration runs. On an existing database that ran e6180fca2ee0
    # before M14 existed, this row is genuinely new here. The upsert
    # makes this migration correct in both cases without needing to know
    # which one it's running against. A bare INSERT (which this migration
    # shipped with initially) fails with a UniqueViolation on the
    # fresh-database path -- caught by tests/test_migrations.py's own
    # full-cycle test, not shipped unnoticed.
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
        name: id_
        for name, id_ in conn.execute(
            sa.text("SELECT name, id FROM roles WHERE name = ANY(:names)"),
            {"names": list(_ROLE_GRANTS)},
        ).all()
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

    # Guard FIRST, before any destructive step (same discipline as
    # 36ec624cf083's own downgrade): real M14 data the M13 schema cannot
    # represent must refuse the downgrade loudly, not silently destroy
    # approval history or a store's live threshold configuration.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM stores WHERE return_approval_threshold_amount IS NOT NULL) "
        "THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more stores have a configured return_approval_threshold_amount "
        "-- dropping this column would silently discard real store configuration.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM sale_returns WHERE approval_required IS TRUE) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more sale_returns were created under the M14 approval gate "
        "(approval_required = true) -- dropping this column would silently destroy that "
        "real authorization history.'; "
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

    op.drop_column("sale_returns", "approval_required")
    op.drop_constraint(
        "ck_stores_return_approval_threshold_non_negative", "stores", type_="check"
    )
    op.drop_column("stores", "return_approval_threshold_amount")
