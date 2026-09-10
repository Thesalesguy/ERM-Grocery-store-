"""Imports every ORM model so Alembic's autogenerate can see them.

This module is imported for its side effects only (by alembic/env.py). Add a
new module's models here as they're introduced.
"""

from app.db.base_class import Base  # noqa: F401
from app.modules.audit.models import AuditLog  # noqa: F401
from app.modules.auth.models import (  # noqa: F401
    Permission,
    Role,
    RolePermission,
    Store,
    User,
    UserRole,
)
