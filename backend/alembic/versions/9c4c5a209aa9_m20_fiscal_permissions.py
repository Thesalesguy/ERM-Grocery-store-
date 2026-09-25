"""m20 fiscal permissions

Revision ID: 9c4c5a209aa9
Revises: c8337b04ca47
Create Date: 2026-09-25 12:30:00.000000

See docs/M20_DESIGN.md Section 8. Adds three new permissions
(`fiscal.read`, `fiscal.config.write`, `fiscal.retry`) -- the
fiscal-integration boundary added by c8337b04ca47 had no API/RBAC surface
before this. Granted to Admin (all three, implicitly via
`list(ALL_PERMISSIONS)` on the Python side; explicitly here since the DB
seed is the actual per-request authorization source of truth --
app.modules.auth.service.get_user_permissions reads role_permissions,
not the Python ROLE_PERMISSIONS dict), Manager (read + retry -- mirrors
accounting.read + accounting.reverse, NOT config.write, which is
Admin-only like accounting.admin), and Auditor (read only -- mirrors
Auditor's universal read-only pattern). Mirrors 4a83c462dbff's own
`_NEW_PERMISSIONS`/`_ROLE_GRANTS`/`ON CONFLICT DO UPDATE ... RETURNING`
pattern exactly.

No changes to any other table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c4c5a209aa9"
down_revision: Union[str, None] = "c8337b04ca47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERMISSIONS = [
    ("fiscal.read", "View a store's fiscal configuration and submission history"),
    (
        "fiscal.config.write",
        "Enable/configure fiscal submission for a store (provider, credential "
        "reference, endpoint, retry policy)",
    ),
    ("fiscal.retry", "Manually retry one failed/exhausted fiscal submission"),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": ["fiscal.read", "fiscal.retry"],
    "Auditor": ["fiscal.read"],
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
