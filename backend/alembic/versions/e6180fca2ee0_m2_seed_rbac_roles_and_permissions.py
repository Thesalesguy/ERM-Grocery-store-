"""m2 seed rbac roles and permissions

Revision ID: e6180fca2ee0
Revises: d29dbe67b5b1
Create Date: 2026-09-11 06:48:30.000000

Seeds `roles`, `permissions`, and `role_permissions` from
app.modules.auth.permissions.ROLE_PERMISSIONS — the single source of
truth also used by the `require_permission` dependency, so the seeded
data and the routes that check these permission codes can never drift
apart silently. See docs/M2_AUTH_AND_POS.md for the documented matrix.

This is reference data intrinsic to the application code itself (routes
reference these exact permission code strings), not per-deployment
business configuration — unlike, say, tax_rates in M1, which was
deliberately left unseeded. Seeding it here is the appropriate exception
to "migrations are schema-only".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.modules.auth.permissions import ALL_PERMISSIONS, ALL_ROLES, ROLE_PERMISSIONS

# revision identifiers, used by Alembic.
revision: str = "e6180fca2ee0"
down_revision: Union[str, None] = "d29dbe67b5b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    role_ids: dict[str, int] = {}
    for name, description in ALL_ROLES.items():
        role_ids[name] = conn.execute(
            sa.text(
                "INSERT INTO roles (name, description, created_at) "
                "VALUES (:name, :description, now()) RETURNING id"
            ),
            {"name": name, "description": description},
        ).scalar_one()

    permission_ids: dict[str, int] = {}
    for code, description in ALL_PERMISSIONS.items():
        permission_ids[code] = conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description, created_at) "
                "VALUES (:code, :description, now()) RETURNING id"
            ),
            {"code": code, "description": description},
        ).scalar_one()

    for role_name, codes in ROLE_PERMISSIONS.items():
        role_id = role_ids[role_name]
        for code in codes:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id)"
                ),
                {"role_id": role_id, "permission_id": permission_ids[code]},
            )


def downgrade() -> None:
    conn = op.get_bind()
    role_names = tuple(ALL_ROLES)
    permission_codes = tuple(ALL_PERMISSIONS)

    # A downgrade must not assume an empty database: any user assigned one
    # of these roles (e.g. via scripts/create_admin_user.py) leaves a
    # user_roles row that would otherwise block deleting from roles below.
    conn.execute(
        sa.text(
            "DELETE FROM user_roles WHERE role_id IN "
            "(SELECT id FROM roles WHERE name = ANY(:names))"
        ),
        {"names": list(role_names)},
    )
    conn.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE name = ANY(:names))"
        ),
        {"names": list(role_names)},
    )
    conn.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": list(permission_codes)},
    )
    conn.execute(
        sa.text("DELETE FROM roles WHERE name = ANY(:names)"),
        {"names": list(role_names)},
    )
