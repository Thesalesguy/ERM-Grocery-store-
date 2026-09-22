"""m15 cashier/till shift sessions

Revision ID: db482a11ee31
Revises: 1a4bae98d246
Create Date: 2026-09-22 00:00:00.000000

See docs/M15_DESIGN.md. Adds:

- `cashier_shifts` — a cashier's bounded cash-handling session at one
  store (opening float through closing physical count/variance).
- `cash_movements` — paid-in/paid-out cash movements recorded during an
  OPEN shift.
- `sales.shift_id` / `sale_returns.shift_id` — nullable FKs attributing a
  cash sale/return to the shift open at the time (opportunistic, never
  backfilled for pre-M15 rows — docs/M15_DESIGN.md "Scope boundary").
- One new chart-of-accounts row, `5910 Cash Over/Short` (mirrors the
  M6/M8/M10 precedent of adding an account in a later milestone's
  migration via `ON CONFLICT (code) DO NOTHING`).
- Widens `ck_journal_entries_source_type` to accept `CASH_SHIFT_VARIANCE`.
- Three new permissions (`shift.manage`, `shift.read`, `shift.override`),
  granted to Admin (implicitly, via `list(ALL_PERMISSIONS)`), Manager
  (all three), Cashier (`shift.manage`+`shift.read` — operating a shift
  and viewing shift history go together, mirroring Cashier's existing
  `pos.use`+`sales.read` pairing), and Auditor (`shift.read` only) —
  mirrors e0d2359bb08a's own `_NEW_PERMISSIONS`/`_ROLE_GRANTS`/
  `ON CONFLICT DO UPDATE ... RETURNING` pattern for adding RBAC
  incrementally after the M2 seed migration has already run.

No changes to any M0-M14 table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db482a11ee31"
down_revision: Union[str, None] = "1a4bae98d246"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The full source_type set as it existed immediately before this migration
# (accounting/models.py's AUTOMATED_SOURCE_TYPES + "MANUAL" as of M14) —
# hardcoded here, not imported from app code, so this migration replays
# correctly regardless of later changes to that Python tuple (same
# discipline every prior source_type-widening migration follows).
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
    "PAYROLL_POSTING",
    "PAYROLL_REVERSAL",
    "MANUAL",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + ("CASH_SHIFT_VARIANCE",)

_NEW_ACCOUNT = (
    "5910",
    "Cash Over/Short",
    "EXPENSE",
    "DEBIT",
    "Difference between a cashier shift's physically counted cash and the expected cash "
    "derived from that shift's sales/returns/cash movements (docs/M15_DESIGN.md 'GL "
    "treatment'); routinely carries a credit balance for a run of cash overages, same "
    "contra convention as Purchase Price Variance.",
)

_NEW_PERMISSIONS = [
    ("shift.manage", "Open, record cash movements against, and close one's own cashier shift"),
    ("shift.read", "View cashier shift history and reconciliation detail"),
    ("shift.override", "Close another cashier's shift on their behalf"),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
    "Cashier": ["shift.manage", "shift.read"],
    "Auditor": ["shift.read"],
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- Widen the journal source_type CHECK ----------------------------
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )

    # --- New chart-of-accounts row ---------------------------------------
    code, name, account_type, normal_balance, description = _NEW_ACCOUNT
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

    # --- cashier_shifts ---------------------------------------------------
    op.create_table(
        "cashier_shifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("cashier_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="OPEN"),
        sa.Column("opening_float", sa.Numeric(12, 2), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False, unique=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_counted_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("expected_cash_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("variance_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("closed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "close_client_transaction_id", sa.String(length=100), nullable=True, unique=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_cashier_shifts_status", "cashier_shifts", "status IN ('OPEN', 'CLOSED')"
    )
    op.create_check_constraint(
        "ck_cashier_shifts_opening_float_non_negative", "cashier_shifts", "opening_float >= 0"
    )
    op.create_check_constraint(
        "ck_cashier_shifts_closing_counted_non_negative",
        "cashier_shifts",
        "closing_counted_amount IS NULL OR closing_counted_amount >= 0",
    )
    op.create_check_constraint(
        "ck_cashier_shifts_closing_fields_consistent",
        "cashier_shifts",
        "(status = 'OPEN' AND closed_at IS NULL AND closing_counted_amount IS NULL "
        "AND expected_cash_amount IS NULL AND variance_amount IS NULL AND closed_by IS NULL) "
        "OR "
        "(status = 'CLOSED' AND closed_at IS NOT NULL AND closing_counted_amount IS NOT NULL "
        "AND expected_cash_amount IS NOT NULL AND variance_amount IS NOT NULL "
        "AND closed_by IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_cashier_shifts_variance_consistent",
        "cashier_shifts",
        "status = 'OPEN' OR variance_amount = closing_counted_amount - expected_cash_amount",
    )
    op.create_index("ix_cashier_shifts_store_id", "cashier_shifts", ["store_id"])
    op.create_index("ix_cashier_shifts_cashier_id", "cashier_shifts", ["cashier_id"])
    op.create_index(
        "uq_cashier_shifts_one_open_per_cashier",
        "cashier_shifts",
        ["cashier_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    # --- cash_movements -----------------------------------------------------
    op.create_table(
        "cash_movements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "shift_id", sa.Integer(), sa.ForeignKey("cashier_shifts.id"), nullable=False
        ),
        sa.Column("movement_type", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_cash_movements_amount_positive", "cash_movements", "amount > 0"
    )
    op.create_check_constraint(
        "ck_cash_movements_type", "cash_movements", "movement_type IN ('PAID_IN', 'PAID_OUT')"
    )
    op.create_index("ix_cash_movements_shift_id", "cash_movements", ["shift_id"])

    # --- Sale/SaleReturn shift attribution --------------------------------
    op.add_column(
        "sales", sa.Column("shift_id", sa.Integer(), sa.ForeignKey("cashier_shifts.id"))
    )
    op.add_column(
        "sale_returns", sa.Column("shift_id", sa.Integer(), sa.ForeignKey("cashier_shifts.id"))
    )

    # --- RBAC: three new permissions --------------------------------------
    # ON CONFLICT DO UPDATE ... RETURNING (mirrors e0d2359bb08a exactly —
    # e6180fca2ee0's seed migration reads permissions.py LIVE, so a
    # from-scratch upgrade already seeds these before this migration runs).
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

    # Guard FIRST, before any destructive step (same discipline as
    # e0d2359bb08a's own downgrade): real M15 data the M14 schema cannot
    # represent must refuse the downgrade loudly, not silently destroy a
    # shift's opening float, close/variance history, or cash-movement
    # audit trail.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM cashier_shifts) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more cashier_shifts rows exist -- dropping this table "
        "would silently discard real shift/cash-handling history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_entries WHERE source_type = 'CASH_SHIFT_VARIANCE') "
        "THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_entries has M15 cash-shift-variance rows that would "
        "violate the narrower ck_journal_entries_source_type constraint.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_lines jl JOIN accounts a ON a.id = jl.account_id "
        "  WHERE a.code = '5910') "
        "THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_lines already reference the Cash Over/Short account "
        "from real posted transactions -- downgrading would orphan or destroy that ledger "
        "history.'; "
        "END IF; "
        "END $$;"
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

    op.drop_column("sale_returns", "shift_id")
    op.drop_column("sales", "shift_id")

    op.drop_table("cash_movements")
    op.drop_table("cashier_shifts")

    conn.execute(sa.text("DELETE FROM accounts WHERE code = '5910'"))

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )
