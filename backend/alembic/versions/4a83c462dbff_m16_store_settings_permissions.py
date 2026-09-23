"""m16 store settings permissions

Revision ID: 4a83c462dbff
Revises: e1a681c4aba3
Create Date: 2026-09-22 00:00:00.000001

See docs/M16_DESIGN.md "Settings screen". Adds two new permissions
(`store.settings.read`, `store.settings.write`) -- return_approval_
threshold_amount (M14) and attendance_day_boundary_hour (M10) existed
only as Store columns with no API surface to view or change them before
this milestone. Granted to Admin (implicitly, via `list(ALL_PERMISSIONS)`),
Manager (both -- mirrors Manager's broad day-to-day operational-write
access elsewhere), and Auditor (read only -- mirrors Auditor's universal
read-only pattern) -- mirrors e1a681c4aba3's own
`_NEW_PERMISSIONS`/`_ROLE_GRANTS`/`ON CONFLICT DO UPDATE ... RETURNING`
pattern.

No changes to any other table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a83c462dbff"
down_revision: Union[str, None] = "e1a681c4aba3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERMISSIONS = [
    ("store.settings.read", "View a store's configuration (return threshold, attendance rules)"),
    ("store.settings.write", "Change a store's configuration"),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
    "Auditor": ["store.settings.read"],
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
