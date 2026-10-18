"""m25 phase4 write off approval rbac

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-18 12:05:00.000000

See docs/M24D_TECHNICAL_CONTRACT.md Section 10.3. Adds one new role
("Regional Manager" -- the cross-store-scoped approval-tier role the
contract names with no existing equivalent) and three new permissions
(`inventory.transfer.writeoff.initiate`, `.approve`, `.reverse`) that
a1b2c3d4e5f6's new tables had no RBAC surface for before this. Mirrors
be26de9d9459's (M10) own `_NEW_ROLES`/`_NEW_PERMISSIONS`/`_ROLE_GRANTS`/
`ON CONFLICT DO UPDATE ... RETURNING` pattern exactly.

Grants:
- Admin: all three (implicitly via `list(ALL_PERMISSIONS)` on the Python
  side; explicitly here since the DB seed is the actual per-request
  authorization source of truth).
- Manager: initiate + approve (day-to-day store operations tier --
  mirrors the contract's own illustrative low-tier-approval example).
- Regional Manager: approve only -- holds no day-to-day store-operations
  permissions of its own (see app.modules.auth.permissions.ALL_ROLES).
- Nobody but Admin gets .reverse (Central/System Admin-only correction
  authority, Section 10.4).

No changes to any other table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_ROLES = [
    (
        "Regional Manager",
        "Cross-store approval authority for higher-tier transfer write-offs; holds no "
        "day-to-day store-operations permissions of its own",
    ),
]

_NEW_PERMISSIONS = [
    (
        "inventory.transfer.writeoff.initiate",
        "Request an approval-gated write-off against an investigated transfer discrepancy",
    ),
    (
        "inventory.transfer.writeoff.approve",
        "Approve or deny a pending transfer write-off request under the configured approval "
        "policy",
    ),
    (
        "inventory.transfer.writeoff.reverse",
        "Reverse a posted transfer write-off with a compensating journal entry",
    ),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [
        "inventory.transfer.writeoff.initiate",
        "inventory.transfer.writeoff.approve",
    ],
    "Regional Manager": ["inventory.transfer.writeoff.approve"],
}


def upgrade() -> None:
    conn = op.get_bind()

    for role_name, description in _NEW_ROLES:
        conn.execute(
            sa.text(
                "INSERT INTO roles (name, description, created_at) "
                "VALUES (:name, :description, now()) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name"
            ),
            {"name": role_name, "description": description},
        )

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

    role_row = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": "Regional Manager"}
    ).scalar_one_or_none()
    if role_row is not None:
        assigned = conn.execute(
            sa.text("SELECT COUNT(*) FROM user_roles WHERE role_id = :role_id"),
            {"role_id": role_row},
        ).scalar_one()
        if assigned > 0:
            raise RuntimeError(
                f"Cannot downgrade: {assigned} user(s) are assigned the 'Regional Manager' "
                "role -- the pre-Phase-4 schema has no role for them"
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
    if role_row is not None:
        conn.execute(sa.text("DELETE FROM roles WHERE id = :role_id"), {"role_id": role_row})
