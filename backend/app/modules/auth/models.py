"""ORM models for stores, users, roles, permissions, and refresh tokens.

See docs/TECHNICAL_BLUEPRINT.md Section C.1 for the authoritative design
(columns, constraints, relationships). Authentication logic (hashing,
JWT, login/refresh/logout) lives in app.core.security and
app.modules.auth.service — see the module docstring in __init__.py.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin


class Store(TimestampMixin, Base):
    __tablename__ = "stores"
    __table_args__ = (
        CheckConstraint(
            "attendance_day_boundary_hour >= 0 AND attendance_day_boundary_hour <= 23",
            name="ck_stores_attendance_day_boundary_hour",
        ),
        CheckConstraint(
            "return_approval_threshold_amount IS NULL OR return_approval_threshold_amount >= 0",
            name="ck_stores_return_approval_threshold_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # M10 (docs/M10_DESIGN.md Section 7, approved decision #6): the
    # cross-midnight attendance work_date rule is store configuration,
    # never an employee field. 0 (default) means work_date always equals
    # the calendar date of clock_in_at converted to this store's own
    # `timezone` — no shifting. A store that runs shifts past midnight
    # (e.g. a 22:00-06:00 overnight shift) can set this to the local hour
    # before which a clock-in still belongs to the PREVIOUS calendar
    # date — e.g. 6 means a clock-in at 01:00 local time is attributed to
    # yesterday's work_date, not today's. Always computed from
    # `clock_in_at` (a timestamptz, UTC-stored per the project's
    # established convention) converted into this store's own
    # `timezone` — never the server's local clock.
    attendance_day_boundary_hour: Mapped[int] = mapped_column(nullable=False, default=0)

    # M14 (docs/M14_DESIGN.md): per-store return/void approval threshold,
    # following the exact same precedent as attendance_day_boundary_hour
    # above -- store configuration lives as a typed column on Store, never
    # a generic settings table (no such table exists in this repository;
    # TECHNICAL_BLUEPRINT.md's Section C.7 `store_settings` sketch was
    # never actually built). NULL (the default) means no threshold is
    # configured for this store -- the M14 approval gate is inactive and
    # every return/void behaves exactly as it did before M14. This is
    # deliberately NOT a seeded dollar amount: no business evidence exists
    # in this repository for what that amount should be, so production
    # must explicitly set one (Numeric(12, 2), same precision as every
    # other stored money amount in this system).
    return_approval_threshold_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # M20 (docs/M20_DESIGN.md Section 1.3): legal/business identity, for a
    # future fiscal document's seller-identity fields. Both NULL by
    # default on every store, including every pre-M20 one -- nothing
    # reads or requires either unless that store's FiscalConfig.is_enabled
    # is true (never true out of the box; M20_DISCOVERY.md Section 1 found
    # no confirmed tax jurisdiction, so this stays inert everywhere until
    # one is). `legal_name` falls back to `name` for display when unset;
    # `tax_registration_number` is an opaque string -- its format is
    # authority-specific and unknown today, so it is not validated beyond
    # "some string, if the operator has one to enter."
    legal_name: Mapped[str | None] = mapped_column(String(255))
    tax_registration_number: Mapped[str | None] = mapped_column(String(100))

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
