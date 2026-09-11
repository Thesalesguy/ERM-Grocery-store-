"""ORM models for stores, users, roles, permissions, and refresh tokens.

See docs/TECHNICAL_BLUEPRINT.md Section C.1 for the authoritative design
(columns, constraints, relationships). Authentication logic (hashing,
JWT, login/refresh/logout) lives in app.core.security and
app.modules.auth.service — see the module docstring in __init__.py.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin


class Store(TimestampMixin, Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    users: Mapped[list["User"]] = relationship(back_populates="store")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store | None] = relationship(back_populates="users")
    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="user")


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500))

    role_permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role")
    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="role")


class Permission(TimestampMixin, Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500))

    role_permissions: Mapped[list["RolePermission"]] = relationship(back_populates="permission")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="role_permissions")
    permission: Mapped[Permission] = relationship(back_populates="role_permissions")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)

    user: Mapped[User] = relationship(back_populates="user_roles")
    role: Mapped[Role] = relationship(back_populates="user_roles")


class RefreshToken(TimestampMixin, Base):
    """Server-side half of the auth model (see app.core.security's module
    docstring for the JWT + refresh-token design decision). Only a SHA-256
    hash of the token is stored — never the raw value, matching how
    passwords are stored, so a database dump alone can't be replayed."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))
