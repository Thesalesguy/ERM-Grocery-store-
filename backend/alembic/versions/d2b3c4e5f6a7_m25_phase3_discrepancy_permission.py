"""m25 phase3 discrepancy investigation permission

Revision ID: d2b3c4e5f6a7
Revises: c1a2b3d4e5f6
Create Date: 2026-10-04 12:05:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Section 10.3. Adds one new permission
(`inventory.transfer.discrepancy.investigate`) -- the receiving-
discrepancy ledger added by c1a2b3d4e5f6 had no RBAC surface before this.
Granted to Admin (implicitly via `list(ALL_PERMISSIONS)` on the Python
side; explicitly here since the DB seed is the actual per-request
authorization source of truth -- app.modules.auth.service.
get_user_permissions reads role_permissions, not the Python
ROLE_PERMISSIONS dict), Manager and Inventory Clerk (the same tier that
already holds inventory.transfer.write -- the contract's own stated
"investigation/correction sits at the same or a slightly heavier tier
than the routine write permission" pattern). Mirrors 9c4c5a209aa9's own
`_NEW_PERMISSIONS`/`_ROLE_GRANTS`/`ON CONFLICT DO UPDATE ... RETURNING`
pattern exactly.

No changes to any other table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2b3c4e5f6a7"
down_revision: Union[str, None] = "c1a2b3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERMISSIONS = [
    (
        "inventory.transfer.discrepancy.investigate",
        "Claim a recorded receiving discrepancy for investigation and record its resolution",
    ),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
    "Inventory Clerk": [code for code, _ in _NEW_PERMISSIONS],
}


def upgrade() -> None:
    conn = op.get_bind()

    permission_ids: dict[str, int] = {}
    for perm_code, description in _NEW_PERMISSIONS:
        permission_ids[perm_code] = conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description, created_at) "
                "VALUES (:code, :description, now()) "
                "ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code "
                "RETURNING id"
            ),
            {"code": perm_code, "description": description},
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
        for perm_code in codes:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id) "
                    "ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"role_id": role_id, "permission_id": permission_ids[perm_code]},
            )


def downgrade() -> None:
    conn = op.get_bind()
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
