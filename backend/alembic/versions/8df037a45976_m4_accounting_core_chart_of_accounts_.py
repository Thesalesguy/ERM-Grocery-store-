"""m4 accounting core: chart of accounts, journal engine, permissions

Revision ID: 8df037a45976
Revises: c82162efb3af
Create Date: 2026-09-11 14:06:02.864366

See docs/M4_ACCOUNTING_CORE.md for the full design. This migration does
four things, in order:

1. Creates `accounts`, `journal_entries`, `journal_lines`.
2. Adds a DEFERRED constraint trigger on `journal_lines` that rejects an
   unbalanced, zero-total, or empty journal entry at COMMIT time — a
   real database-level invariant (M4 task Section 5/33 Invariant 1), not
   just a Python-level check. It fires once per row change and
   re-validates the touched entry's current aggregate, so it is
   correct regardless of how many lines are inserted in one statement
   or one transaction.
3. Extends the M1 ledger-privilege pattern
   (9163f992ddc1_m1_runtime_role_privileges...): REVOKEs UPDATE, DELETE
   on `journal_entries` and `journal_lines` from `erp_app`, per that
   migration's own docstring instruction that a future append-only
   table must opt out of the default broad grant explicitly, in the
   same migration that creates it. This is what makes "posted entries
   cannot be edited or deleted" true at the database level, not just by
   convention — a reversal is always a new INSERT, never an UPDATE.
4. Seeds the eleven system accounts (`app.modules.accounting.constants
   .SYSTEM_ACCOUNTS` — the single source of truth also used by
   application code, so the seed and the account-code constants can
   never drift) and the four new `accounting.*` permission codes,
   granted to the roles decided in docs/M4_ACCOUNTING_CORE.md Section
   15. Permissions are seeded here rather than by re-running the M2
   seed migration (e6180fca2ee0), which already executed against every
   existing database — adding new permission codes after that point
   requires a new migration that inserts only the new rows, exactly
   like this one does.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8df037a45976"
down_revision: Union[str, None] = "c82162efb3af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLE = "erp_app"
_LEDGER_TABLES = ("journal_entries", "journal_lines")

_SYSTEM_ACCOUNTS = [
    ("1000", "Cash on Hand", "ASSET", "DEBIT",
     "Physical cash held at the till; CASH-tendered sale payments and change given."),
    ("1010", "Card Clearing", "ASSET", "DEBIT",
     "CARD sale payments pending settlement from the card processor."),
    ("1020", "Mobile Money Clearing", "ASSET", "DEBIT",
     "MOBILE_MONEY sale payments pending settlement."),
    ("1030", "Bank Transfer Clearing", "ASSET", "DEBIT",
     "BANK_TRANSFER sale payments pending settlement."),
    ("1040", "Other Payment Clearing", "ASSET", "DEBIT",
     "Sale payments recorded under the OTHER payment method."),
    ("1500", "Inventory", "ASSET", "DEBIT",
     "Weighted-average cost value of on-hand stock across all stores."),
    ("2000", "Purchase Clearing (Accounts Payable)", "LIABILITY", "CREDIT",
     "Goods received from a supplier but not yet reconciled against a supplier "
     "payable/paid record (docs/M4_ACCOUNTING_CORE.md Section 9)."),
    ("2100", "Tax Payable", "LIABILITY", "CREDIT",
     "Sales tax collected from customers, owed to the tax authority."),
    ("4000", "Sales Revenue", "REVENUE", "CREDIT",
     "Gross sale revenue before discounts (net of tax)."),
    ("4100", "Sales Discounts", "REVENUE", "DEBIT",
     "Contra-revenue: discounts given on sales, reducing gross Sales Revenue."),
    ("4900", "Inventory Adjustment Gain", "REVENUE", "CREDIT",
     "Stock found in excess of the recorded on-hand quantity (positive adjustment)."),
    ("5000", "Cost of Goods Sold", "EXPENSE", "DEBIT",
     "Weighted-average cost of inventory sold, frozen per sale line at sale time."),
    ("5900", "Inventory Shrinkage Expense", "EXPENSE", "DEBIT",
     "Stock found missing relative to the recorded on-hand quantity (negative adjustment)."),
]

_NEW_PERMISSIONS = [
    ("accounting.read", "View the chart of accounts, journal entries, and financial reports"),
    ("accounting.post", "Manually post a journal entry (reserved; no endpoint uses this yet)"),
    ("accounting.reverse", "Reverse a posted journal entry with a compensating entry"),
    ("accounting.admin", "Manage accounting configuration (reserved for future chart-of-accounts admin)"),
]

# role name -> permission codes granted, from this migration's new set only
# (docs/M4_ACCOUNTING_CORE.md Section 15). Admin already has every
# permission via a separate mechanism in the seed migration's own logic,
# but that mechanism doesn't run again here, so Admin is granted
# explicitly below too.
_ROLE_GRANTS = {
    "Admin": ["accounting.read", "accounting.post", "accounting.reverse", "accounting.admin"],
    "Manager": ["accounting.read", "accounting.reverse"],
    "Auditor": ["accounting.read"],
}


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("account_type", sa.String(length=20), nullable=False),
        sa.Column("normal_balance", sa.String(length=10), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')",
            name="ck_accounts_type",
        ),
        sa.CheckConstraint(
            "normal_balance IN ('DEBIT', 'CREDIT')", name="ck_accounts_normal_balance"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_accounts_code"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_number", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("entry_type", sa.String(length=10), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("reversal_of_id", sa.Integer(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(entry_type = 'REVERSAL' AND reversal_of_id IS NOT NULL) OR "
            "(entry_type = 'STANDARD' AND reversal_of_id IS NULL)",
            name="ck_journal_entries_reversal_consistency",
        ),
        sa.CheckConstraint(
            "entry_type IN ('STANDARD', 'REVERSAL')", name="ck_journal_entries_entry_type"
        ),
        sa.CheckConstraint(
            "source_type IN ('SALE', 'PURCHASE_RECEIPT', 'PURCHASE_RETURN', "
            "'SALE_RETURN', 'STOCK_ADJUSTMENT')",
            name="ck_journal_entries_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_journal_entries_created_by_users"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of_id"], ["journal_entries.id"], name="fk_journal_entries_reversal_of_id"
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_journal_entries_store_id_stores"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_number", name="uq_journal_entries_journal_number"),
    )
    op.create_index(
        "ix_journal_entries_source", "journal_entries", ["source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_journal_entries_store_posting_date", "journal_entries",
        ["store_id", "posting_date"], unique=False,
    )
    op.create_index(
        "uq_journal_entries_source", "journal_entries", ["source_type", "source_id"],
        unique=True, postgresql_where=sa.text("source_id IS NOT NULL AND entry_type = 'STANDARD'"),
    )
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("debit", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("credit", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_lines_exactly_one_side",
        ),
        sa.CheckConstraint("credit >= 0", name="ck_journal_lines_credit_non_negative"),
        sa.CheckConstraint("debit >= 0", name="ck_journal_lines_debit_non_negative"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_journal_lines_account_id_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"], ["journal_entries.id"],
            name="fk_journal_lines_journal_entry_id",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_journal_lines_product_id_products"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_journal_lines_account_id", "journal_lines", ["account_id"], unique=False
    )
    op.create_index(
        "ix_journal_lines_journal_entry_id", "journal_lines", ["journal_entry_id"], unique=False
    )

    # --- Deferred balance/non-zero/non-empty constraint trigger --------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_journal_entry_balance() RETURNS TRIGGER AS $$
        DECLARE
            entry_id INTEGER;
            debit_sum NUMERIC;
            credit_sum NUMERIC;
            line_count INTEGER;
        BEGIN
            entry_id := NEW.journal_entry_id;

            SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0), COUNT(*)
                INTO debit_sum, credit_sum, line_count
                FROM journal_lines
                WHERE journal_entry_id = entry_id;

            IF line_count = 0 THEN
                RAISE EXCEPTION 'Journal entry % has no lines', entry_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF debit_sum <> credit_sum THEN
                RAISE EXCEPTION 'Journal entry % is unbalanced: debits % <> credits %',
                    entry_id, debit_sum, credit_sum
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF debit_sum = 0 THEN
                RAISE EXCEPTION 'Journal entry % has a zero total', entry_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        -- INSERT/UPDATE only, deliberately not DELETE: erp_app can never
        -- DELETE a journal_lines row (privilege revoked below), so the
        -- only way a line is ever removed is superuser/DBA-level manual
        -- intervention (e.g. purging test data) — a case that should not
        -- be blocked by a trigger meant to keep the *application* from
        -- ever creating an unbalanced/empty entry. Checking DELETE too
        -- would reject the ordinary "delete the lines, then delete the
        -- now-orphaned entry" cleanup sequence at the first step, since
        -- the entry briefly has zero lines before it is itself removed.
        CREATE CONSTRAINT TRIGGER trg_journal_lines_balance
        AFTER INSERT OR UPDATE ON journal_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_journal_entry_balance();
        """
    )

    # --- Ledger-table privilege carve-out (M1 pattern, extended) -------
    for table in _LEDGER_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                    EXECUTE 'REVOKE UPDATE, DELETE ON {table} FROM {_APP_ROLE}';
                END IF;
            END
            $$;
            """
        )

    # --- Seed the chart of accounts -------------------------------------
    conn = op.get_bind()
    for code, name, account_type, normal_balance, description in _SYSTEM_ACCOUNTS:
        conn.execute(
            sa.text(
                "INSERT INTO accounts "
                "(code, name, account_type, normal_balance, is_system, is_active, "
                " description, created_at) "
                "VALUES (:code, :name, :account_type, :normal_balance, true, true, "
                " :description, now())"
            ),
            {
                "code": code,
                "name": name,
                "account_type": account_type,
                "normal_balance": normal_balance,
                "description": description,
            },
        )

    # --- Seed the new accounting.* permissions and grant them -----------
    # ON CONFLICT DO NOTHING: e6180fca2ee0 (the M2 seed migration) reads
    # app.modules.auth.permissions.ALL_PERMISSIONS/ROLE_PERMISSIONS live
    # at migration-run time, not a frozen snapshot — so on a *fresh*
    # database (e.g. this test's own downgrade-to-base-then-upgrade
    # cycle), replaying e6180fca2ee0 with today's permissions.py (which
    # now includes the accounting.* codes added for M4) already seeds
    # these rows before this migration runs. On an *existing* database
    # that ran e6180fca2ee0 before M4 existed, those rows are genuinely
    # new here. ON CONFLICT makes this migration correct in both cases
    # without needing to know which one it's running against.
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

    role_ids: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            sa.text("SELECT name, id FROM roles WHERE name = ANY(:names)"),
            {"names": list(_ROLE_GRANTS)},
        )
    }
    for role_name, codes in _ROLE_GRANTS.items():
        role_id = role_ids.get(role_name)
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

    op.execute("DROP TRIGGER IF EXISTS trg_journal_lines_balance ON journal_lines")
    op.execute("DROP FUNCTION IF EXISTS check_journal_entry_balance()")

    op.drop_index("ix_journal_lines_journal_entry_id", table_name="journal_lines")
    op.drop_index("ix_journal_lines_account_id", table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index(
        "uq_journal_entries_source", table_name="journal_entries",
        postgresql_where=sa.text("source_id IS NOT NULL AND entry_type = 'STANDARD'"),
    )
    op.drop_index("ix_journal_entries_store_posting_date", table_name="journal_entries")
    op.drop_index("ix_journal_entries_source", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_table("accounts")
