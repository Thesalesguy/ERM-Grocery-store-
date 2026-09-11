"""m6 accounts payable: purchase invoices, supplier payments

Revision ID: 36173e29a9f0
Revises: 35d411b947ec
Create Date: 2026-09-11 18:33:00.583318

See docs/M6_AP_VENDOR_ACCOUNTING.md. Adds:

- Five new Chart of Accounts entries: Bank Account (Operating, 1050),
  Accounts Payable (2010), Purchase Price Variance (5100), Purchase
  Discounts (5150), Purchase Tax Expense — Non-Recoverable (5200).
- Renames the existing Purchase Clearing (2000) account's name/
  description now that a real, separate Accounts Payable account exists
  (it was never itself the payable to the supplier).
- Widens `ck_journal_entries_source_type` to accept three new automated
  source types: PURCHASE_INVOICE, PURCHASE_INVOICE_VOID, SUPPLIER_PAYMENT.
- `suppliers.default_payment_terms_days` — nullable, no backfill needed
  (NULL is itself a valid, meaningful value: "no standing term").
- `purchase_order_items.quantity_invoiced` — a maintained cache mirroring
  `quantity_received`'s own M3 pattern. Nullable -> backfill 0 -> NOT
  NULL -> CHECK bounds, the same safe pattern M5 used for
  `sale_items.quantity_returned`.
- Three new tables: `purchase_invoices`, `purchase_invoice_lines`,
  `supplier_payments`.
- Seeds the four new `ap.*` permissions and grants them per
  docs/M6_AP_VENDOR_ACCOUNTING.md "RBAC".

All constraints named, per repository convention.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "36173e29a9f0"
down_revision: Union[str, None] = "35d411b947ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "MANUAL",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + (
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
)

_NEW_ACCOUNTS = [
    (
        "1050",
        "Bank Account (Operating)",
        "ASSET",
        "DEBIT",
        "The business's real operating bank account; reduced by BANK_TRANSFER/CHEQUE/OTHER "
        "supplier payments (docs/M6_AP_VENDOR_ACCOUNTING.md 'Supplier payment accounting').",
    ),
    (
        "2010",
        "Accounts Payable",
        "LIABILITY",
        "CREDIT",
        "Amounts owed to suppliers for POSTED (matched) invoices, reduced only by a "
        "recorded SupplierPayment (docs/M6_AP_VENDOR_ACCOUNTING.md 'AP liability').",
    ),
    (
        "5100",
        "Purchase Price Variance",
        "EXPENSE",
        "DEBIT",
        "Difference between a posted invoice's unit price and the receipt cost it is "
        "matched against (docs/M6_AP_VENDOR_ACCOUNTING.md 'Invoice variances'); often "
        "carries a credit balance for favorable variances, same contra convention as "
        "Sales Discounts.",
    ),
    (
        "5150",
        "Purchase Discounts",
        "EXPENSE",
        "CREDIT",
        "Contra-cost: a supplier-granted discount stated on a posted invoice, reducing "
        "the net cost of the purchase (docs/M6_AP_VENDOR_ACCOUNTING.md 'Invoice discounts').",
    ),
    (
        "5200",
        "Purchase Tax Expense (Non-Recoverable)",
        "EXPENSE",
        "DEBIT",
        "Tax stated on a posted supplier invoice, treated conservatively as a "
        "non-recoverable cost of the purchase in the absence of an input-tax-recovery "
        "workflow (docs/M6_AP_VENDOR_ACCOUNTING.md 'Purchase invoice tax').",
    ),
]

_NEW_PERMISSIONS = [
    ("ap.read", "View supplier invoices, AP balances, aging, and Purchase Clearing reconciliation"),
    ("ap.write", "Create/void a draft supplier invoice"),
    ("ap.post", "Post a supplier invoice, matching it against receipts and establishing AP"),
    ("ap.pay", "Record a supplier payment, settling Accounts Payable"),
]

_ROLE_GRANTS = {
    "Admin": ["ap.read", "ap.write", "ap.post", "ap.pay"],
    "Manager": ["ap.read", "ap.write", "ap.post", "ap.pay"],
    "Inventory Clerk": ["ap.read", "ap.write"],
    "Auditor": ["ap.read"],
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

    # --- Rename the existing Purchase Clearing account ------------------
    conn.execute(
        sa.text("UPDATE accounts SET name = :name, description = :description WHERE code = '2000'"),
        {
            "name": "Purchase Clearing",
            "description": (
                "Goods received from a supplier but not yet matched against a posted supplier "
                "invoice (docs/M6_AP_VENDOR_ACCOUNTING.md 'Purchase Clearing lifecycle'). Renamed "
                "from 'Purchase Clearing (Accounts Payable)' in M6 now that a real, separate "
                "Accounts Payable account exists — this account was never itself the payable to "
                "the supplier, only the interim received-not-yet-invoiced position."
            ),
        },
    )

    # --- Seed the five new accounts --------------------------------------
    for code, name, account_type, normal_balance, description in _NEW_ACCOUNTS:
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

    # --- suppliers.default_payment_terms_days ----------------------------
    op.add_column("suppliers", sa.Column("default_payment_terms_days", sa.Integer(), nullable=True))

    # --- purchase_order_items.quantity_invoiced ---------------------------
    op.add_column(
        "purchase_order_items",
        sa.Column("quantity_invoiced", sa.Numeric(precision=14, scale=3), nullable=True),
    )
    op.execute(
        "UPDATE purchase_order_items SET quantity_invoiced = 0 WHERE quantity_invoiced IS NULL"
    )
    op.alter_column("purchase_order_items", "quantity_invoiced", nullable=False)
    op.create_check_constraint(
        "ck_purchase_order_items_qty_invoiced_bounds",
        "purchase_order_items",
        "quantity_invoiced >= 0 AND quantity_invoiced <= quantity_received",
    )

    # --- purchase_invoices --------------------------------------------------
    op.create_table(
        "purchase_invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column(
            "purchase_order_id", sa.Integer(), sa.ForeignKey("purchase_orders.id"), nullable=False
        ),
        sa.Column("invoice_number", sa.String(length=100), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column(
            "subtotal", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "discount_total", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "tax_total", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "grand_total", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "amount_paid", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_purchase_invoices_supplier_invoice_number",
        "purchase_invoices",
        ["supplier_id", "invoice_number"],
    )
    op.create_unique_constraint(
        "uq_purchase_invoices_client_transaction_id",
        "purchase_invoices",
        ["client_transaction_id"],
    )
    op.create_check_constraint(
        "ck_purchase_invoices_status",
        "purchase_invoices",
        "status IN ('DRAFT', 'POSTED', 'PARTIALLY_PAID', 'PAID', 'VOIDED')",
    )
    op.create_check_constraint(
        "ck_purchase_invoices_subtotal_non_negative", "purchase_invoices", "subtotal >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_discount_non_negative", "purchase_invoices", "discount_total >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_tax_non_negative", "purchase_invoices", "tax_total >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_grand_total_consistent",
        "purchase_invoices",
        "grand_total = subtotal - discount_total + tax_total",
    )
    op.create_check_constraint(
        "ck_purchase_invoices_grand_total_non_negative", "purchase_invoices", "grand_total >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_amount_paid_bounds",
        "purchase_invoices",
        "amount_paid >= 0 AND amount_paid <= grand_total",
    )
    op.create_index("ix_purchase_invoices_store_id", "purchase_invoices", ["store_id"])
    op.create_index("ix_purchase_invoices_supplier_id", "purchase_invoices", ["supplier_id"])
    op.create_index(
        "ix_purchase_invoices_purchase_order_id", "purchase_invoices", ["purchase_order_id"]
    )
    op.create_index("ix_purchase_invoices_status", "purchase_invoices", ["status"])

    # --- purchase_invoice_lines --------------------------------------------
    op.create_table(
        "purchase_invoice_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "purchase_invoice_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoices.id"),
            nullable=False,
        ),
        sa.Column(
            "purchase_order_item_id",
            sa.Integer(),
            sa.ForeignKey("purchase_order_items.id"),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity_invoiced", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column("line_total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_purchase_invoice_lines_qty_positive", "purchase_invoice_lines", "quantity_invoiced > 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoice_lines_price_non_negative", "purchase_invoice_lines", "unit_price >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoice_lines_discount_non_negative",
        "purchase_invoice_lines",
        "discount_amount >= 0",
    )
    op.create_check_constraint(
        "ck_purchase_invoice_lines_tax_non_negative", "purchase_invoice_lines", "tax_amount >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoice_lines_total_consistent",
        "purchase_invoice_lines",
        "line_total = round(quantity_invoiced * unit_price - discount_amount + tax_amount, 2)",
    )
    op.create_index(
        "ix_purchase_invoice_lines_purchase_invoice_id",
        "purchase_invoice_lines",
        ["purchase_invoice_id"],
    )
    op.create_index(
        "ix_purchase_invoice_lines_purchase_order_item_id",
        "purchase_invoice_lines",
        ["purchase_order_item_id"],
    )

    # --- supplier_payments ---------------------------------------------------
    op.create_table(
        "supplier_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column(
            "purchase_invoice_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoices.id"),
            nullable=False,
        ),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("payment_method", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_supplier_payments_client_transaction_id",
        "supplier_payments",
        ["client_transaction_id"],
    )
    op.create_check_constraint(
        "ck_supplier_payments_method",
        "supplier_payments",
        "payment_method IN ('CASH', 'BANK_TRANSFER', 'CHEQUE', 'OTHER')",
    )
    op.create_check_constraint(
        "ck_supplier_payments_amount_positive", "supplier_payments", "amount > 0"
    )
    op.create_index("ix_supplier_payments_store_id", "supplier_payments", ["store_id"])
    op.create_index("ix_supplier_payments_supplier_id", "supplier_payments", ["supplier_id"])
    op.create_index(
        "ix_supplier_payments_purchase_invoice_id", "supplier_payments", ["purchase_invoice_id"]
    )

    # --- Seed the new ap.* permissions and grant them ---------------------
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

    # --- Guard FIRST, before any destructive step: the new accounts are
    # real Chart-of-Accounts rows that `journal_lines` (an immutable
    # ledger — UPDATE/DELETE are revoked from erp_app on it) may already
    # reference from any real PURCHASE_INVOICE/PURCHASE_INVOICE_VOID/
    # SUPPLIER_PAYMENT posting. Attempting `DELETE FROM accounts` while
    # such references exist would previously raise a raw
    # ForeignKeyViolation mid-migration (caught live during this
    # milestone's own Session N verification against real populated
    # data) — fail loudly and cleanly instead, before touching anything,
    # rather than a confusing crash partway through. This is the
    # financially correct outcome: once a real transaction has posted
    # against these accounts, downgrading past this migration would
    # either orphan that ledger data or silently delete financial
    # history — neither is acceptable, so the downgrade refuses instead
    # (same "fail loudly rather than silently destroy data" principle as
    # 581d2a07f38c's MANUAL-rows guard, applied one level earlier here).
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "  SELECT 1 FROM journal_lines jl "
        "  JOIN accounts a ON a.id = jl.account_id "
        "  WHERE a.code IN ('1050', '2010', '5100', '5150', '5200')"
        ") THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_lines already reference one or more M6 AP accounts "
        "(Bank Account/Accounts Payable/Purchase Price Variance/Purchase Discounts/"
        "Purchase Tax Expense) from real posted transactions — downgrading would orphan "
        "or destroy that ledger history. Reverse/void the underlying AP transactions "
        "first if this downgrade is genuinely required.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_entries WHERE source_type IN "
        "('PURCHASE_INVOICE', 'PURCHASE_INVOICE_VOID', 'SUPPLIER_PAYMENT')) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_entries has M6 AP rows that would violate the "
        "narrower ck_journal_entries_source_type constraint'; "
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

    op.drop_table("supplier_payments")
    op.drop_table("purchase_invoice_lines")
    op.drop_table("purchase_invoices")

    op.drop_constraint(
        "ck_purchase_order_items_qty_invoiced_bounds", "purchase_order_items", type_="check"
    )
    op.drop_column("purchase_order_items", "quantity_invoiced")

    op.drop_column("suppliers", "default_payment_terms_days")

    conn.execute(
        sa.text("DELETE FROM accounts WHERE code = ANY(:codes)"),
        {"codes": ["1050", "2010", "5100", "5150", "5200"]},
    )
    conn.execute(
        sa.text("UPDATE accounts SET name = :name, description = :description WHERE code = '2000'"),
        {
            "name": "Purchase Clearing (Accounts Payable)",
            "description": (
                "Goods received from a supplier but not yet reconciled against a supplier "
                "payable/paid record (docs/M4_ACCOUNTING_CORE.md Section 9)."
            ),
        },
    )

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )
