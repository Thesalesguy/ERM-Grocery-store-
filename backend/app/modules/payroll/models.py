"""ORM models for payroll: configurable deduction rules, per-store payroll
periods, and the derived/posted payroll result (earnings, deductions, net
pay), plus the GL posting link and reversal record.

See docs/M10_DESIGN.md for the full design. Load-bearing decisions:

- PayrollPeriod is PER-STORE (approved decision #1): uniqueness is
  enforced on (store_id, period_start, period_end) so the same store can
  never have two periods covering the same dates, and a posted period's
  store association is permanent (store_id is never rewritten after
  creation). `payroll_run_id` is an optional grouping key so a UI/service
  can present "run payroll for every store this cycle" as one action
  without a separate company-wide PayrollPeriod row or a second
  posting/calculation code path — it is a label, not a transactional
  unit.
- No statutory tax formula is hardcoded anywhere in this module
  (approved decision #8). DeductionType/DeductionRate are generic,
  effective-dated, JSONB-parameterized configuration; the calculation
  engine (app.modules.payroll.service) interprets `calculation_method` +
  `parameters` generically. This is a configurable-deduction boundary,
  not a jurisdiction-specific payroll-tax implementation.
- Money is `Numeric(12, 2)` throughout (the app's standard 2dp currency
  precision — see accounting/models.py's own note about why the ledger
  itself uses a different, 6dp quantum; payroll amounts round-trip
  losslessly into that when posted).
- PayrollPeriod carries two independent, nullable, unique
  client_transaction_id columns (calculation and posting are separate
  idempotent operations, mirroring transfers.InterStoreTransfer's
  ship/receive split) plus the state-based idempotency fast path used
  throughout the app (a retried call on an already-CALCULATED/POSTED
  period returns the existing result rather than recomputing/reposting).
- PayrollReversal is a separate table, not a status flip on
  PayrollPeriod: a period's status stays POSTED forever (mirrors
  JournalEntry's own append-only convention) and "was this period
  reversed" is a derived fact — does a PayrollReversal row reference it?
  A unique constraint on payroll_period_id makes a period reversible
  exactly once.
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
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

# Generic, jurisdiction-agnostic calculation shapes a DeductionRate's
# `parameters` JSONB can express. FLAT_AMOUNT: {"amount": "50.00"}.
# PERCENT_OF_GROSS / PERCENT_OF_TAXABLE: {"percent": "5.00"}. BRACKETED:
# {"brackets": [{"upto": "1000.00", "percent": "0"}, ...]}. The engine
# validates/interprets these; this module only stores them.
DEDUCTION_CALCULATION_METHODS = (
    "FLAT_AMOUNT",
    "PERCENT_OF_GROSS",
    "PERCENT_OF_TAXABLE",
    "BRACKETED",
)
DEDUCTION_CATEGORIES = ("EMPLOYEE", "EMPLOYER_CONTRIBUTION")

# CANCELLED added by a follow-up migration (see alembic/versions/
# 2857faf007be_m10_payroll_periods_add_cancelled_status.py) — the design
# doc's lifecycle diagram (Section 8) always included a cancel
# transition (DRAFT/OPEN/CALCULATED/APPROVED -> CANCELLED, never from
# POSTED) that the original migration's CHECK constraint omitted.
PAYROLL_PERIOD_STATUSES = ("DRAFT", "OPEN", "CALCULATED", "APPROVED", "POSTED", "CANCELLED")
# Mutable-after-calculation only via the dedicated lifecycle transitions
# in app.modules.payroll.service (calculate/approve/post) — never a bare
# UPDATE. POSTED is terminal; reversal is a separate PayrollReversal row,
# never a status write back onto this table (see module docstring).
_LOCKED_PERIOD_STATUSES = ("APPROVED", "POSTED")

PAYROLL_EMPLOYEE_RESULT_STATUSES = ("DRAFT", "FINAL")

EARNING_TYPES = ("REGULAR", "OVERTIME", "BONUS", "ADJUSTMENT")


class DeductionType(TimestampMixin, Base):
    """Global reference data naming a kind of deduction/contribution
    (e.g. "Health Insurance", "Retirement Contribution") — never itself
    dated or rated; see DeductionRate for the effective-dated amount."""

    __tablename__ = "deduction_types"
    __table_args__ = (
        CheckConstraint(
            "category IN ('" + "', '".join(DEDUCTION_CATEGORIES) + "')",
            name="ck_deduction_types_category",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False, default="EMPLOYEE")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(500))


class DeductionRate(TimestampMixin, Base):
    """Effective-dated rate/formula for one DeductionType. Global (not
    per-store, not per-employee) for M10 — no requirement demonstrated a
    need for narrower scoping, matching the OvertimePolicy precedent."""

    __tablename__ = "deduction_rates"
    __table_args__ = (
        CheckConstraint(
            "calculation_method IN ('" + "', '".join(DEDUCTION_CALCULATION_METHODS) + "')",
            name="ck_deduction_rates_calculation_method",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_deduction_rates_valid_range",
        ),
        ExcludeConstraint(
            ("deduction_type_id", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="ex_deduction_rates_no_overlap",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deduction_type_id: Mapped[int] = mapped_column(ForeignKey("deduction_types.id"), nullable=False)
    calculation_method: Mapped[str] = mapped_column(String(30), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class PayrollPeriod(TimestampMixin, Base):
    """One store's payroll run for one date range. See module docstring
    for why this is per-store and what `payroll_run_id` is/is not."""

    __tablename__ = "payroll_periods"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "period_start", "period_end", name="uq_payroll_periods_store_range"
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(PAYROLL_PERIOD_STATUSES) + "')",
            name="ck_payroll_periods_status",
        ),
        CheckConstraint("period_end >= period_start", name="ck_payroll_periods_valid_range"),
        CheckConstraint("pay_date >= period_end", name="ck_payroll_periods_pay_date_after_period"),
        CheckConstraint(
            "(status IN ('APPROVED', 'POSTED') AND approved_by IS NOT NULL AND approved_at IS "
            "NOT NULL) OR (status NOT IN ('APPROVED', 'POSTED'))",
            name="ck_payroll_periods_approval_consistency",
        ),
        CheckConstraint(
            "(status = 'POSTED' AND journal_entry_id IS NOT NULL) OR "
            "(status <> 'POSTED' AND journal_entry_id IS NULL)",
            name="ck_payroll_periods_posting_consistency",
        ),
        # Added by a follow-up migration (alembic/versions/
        # 1e832b76969e_m10_payroll_periods_add_calculation_.py) —
        # discovered while building Phase 4's approve_payroll_period,
        # which must be able to trust that a CALCULATED period always
        # has calculated_at/calculated_by set.
        CheckConstraint(
            "(status IN ('CALCULATED', 'APPROVED', 'POSTED') AND calculated_by IS NOT NULL "
            "AND calculated_at IS NOT NULL) OR "
            "(status NOT IN ('CALCULATED', 'APPROVED', 'POSTED'))",
            name="ck_payroll_periods_calculation_consistency",
        ),
        Index("ix_payroll_periods_store_id", "store_id"),
        Index("ix_payroll_periods_status", "status"),
        Index("ix_payroll_periods_payroll_run_id", "payroll_run_id"),
        # M11 (docs/M11_DESIGN.md Section 11): payroll_cost_summary,
        # headcount_by_store, and the KPI dashboard all filter by store_id
        # AND status together (e.g. status='POSTED') -- a composite index
        # serves that combined filter better than the two single-column
        # indexes above can.
        Index("ix_payroll_periods_store_status", "store_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # Grouping label only (see module docstring) — never a join target for
    # a transactional invariant; NULL for a period run on its own.
    payroll_run_id: Mapped[str | None] = mapped_column(String(36))
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")

    calculation_client_transaction_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    calculated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    posting_client_transaction_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    total_gross: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_deductions: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_employer_contributions: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    total_net_pay: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)


class PayrollEmployeeResult(TimestampMixin, Base):
    """One employee's calculated pay within a PayrollPeriod. Snapshots
    compensation/assignment context at calculation time (docs/M10_DESIGN.md
    "Liability reconciliation") — later effective-dated changes to
    CompensationPeriod/EmploymentAssignment must never rewrite an already-
    calculated or posted result."""

    __tablename__ = "payroll_employee_results"
    __table_args__ = (
        UniqueConstraint(
            "payroll_period_id", "employee_id", name="uq_payroll_employee_results_period_employee"
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(PAYROLL_EMPLOYEE_RESULT_STATUSES) + "')",
            name="ck_payroll_employee_results_status",
        ),
        CheckConstraint("regular_hours >= 0", name="ck_payroll_employee_results_regular_hours"),
        CheckConstraint("overtime_hours >= 0", name="ck_payroll_employee_results_overtime_hours"),
        CheckConstraint("gross_pay >= 0", name="ck_payroll_employee_results_gross_pay"),
        CheckConstraint(
            "total_deductions >= 0", name="ck_payroll_employee_results_total_deductions"
        ),
        CheckConstraint("net_pay >= 0", name="ck_payroll_employee_results_net_pay"),
        CheckConstraint(
            "net_pay = gross_pay - total_deductions", name="ck_payroll_employee_results_net_math"
        ),
        Index("ix_payroll_employee_results_payroll_period_id", "payroll_period_id"),
        Index("ix_payroll_employee_results_employee_id", "employee_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")

    # Snapshot columns — the compensation/assignment state actually used
    # for this calculation, frozen at calculation time.
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    pay_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pay_rate: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    regular_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    overtime_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    gross_pay: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_deductions: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_employer_contributions: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    net_pay: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)


class PayrollEarningLine(TimestampMixin, Base):
    __tablename__ = "payroll_earning_lines"
    __table_args__ = (
        CheckConstraint(
            "earning_type IN ('" + "', '".join(EARNING_TYPES) + "')",
            name="ck_payroll_earning_lines_type",
        ),
        CheckConstraint("hours IS NULL OR hours >= 0", name="ck_payroll_earning_lines_hours"),
        CheckConstraint("amount >= 0", name="ck_payroll_earning_lines_amount"),
        Index("ix_payroll_earning_lines_payroll_employee_result_id", "payroll_employee_result_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_employee_result_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_employee_results.id"), nullable=False
    )
    earning_type: Mapped[str] = mapped_column(String(20), nullable=False)
    hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))


class PayrollDeductionLine(TimestampMixin, Base):
    """One deduction/contribution amount within a PayrollEmployeeResult.
    `is_employer_contribution` distinguishes an amount withheld from the
    employee's pay from an employer-paid contribution computed alongside
    it on the same line shape (docs/M10_DESIGN.md "Accounting integration")
    — only the former reduces net_pay; both post to the GL."""

    __tablename__ = "payroll_deduction_lines"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_payroll_deduction_lines_amount"),
        Index(
            "ix_payroll_deduction_lines_payroll_employee_result_id", "payroll_employee_result_id"
        ),
        Index("ix_payroll_deduction_lines_deduction_type_id", "deduction_type_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_employee_result_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_employee_results.id"), nullable=False
    )
    deduction_type_id: Mapped[int] = mapped_column(ForeignKey("deduction_types.id"), nullable=False)
    is_employer_contribution: Mapped[bool] = mapped_column(nullable=False, default=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))


class PayrollReversal(TimestampMixin, Base):
    """Records that a POSTED PayrollPeriod was reversed. See module
    docstring for why this is a separate append-only row rather than a
    status flip. `payroll.reverse` is the only permission that can create
    one — see app.modules.auth.permissions."""

    __tablename__ = "payroll_reversals"
    __table_args__ = (UniqueConstraint("payroll_period_id", name="uq_payroll_reversals_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    reversal_journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reversed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
