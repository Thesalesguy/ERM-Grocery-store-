"""ORM models for HR/workforce master data, effective-dated employment
history, and attendance. See docs/M10_DESIGN.md for the full design.

Load-bearing conventions established there and applied throughout:

- `Employee` carries only truly-immutable identity fields
  (`employee_number`, `legal_name`, `hire_date`). Anything that can
  legitimately change over an employee's tenure (status, store/
  department/position, compensation) is modeled as insert-only,
  effective-dated HISTORY on a separate table — never a mutable column
  on `Employee` itself — so historical payroll can never be rewritten by
  a later master-data edit (M10_DESIGN.md Section 2's data taxonomy).
- Every effective-dated table therefore has NO `is_active`/`status`-style
  "current value" column: "what is true right now" is always a query
  (`effective_to IS NULL`, i.e. the open-ended row), never a second,
  independently-maintained flag that could drift from the history.
- Non-overlap for effective-dated periods and attendance shifts is
  enforced by a PostgreSQL EXCLUDE (GiST) constraint requiring the
  `btree_gist` extension (enabled by this module's migration) — the
  invariant lives in the database, not only in application code,
  matching the M9 hardening pass's own lesson that a service-layer
  check alone is not sufficient defense against a genuine race.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

EMPLOYMENT_STATUSES = ("ACTIVE", "ON_LEAVE", "SUSPENDED", "TERMINATED")
PAY_TYPES = ("HOURLY", "SALARY")
PAY_FREQUENCIES = ("WEEKLY", "BIWEEKLY", "SEMIMONTHLY", "MONTHLY")
ATTENDANCE_SOURCES = ("CLOCK", "MANUAL")
ATTENDANCE_STATUSES = ("OPEN", "CLOSED", "VOIDED")


class Department(TimestampMixin, Base):
    """Global reference data (like `ProductCategory`) — not store-scoped;
    a chain-wide department (e.g. "Produce", "Bakery") can have employees
    at multiple stores."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class Position(TimestampMixin, Base):
    """Global reference data. `department_id` is nullable — a position
    (e.g. "Store Manager") need not belong to a single department."""

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_department_id", "department_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class Employee(TimestampMixin, Base):
    """The one row that IS a person, for their whole tenure — see this
    module's own docstring for why only truly-immutable fields live
    here. `user_id` is an OPTIONAL, nullable, unique one-to-one link to
    `app.modules.auth.models.User` (M10_DESIGN.md Section 1): not every
    paid employee needs system access, and not every system account is a
    paid employee, so this is a plain nullable FK — never a shared
    primary key, never a required relationship."""

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("employee_number", name="uq_employees_employee_number"),
        UniqueConstraint("user_id", name="uq_employees_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_number: Mapped[str] = mapped_column(String(32), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Nullable — not every employee has (or needs) a login. See module
    # docstring. A unique constraint means at most one Employee may link
    # to a given User (Postgres allows multiple NULLs under a plain
    # UNIQUE constraint, so employees with no login never collide).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class EmploymentStatusPeriod(TimestampMixin, Base):
    """Effective-dated employment status history — hire/leave/suspend/
    terminate/rehire are all represented as a new row here, never an
    UPDATE of a prior row's `status` (M10_DESIGN.md Section 4's state
    machine). `effective_to IS NULL` means "currently in effect."

    The EXCLUDE constraint below is the DB-level non-overlap guarantee:
    no two periods for the SAME employee may cover the same date,
    represented as a closed-open range comparison via `daterange(...,
    '[]')` (inclusive on both ends, matching how `effective_to` itself
    is inclusive of its own last day)."""

    __tablename__ = "employment_status_periods"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(EMPLOYMENT_STATUSES) + "')",
            name="ck_employment_status_periods_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employment_status_periods_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_employment_status_periods_no_overlap",
        ),
        Index("ix_employment_status_periods_employee_id", "employee_id"),
        Index("ix_employment_status_periods_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class EmploymentAssignment(TimestampMixin, Base):
    """Effective-dated store/department/position/manager assignment.
    Store transfers, department changes, and position changes are all
    represented as a new row here (M10_DESIGN.md Section 5's boundary-
    date example: Store A through June 30, Store B from July 1)."""

    __tablename__ = "employment_assignments"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employment_assignments_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_employment_assignments_no_overlap",
        ),
        Index("ix_employment_assignments_employee_id", "employee_id"),
        Index("ix_employment_assignments_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    manager_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class CompensationPeriod(TimestampMixin, Base):
    """Effective-dated compensation history. A rate change is always a
    NEW row (M10_DESIGN.md Section 6's $10/hour-then-$12/hour example) —
    `PayrollEarningLine.rate_used` snapshots the value actually applied,
    so a later change here can never retroactively alter a past
    payslip, even though this row itself never changes once created."""

    __tablename__ = "compensation_periods"
    __table_args__ = (
        CheckConstraint(
            "pay_type IN ('" + "', '".join(PAY_TYPES) + "')",
            name="ck_compensation_periods_pay_type",
        ),
        CheckConstraint(
            "pay_frequency IN ('" + "', '".join(PAY_FREQUENCIES) + "')",
            name="ck_compensation_periods_pay_frequency",
        ),
        CheckConstraint("rate >= 0", name="ck_compensation_periods_rate_non_negative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_compensation_periods_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_compensation_periods_no_overlap",
        ),
        Index("ix_compensation_periods_employee_id", "employee_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    pay_type: Mapped[str] = mapped_column(String(10), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    pay_frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    overtime_eligible: Mapped[bool] = mapped_column(nullable=False, default=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class OvertimePolicy(TimestampMixin, Base):
    """Global (not per-store — M10 approved decision #7), effective-dated
    overtime configuration. No legal overtime rate is hardcoded anywhere
    in the calculation engine; every rule lives here."""

    __tablename__ = "overtime_policies"
    __table_args__ = (
        CheckConstraint(
            "threshold_hours_per_period > 0",
            name="ck_overtime_policies_threshold_positive",
        ),
        CheckConstraint("multiplier >= 1", name="ck_overtime_policies_multiplier_valid"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_overtime_policies_valid_range",
        ),
        ExcludeConstraint(
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_overtime_policies_no_overlap",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    threshold_hours_per_period: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.5")
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)


class AttendanceRecord(TimestampMixin, Base):
    """A single clock-in/clock-out shift (or a manual entry standing in
    for one). A correction is a NEW row referencing the record it
    corrects via `correction_of_id` — the original is marked VOIDED,
    never edited or deleted (mirrors M8's stock-count recount pattern:
    both the original and the correction remain permanently visible).

    The EXCLUDE constraint's range uses `clock_out_at` directly (NULL
    means "still open," which Postgres's `tstzrange` treats as an
    unbounded upper end) — an `OPEN` shift therefore correctly excludes
    any new shift for the same employee starting before it closes.
    VOIDED records are excluded from the constraint via the partial
    `WHERE` clause, since a corrected-away record must never block a
    legitimate new one from being created."""

    __tablename__ = "attendance_records"
    __table_args__ = (
        CheckConstraint(
            "source IN ('" + "', '".join(ATTENDANCE_SOURCES) + "')",
            name="ck_attendance_records_source",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(ATTENDANCE_STATUSES) + "')",
            name="ck_attendance_records_status",
        ),
        CheckConstraint(
            "clock_out_at IS NULL OR clock_out_at > clock_in_at",
            name="ck_attendance_records_valid_range",
        ),
        CheckConstraint(
            "(correction_of_id IS NULL) OR (correction_reason IS NOT NULL)",
            name="ck_attendance_records_correction_needs_reason",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (text("tstzrange(clock_in_at, clock_out_at, '[)')"), "&&"),
            using="gist",
            name="excl_attendance_records_no_overlap",
            where=text("status <> 'VOIDED'"),
        ),
        Index("ix_attendance_records_employee_work_date", "employee_id", "work_date"),
        Index("ix_attendance_records_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # The shift's LOGICAL business date — never inferred from
    # clock_out_at's calendar date (M10_DESIGN.md Section 7's
    # cross-midnight rule; the actual boundary policy is resolved by
    # app.modules.hr.service against the store's own configuration, see
    # that module for the deterministic algorithm).
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    clock_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    correction_of_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_records.id"))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
