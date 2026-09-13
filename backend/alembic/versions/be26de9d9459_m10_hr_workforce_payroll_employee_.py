"""m10 hr/workforce and payroll: employee master data, effective-dated
employment history, attendance, and per-store payroll periods

Revision ID: be26de9d9459
Revises: 36ec624cf083
Create Date: 2026-09-13 06:03:02.096240

See docs/M10_DESIGN.md. Adds, in dependency order:

- `departments` / `positions` — global HR reference data (mirrors
  `product_categories`/`tax_rates`: not store-scoped).
- `employees` — the one row that IS a person for their whole tenure;
  deliberately carries only immutable identity fields. `user_id` is an
  OPTIONAL, nullable, unique FK to `users.id` (M10_DESIGN.md Section 1:
  Employee and User are separate concepts — not every paid employee
  needs system access, and not every system account is a paid
  employee).
- `employment_status_periods` / `employment_assignments` /
  `compensation_periods` — effective-dated employment history, each
  non-overlapping per employee via a PostgreSQL EXCLUDE (GiST)
  constraint requiring the `btree_gist` extension (approved decision #4:
  use EXCLUDE constraints backed by `btree_gist`, verified available and
  installable by the migrations role in this environment; if that were
  ever not true, `CREATE EXTENSION` below fails loudly and the whole
  migration transaction rolls back — it can never silently omit the
  invariant).
- `overtime_policies` — GLOBAL (not per-store — approved decision #7),
  effective-dated, non-overlapping via the same EXCLUDE mechanism.
- `attendance_records` — one row per shift, non-overlapping per employee
  (VOIDED/corrected-away records excluded via a partial EXCLUDE
  constraint so a correction can supersede an original without
  colliding with it).
- `deduction_types` / `deduction_rates` — generic, effective-dated,
  JSONB-parameterized deduction/contribution configuration (approved
  decision #8: no statutory tax formula is hardcoded anywhere).
- `payroll_periods` — PER-STORE (approved decision #1), unique on
  (store_id, period_start, period_end); `payroll_run_id` is an optional
  grouping label, never a second transactional unit.
- `payroll_employee_results` / `payroll_earning_lines` /
  `payroll_deduction_lines` — the derived, calculated payroll result.
- `payroll_reversals` — records that a POSTED period was reversed
  (append-only, unique per period — a period can be reversed exactly
  once).

Also, as pure schema/seed-data widening of the EXISTING accounting core
(mirroring the M6/M7/M8/M9 precedent of widening `journal_entries`'
`source_type` CHECK and seeding new accounts in the same migration that
introduces the feature needing them — never split across two
migrations):

- `stores.attendance_day_boundary_hour` — the cross-midnight
  attendance work_date rule (approved decision #6: store configuration,
  never an employee field).
- Six new GL accounts (`accounting/constants.py`: Payroll Payable,
  Statutory Withholding Payable, Benefit/Other Deduction Payable,
  Employer Contribution Payable, Wage & Salary Expense, Employer
  Contribution Expense) and two new `journal_entries.source_type` values
  (`PAYROLL_POSTING`, `PAYROLL_REVERSAL`). No payroll posting FUNCTION
  is added here — `post_payroll_journal` is Phase 6 service-layer work;
  this migration only makes the schema capable of representing it.

Ten new permissions (`hr.read`/`hr.write`/`hr.compensation.write`,
`attendance.read`/`attendance.write`, `payroll.read`/`calculate`/
`approve`/`post`/`reverse`) and one new role (`HR Clerk`) are seeded per
`app.modules.auth.permissions` — see that module for the full RBAC
rationale, including why Manager is deliberately NOT granted
`payroll.post`/`payroll.reverse` (approved decision #2).

No changes to any M0-M9 table's own columns/constraints/data beyond the
one new nullable-with-default `stores` column and the two purely-additive
widenings (`accounts` gains new rows; `journal_entries`'
`source_type` CHECK gains two new allowed values) described above.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint

# revision identifiers, used by Alembic.
revision: str = "be26de9d9459"
down_revision: Union[str, None] = "36ec624cf083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EMPLOYMENT_STATUSES = ("ACTIVE", "ON_LEAVE", "SUSPENDED", "TERMINATED")
_PAY_TYPES = ("HOURLY", "SALARY")
_PAY_FREQUENCIES = ("WEEKLY", "BIWEEKLY", "SEMIMONTHLY", "MONTHLY")
_ATTENDANCE_SOURCES = ("CLOCK", "MANUAL")
_ATTENDANCE_STATUSES = ("OPEN", "CLOSED", "VOIDED")
_DEDUCTION_CALCULATION_METHODS = (
    "FLAT_AMOUNT",
    "PERCENT_OF_GROSS",
    "PERCENT_OF_TAXABLE",
    "BRACKETED",
)
_DEDUCTION_CATEGORIES = ("EMPLOYEE", "EMPLOYER_CONTRIBUTION")
_PAYROLL_PERIOD_STATUSES = ("DRAFT", "OPEN", "CALCULATED", "APPROVED", "POSTED")
_PAYROLL_EMPLOYEE_RESULT_STATUSES = ("DRAFT", "FINAL")
_EARNING_TYPES = ("REGULAR", "OVERTIME", "BONUS", "ADJUSTMENT")

_OLD_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
    "SUPPLIER_CREDIT_NOTE",
    "INTER_STORE_TRANSFER_SHIP",
    "INTER_STORE_TRANSFER_RECEIVE",
    "MANUAL",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + ("PAYROLL_POSTING", "PAYROLL_REVERSAL")

_APP_ROLE = "erp_app"
# payroll_reversals is genuinely append-only forever — a period is
# reversed exactly once, and that record is never edited or deleted
# (mirrors journal_entries/journal_lines' own M4 carve-out, and M1's for
# audit_logs/inventory_movements). Every OTHER new table needs ordinary
# UPDATE (effective-dated history closes out a prior row's
# `effective_to`; attendance_records flips `status` to VOIDED on
# correction; payroll_periods/payroll_employee_results carry a real
# lifecycle) so none of them get this carve-out.
_LEDGER_TABLES = ("payroll_reversals",)

_NEW_ACCOUNTS = [
    (
        "2200",
        "Payroll Payable",
        "LIABILITY",
        "CREDIT",
        "Net pay owed to employees for a POSTED payroll period, relieved when paid.",
    ),
    (
        "2210",
        "Statutory Withholding Payable",
        "LIABILITY",
        "CREDIT",
        "Amounts withheld from employee pay for future remittance to a statutory authority.",
    ),
    (
        "2220",
        "Benefit / Other Deduction Payable",
        "LIABILITY",
        "CREDIT",
        "Amounts withheld from employee pay for a non-statutory deduction (benefits, etc.).",
    ),
    (
        "2230",
        "Employer Contribution Payable",
        "LIABILITY",
        "CREDIT",
        "Employer-side contribution amounts owed, mirroring Employer Contribution Expense.",
    ),
    (
        "6000",
        "Wage & Salary Expense",
        "EXPENSE",
        "DEBIT",
        "Gross pay expense recognized when a payroll period is posted.",
    ),
    (
        "6100",
        "Employer Contribution Expense",
        "EXPENSE",
        "DEBIT",
        "Employer-side contribution expense recognized alongside gross pay.",
    ),
]

_NEW_PERMISSIONS = [
    ("hr.read", "View employee records, employment history, and assignments"),
    ("hr.write", "Create/edit employees, employment status, and store/role assignments"),
    ("hr.compensation.write", "Set or change an employee's compensation (pay rate, pay type)"),
    ("attendance.read", "View attendance records"),
    ("attendance.write", "Record and correct attendance (clock in/out, corrections)"),
    ("payroll.read", "View payroll periods and calculated results"),
    ("payroll.calculate", "Run/re-run payroll calculation for a period (no GL impact)"),
    ("payroll.approve", "Approve a calculated payroll period before posting"),
    ("payroll.post", "Post an approved payroll period, creating a real GL liability"),
    ("payroll.reverse", "Reverse a posted payroll period with a compensating entry"),
]

_NEW_ROLES = [
    (
        "HR Clerk",
        "Manages employee records, employment history, and attendance; prepares payroll "
        "for approval but cannot approve, post, or reverse it",
    ),
]

# See app.modules.auth.permissions for the full rationale (Manager
# deliberately excluded from payroll.post/payroll.reverse; HR Clerk
# scoped to exactly M10 design decision #3's list).
_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [
        "hr.read",
        "hr.write",
        "hr.compensation.write",
        "attendance.read",
        "attendance.write",
        "payroll.read",
        "payroll.calculate",
        "payroll.approve",
    ],
    "Auditor": ["hr.read", "attendance.read", "payroll.read"],
    "HR Clerk": [
        "hr.read",
        "hr.write",
        "attendance.read",
        "attendance.write",
        "payroll.read",
        "payroll.calculate",
    ],
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- btree_gist: required for every EXCLUDE constraint below -----------
    # Approved decision #4: use EXCLUDE constraints backed by btree_gist,
    # or STOP. Verified available/installable by the migrations role in
    # this environment; if that were ever untrue, this statement raises
    # loudly here and the whole migration transaction rolls back — the
    # invariant is never silently weakened or omitted.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # --- stores: cross-midnight attendance configuration --------------------
    op.add_column(
        "stores",
        sa.Column("attendance_day_boundary_hour", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_stores_attendance_day_boundary_hour",
        "stores",
        "attendance_day_boundary_hour >= 0 AND attendance_day_boundary_hour <= 23",
    )

    # --- departments / positions ---------------------------------------------
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_positions_department_id", "positions", ["department_id"])

    # --- employees -------------------------------------------------------------
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_number", sa.String(length=32), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_employees_employee_number", "employees", ["employee_number"])
    op.create_unique_constraint("uq_employees_user_id", "employees", ["user_id"])

    # --- employment_status_periods ----------------------------------------------
    op.create_table(
        "employment_status_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('" + "', '".join(_EMPLOYMENT_STATUSES) + "')",
            name="ck_employment_status_periods_status",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employment_status_periods_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_employment_status_periods_no_overlap",
        ),
    )
    op.create_index(
        "ix_employment_status_periods_employee_id", "employment_status_periods", ["employee_id"]
    )
    op.create_index("ix_employment_status_periods_status", "employment_status_periods", ["status"])

    # --- employment_assignments ------------------------------------------------
    op.create_table(
        "employment_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=True),
        sa.Column(
            "manager_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employment_assignments_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_employment_assignments_no_overlap",
        ),
    )
    op.create_index(
        "ix_employment_assignments_employee_id", "employment_assignments", ["employee_id"]
    )
    op.create_index("ix_employment_assignments_store_id", "employment_assignments", ["store_id"])

    # --- compensation_periods --------------------------------------------------
    op.create_table(
        "compensation_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("pay_type", sa.String(length=10), nullable=False),
        sa.Column("rate", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("pay_frequency", sa.String(length=20), nullable=False),
        sa.Column("overtime_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "pay_type IN ('" + "', '".join(_PAY_TYPES) + "')",
            name="ck_compensation_periods_pay_type",
        ),
        sa.CheckConstraint(
            "pay_frequency IN ('" + "', '".join(_PAY_FREQUENCIES) + "')",
            name="ck_compensation_periods_pay_frequency",
        ),
        sa.CheckConstraint("rate >= 0", name="ck_compensation_periods_rate_non_negative"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_compensation_periods_valid_range",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_compensation_periods_no_overlap",
        ),
    )
    op.create_index("ix_compensation_periods_employee_id", "compensation_periods", ["employee_id"])

    # --- overtime_policies (global, not per-store — decision #7) ------------------
    op.create_table(
        "overtime_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("threshold_hours_per_period", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column(
            "multiplier", sa.Numeric(precision=4, scale=2), nullable=False, server_default="1.5"
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "threshold_hours_per_period > 0", name="ck_overtime_policies_threshold_positive"
        ),
        sa.CheckConstraint("multiplier >= 1", name="ck_overtime_policies_multiplier_valid"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_overtime_policies_valid_range",
        ),
        ExcludeConstraint(
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="excl_overtime_policies_no_overlap",
        ),
    )

    # --- attendance_records --------------------------------------------------
    op.create_table(
        "attendance_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("clock_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="OPEN"),
        sa.Column(
            "correction_of_id", sa.Integer(), sa.ForeignKey("attendance_records.id"), nullable=True
        ),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        sa.Column("corrected_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('" + "', '".join(_ATTENDANCE_SOURCES) + "')",
            name="ck_attendance_records_source",
        ),
        sa.CheckConstraint(
            "status IN ('" + "', '".join(_ATTENDANCE_STATUSES) + "')",
            name="ck_attendance_records_status",
        ),
        sa.CheckConstraint(
            "clock_out_at IS NULL OR clock_out_at > clock_in_at",
            name="ck_attendance_records_valid_range",
        ),
        sa.CheckConstraint(
            "(correction_of_id IS NULL) OR (correction_reason IS NOT NULL)",
            name="ck_attendance_records_correction_needs_reason",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (sa.text("tstzrange(clock_in_at, clock_out_at, '[)')"), "&&"),
            using="gist",
            name="excl_attendance_records_no_overlap",
            where=sa.text("status <> 'VOIDED'"),
        ),
    )
    op.create_index(
        "ix_attendance_records_employee_work_date",
        "attendance_records",
        ["employee_id", "work_date"],
    )
    op.create_index("ix_attendance_records_store_id", "attendance_records", ["store_id"])

    # --- deduction_types / deduction_rates --------------------------------------
    op.create_table(
        "deduction_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False, server_default="EMPLOYEE"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('" + "', '".join(_DEDUCTION_CATEGORIES) + "')",
            name="ck_deduction_types_category",
        ),
    )
    op.create_table(
        "deduction_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "deduction_type_id", sa.Integer(), sa.ForeignKey("deduction_types.id"), nullable=False
        ),
        sa.Column("calculation_method", sa.String(length=30), nullable=False),
        sa.Column("parameters", JSONB(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "calculation_method IN ('" + "', '".join(_DEDUCTION_CALCULATION_METHODS) + "')",
            name="ck_deduction_rates_calculation_method",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_deduction_rates_valid_range",
        ),
        ExcludeConstraint(
            ("deduction_type_id", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="ex_deduction_rates_no_overlap",
        ),
    )

    # --- payroll_periods (per-store — approved decision #1) -----------------------
    op.create_table(
        "payroll_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("payroll_run_id", sa.String(length=36), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("calculation_client_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("calculated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posting_client_transaction_id", sa.String(length=100), nullable=True),
        sa.Column(
            "journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True
        ),
        sa.Column("posted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total_gross", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_deductions",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_employer_contributions",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_net_pay", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "store_id", "period_start", "period_end", name="uq_payroll_periods_store_range"
        ),
        sa.CheckConstraint(
            "status IN ('" + "', '".join(_PAYROLL_PERIOD_STATUSES) + "')",
            name="ck_payroll_periods_status",
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_payroll_periods_valid_range"),
        sa.CheckConstraint(
            "pay_date >= period_end", name="ck_payroll_periods_pay_date_after_period"
        ),
        sa.CheckConstraint(
            "(status IN ('APPROVED', 'POSTED') AND approved_by IS NOT NULL AND approved_at IS "
            "NOT NULL) OR (status NOT IN ('APPROVED', 'POSTED'))",
            name="ck_payroll_periods_approval_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'POSTED' AND journal_entry_id IS NOT NULL) OR "
            "(status <> 'POSTED' AND journal_entry_id IS NULL)",
            name="ck_payroll_periods_posting_consistency",
        ),
    )
    op.create_unique_constraint(
        "uq_payroll_periods_calculation_ctxn",
        "payroll_periods",
        ["calculation_client_transaction_id"],
    )
    op.create_unique_constraint(
        "uq_payroll_periods_posting_ctxn", "payroll_periods", ["posting_client_transaction_id"]
    )
    op.create_index("ix_payroll_periods_store_id", "payroll_periods", ["store_id"])
    op.create_index("ix_payroll_periods_status", "payroll_periods", ["status"])
    op.create_index("ix_payroll_periods_payroll_run_id", "payroll_periods", ["payroll_run_id"])

    # --- payroll_employee_results --------------------------------------------
    op.create_table(
        "payroll_employee_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payroll_period_id", sa.Integer(), sa.ForeignKey("payroll_periods.id"), nullable=False
        ),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=True),
        sa.Column("pay_type", sa.String(length=20), nullable=False),
        sa.Column("pay_rate", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column(
            "regular_hours", sa.Numeric(precision=8, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "overtime_hours", sa.Numeric(precision=8, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "gross_pay", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_deductions",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_employer_contributions",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_pay", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "payroll_period_id", "employee_id", name="uq_payroll_employee_results_period_employee"
        ),
        sa.CheckConstraint(
            "status IN ('" + "', '".join(_PAYROLL_EMPLOYEE_RESULT_STATUSES) + "')",
            name="ck_payroll_employee_results_status",
        ),
        sa.CheckConstraint("regular_hours >= 0", name="ck_payroll_employee_results_regular_hours"),
        sa.CheckConstraint(
            "overtime_hours >= 0", name="ck_payroll_employee_results_overtime_hours"
        ),
        sa.CheckConstraint("gross_pay >= 0", name="ck_payroll_employee_results_gross_pay"),
        sa.CheckConstraint(
            "total_deductions >= 0", name="ck_payroll_employee_results_total_deductions"
        ),
        sa.CheckConstraint("net_pay >= 0", name="ck_payroll_employee_results_net_pay"),
        sa.CheckConstraint(
            "net_pay = gross_pay - total_deductions", name="ck_payroll_employee_results_net_math"
        ),
    )
    op.create_index(
        "ix_payroll_employee_results_payroll_period_id",
        "payroll_employee_results",
        ["payroll_period_id"],
    )
    op.create_index(
        "ix_payroll_employee_results_employee_id", "payroll_employee_results", ["employee_id"]
    )

    # --- payroll_earning_lines / payroll_deduction_lines ------------------------
    op.create_table(
        "payroll_earning_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payroll_employee_result_id",
            sa.Integer(),
            sa.ForeignKey("payroll_employee_results.id"),
            nullable=False,
        ),
        sa.Column("earning_type", sa.String(length=20), nullable=False),
        sa.Column("hours", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("rate", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "earning_type IN ('" + "', '".join(_EARNING_TYPES) + "')",
            name="ck_payroll_earning_lines_type",
        ),
        sa.CheckConstraint("hours IS NULL OR hours >= 0", name="ck_payroll_earning_lines_hours"),
        sa.CheckConstraint("amount >= 0", name="ck_payroll_earning_lines_amount"),
    )
    op.create_index(
        "ix_payroll_earning_lines_payroll_employee_result_id",
        "payroll_earning_lines",
        ["payroll_employee_result_id"],
    )

    op.create_table(
        "payroll_deduction_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payroll_employee_result_id",
            sa.Integer(),
            sa.ForeignKey("payroll_employee_results.id"),
            nullable=False,
        ),
        sa.Column(
            "deduction_type_id", sa.Integer(), sa.ForeignKey("deduction_types.id"), nullable=False
        ),
        sa.Column(
            "is_employer_contribution", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount >= 0", name="ck_payroll_deduction_lines_amount"),
    )
    op.create_index(
        "ix_payroll_deduction_lines_payroll_employee_result_id",
        "payroll_deduction_lines",
        ["payroll_employee_result_id"],
    )
    op.create_index(
        "ix_payroll_deduction_lines_deduction_type_id",
        "payroll_deduction_lines",
        ["deduction_type_id"],
    )

    # --- payroll_reversals -----------------------------------------------------
    op.create_table(
        "payroll_reversals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payroll_period_id", sa.Integer(), sa.ForeignKey("payroll_periods.id"), nullable=False
        ),
        sa.Column(
            "reversal_journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entries.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reversed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("payroll_period_id", name="uq_payroll_reversals_period"),
    )

    # --- append-only privilege carve-out (M1/M4 pattern) ------------------------
    for table in _LEDGER_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                    EXECUTE 'REVOKE UPDATE, DELETE ON {table} FROM {_APP_ROLE}';
                END IF;
            END
            $$;
            """)

    # --- widen the journal source_type CHECK ---------------------------------
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )

    # --- seed the six new payroll GL accounts -----------------------------------
    for code, name, account_type, normal_balance, description in _NEW_ACCOUNTS:
        conn.execute(
            sa.text(
                "INSERT INTO accounts "
                "(code, name, account_type, normal_balance, is_system, is_active, description, "
                " created_at) "
                "VALUES (:code, :name, :account_type, :normal_balance, true, true, :description, "
                " now()) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "code": code,
                "name": name,
                "account_type": account_type,
                "normal_balance": normal_balance,
                "description": description,
            },
        )

    # --- seed the new role, new permissions, and role grants --------------------
    role_ids: dict[str, int] = {}
    for name, description in _NEW_ROLES:
        role_ids[name] = conn.execute(
            sa.text(
                "INSERT INTO roles (name, description, created_at) "
                "VALUES (:name, :description, now()) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"name": name, "description": description},
        ).scalar_one()

    permission_ids: dict[str, int] = {}
    for code, description in _NEW_PERMISSIONS:
        permission_ids[code] = conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description, created_at) "
                "VALUES (:code, :description, now()) "
                "ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code "
                "RETURNING id"
            ),
            {"code": code, "description": description},
        ).scalar_one()

    existing_role_ids: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            sa.text("SELECT name, id FROM roles WHERE name = ANY(:names)"),
            {"names": list(_ROLE_GRANTS)},
        )
    }
    for role_name, codes in _ROLE_GRANTS.items():
        role_id = existing_role_ids.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id) "
                    "ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"role_id": role_id, "permission_id": permission_ids[code]},
            )


def downgrade() -> None:
    conn = op.get_bind()

    # --- Guard FIRST, before any destructive step (same discipline as
    # every prior milestone's downgrade). Any real HR/payroll data is
    # unconditionally irreplaceable (there is no other representation of
    # an employee, a shift, a compensation record, or a payroll run
    # anywhere else in the schema) — mirrors M9's supplier_products
    # guard exactly. A payroll_period still in DRAFT (no calculation has
    # happened, nothing has been decided) is the one exception, mirroring
    # M9's own RECOMMENDED-status replenishment_plans exception.
    for table, label in [
        ("employees", "employee records"),
        ("employment_status_periods", "employment status history"),
        ("employment_assignments", "employment assignment history"),
        ("compensation_periods", "compensation history"),
        ("overtime_policies", "overtime policy configuration"),
        ("attendance_records", "attendance records"),
        ("deduction_types", "deduction type configuration"),
        ("deduction_rates", "deduction rate configuration"),
        ("payroll_employee_results", "calculated payroll results"),
        ("payroll_earning_lines", "payroll earning detail"),
        ("payroll_deduction_lines", "payroll deduction detail"),
        ("payroll_reversals", "payroll reversal history"),
    ]:
        op.execute(
            f"DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {table}) THEN "
            f"RAISE EXCEPTION "
            f"'Cannot downgrade: {table} has real {label} — dropping this table would "
            f"destroy it with no other representation anywhere in the schema.'; "
            f"END IF; "
            f"END $$;"
        )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM payroll_periods WHERE status <> 'DRAFT') THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more payroll_periods have been opened, calculated, "
        "approved, or posted — dropping this table would destroy that real payroll "
        "decision history. Only periods still in DRAFT (never acted on) may be "
        "discarded.'; "
        "END IF; "
        "END $$;"
    )

    # --- revert role/permission seed data ---------------------------------------
    new_permission_codes = [code for code, _ in _NEW_PERMISSIONS]
    new_role_names = [name for name, _ in _NEW_ROLES]
    conn.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": new_permission_codes},
    )
    conn.execute(
        sa.text(
            "DELETE FROM user_roles WHERE role_id IN "
            "(SELECT id FROM roles WHERE name = ANY(:names))"
        ),
        {"names": new_role_names},
    )
    conn.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE name = ANY(:names))"
        ),
        {"names": new_role_names},
    )
    conn.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": new_permission_codes},
    )
    conn.execute(
        sa.text("DELETE FROM roles WHERE name = ANY(:names)"),
        {"names": new_role_names},
    )

    # --- revert the six new payroll GL accounts ---------------------------------
    conn.execute(
        sa.text("DELETE FROM accounts WHERE code = ANY(:codes)"),
        {"codes": [code for code, *_ in _NEW_ACCOUNTS]},
    )

    # --- narrow the journal source_type CHECK back -------------------------------
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )

    # --- drop tables, children first --------------------------------------------
    op.drop_table("payroll_reversals")
    op.drop_table("payroll_deduction_lines")
    op.drop_table("payroll_earning_lines")
    op.drop_table("payroll_employee_results")
    op.drop_table("payroll_periods")
    op.drop_table("deduction_rates")
    op.drop_table("deduction_types")
    op.drop_table("attendance_records")
    op.drop_table("overtime_policies")
    op.drop_table("compensation_periods")
    op.drop_table("employment_assignments")
    op.drop_table("employment_status_periods")
    op.drop_table("employees")
    op.drop_table("positions")
    op.drop_table("departments")

    op.drop_constraint("ck_stores_attendance_day_boundary_hour", "stores", type_="check")
    op.drop_column("stores", "attendance_day_boundary_hour")
