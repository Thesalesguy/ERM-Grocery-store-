"""m7 advanced ap settlement: multi-po invoices, persisted receipt matches,
multi-invoice payment allocation, supplier credit notes

Revision ID: a4f2c8e91b6d
Revises: 36173e29a9f0
Create Date: 2026-09-12 00:00:00.000000

See docs/M7_ADVANCED_AP_SETTLEMENT.md. Adds/changes:

- `purchase_invoices.purchase_order_id` becomes nullable (no longer a
  validation boundary — an invoice's lines may span multiple purchase
  orders; see the design doc Section 1).
- `purchase_invoices.amount_credited` — mirrors `amount_paid`; the old
  `ck_purchase_invoices_amount_paid_bounds` check is replaced by
  `ck_purchase_invoices_settlement_bounds`
  (`amount_paid + amount_credited <= grand_total`).
- New table `purchase_invoice_receipt_matches` — persists the FIFO
  receipt-lot matches `post_purchase_invoice` computes, instead of only
  summing them (design doc Section 1/5).
- `supplier_payments.purchase_invoice_id` is replaced by a new
  `supplier_payment_allocations` table (a payment may now settle several
  invoices). Existing M6 payment rows are migrated into one allocation
  row each before the column is dropped — no data loss.
- Three new tables: `supplier_credit_notes`, `supplier_credit_note_lines`,
  `supplier_credit_allocations`.
- Widens `ck_journal_entries_source_type` to accept `SUPPLIER_CREDIT_NOTE`.
- Seeds the new `ap.credit` permission and grants it to Admin/Manager.

No new Chart-of-Accounts entries — credit notes post through the existing
Accounts Payable/Inventory/Purchase Discounts accounts (design doc Section
13), so this migration touches no `accounts` rows at all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4f2c8e91b6d"
down_revision: Union[str, None] = "36173e29a9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "MANUAL",
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES + ("SUPPLIER_CREDIT_NOTE",)

_NEW_PERMISSIONS = [
    ("ap.credit", "Create a supplier credit note, reducing Accounts Payable"),
]

_ROLE_GRANTS = {
    "Admin": ["ap.credit"],
    "Manager": ["ap.credit"],
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

    # --- purchase_invoices: nullable purchase_order_id + amount_credited --
    op.alter_column("purchase_invoices", "purchase_order_id", nullable=True)
    op.add_column(
        "purchase_invoices",
        sa.Column(
            "amount_credited",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE purchase_invoices SET amount_credited = 0 WHERE amount_credited IS NULL"
    )
    op.alter_column("purchase_invoices", "amount_credited", nullable=False, server_default="0")
    op.drop_constraint(
        "ck_purchase_invoices_amount_paid_bounds", "purchase_invoices", type_="check"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_amount_paid_non_negative", "purchase_invoices", "amount_paid >= 0"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_amount_credited_non_negative",
        "purchase_invoices",
        "amount_credited >= 0",
    )
    op.create_check_constraint(
        "ck_purchase_invoices_settlement_bounds",
        "purchase_invoices",
        "amount_paid + amount_credited <= grand_total",
    )

    # --- purchase_invoice_receipt_matches ---------------------------------
    op.create_table(
        "purchase_invoice_receipt_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "purchase_invoice_line_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoice_lines.id"),
            nullable=False,
        ),
        sa.Column(
            "goods_receipt_item_id",
            sa.Integer(),
            sa.ForeignKey("goods_receipt_items.id"),
            nullable=False,
        ),
        sa.Column("matched_quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("matched_unit_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("variance_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_pi_receipt_matches_qty_positive",
        "purchase_invoice_receipt_matches",
        "matched_quantity > 0",
    )
    op.create_check_constraint(
        "ck_pi_receipt_matches_unit_cost_non_negative",
        "purchase_invoice_receipt_matches",
        "matched_unit_cost >= 0",
    )
    op.create_index(
        "ix_pi_receipt_matches_purchase_invoice_line_id",
        "purchase_invoice_receipt_matches",
        ["purchase_invoice_line_id"],
    )
    op.create_index(
        "ix_pi_receipt_matches_goods_receipt_item_id",
        "purchase_invoice_receipt_matches",
        ["goods_receipt_item_id"],
    )

    # --- supplier_payment_allocations (+ migrate existing M6 data) --------
    op.create_table(
        "supplier_payment_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_payment_id",
            sa.Integer(),
            sa.ForeignKey("supplier_payments.id"),
            nullable=False,
        ),
        sa.Column(
            "purchase_invoice_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoices.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_supplier_payment_allocations_payment_invoice",
        "supplier_payment_allocations",
        ["supplier_payment_id", "purchase_invoice_id"],
    )
    op.create_check_constraint(
        "ck_supplier_payment_allocations_amount_positive",
        "supplier_payment_allocations",
        "amount > 0",
    )
    op.create_index(
        "ix_supplier_payment_allocations_supplier_payment_id",
        "supplier_payment_allocations",
        ["supplier_payment_id"],
    )
    op.create_index(
        "ix_supplier_payment_allocations_purchase_invoice_id",
        "supplier_payment_allocations",
        ["purchase_invoice_id"],
    )

    # Every existing M6 SupplierPayment settled exactly one invoice via its
    # own purchase_invoice_id column — migrate each into one allocation row
    # of the same amount before the column disappears. No data loss: the
    # (payment, invoice, amount) triple is preserved exactly.
    conn.execute(
        sa.text(
            "INSERT INTO supplier_payment_allocations "
            "(supplier_payment_id, purchase_invoice_id, amount, created_at) "
            "SELECT id, purchase_invoice_id, amount, now() FROM supplier_payments"
        )
    )
    op.drop_index("ix_supplier_payments_purchase_invoice_id", table_name="supplier_payments")
    op.drop_column("supplier_payments", "purchase_invoice_id")

    # --- supplier_credit_notes ---------------------------------------------
    op.create_table(
        "supplier_credit_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("credit_number", sa.String(length=100), nullable=False),
        sa.Column("credit_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column(
            "purchase_return_id",
            sa.Integer(),
            sa.ForeignKey("purchase_returns.id"),
            nullable=True,
        ),
        sa.Column("grand_total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "amount_allocated",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("client_transaction_id", sa.String(length=100), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_supplier_credit_notes_supplier_credit_number",
        "supplier_credit_notes",
        ["supplier_id", "credit_number"],
    )
    op.create_unique_constraint(
        "uq_supplier_credit_notes_client_transaction_id",
        "supplier_credit_notes",
        ["client_transaction_id"],
    )
    op.create_check_constraint(
        "ck_supplier_credit_notes_reason",
        "supplier_credit_notes",
        "reason IN ('GOODS_RETURN', 'COMMERCIAL_DISCOUNT')",
    )
    op.create_check_constraint(
        "ck_supplier_credit_notes_grand_total_positive",
        "supplier_credit_notes",
        "grand_total > 0",
    )
    op.create_check_constraint(
        "ck_supplier_credit_notes_amount_allocated_bounds",
        "supplier_credit_notes",
        "amount_allocated >= 0 AND amount_allocated <= grand_total",
    )
    op.create_check_constraint(
        "ck_supplier_credit_notes_reason_reference_consistent",
        "supplier_credit_notes",
        "(reason = 'GOODS_RETURN' AND purchase_return_id IS NOT NULL) OR "
        "(reason = 'COMMERCIAL_DISCOUNT' AND purchase_return_id IS NULL)",
    )
    op.create_index("ix_supplier_credit_notes_store_id", "supplier_credit_notes", ["store_id"])
    op.create_index(
        "ix_supplier_credit_notes_supplier_id", "supplier_credit_notes", ["supplier_id"]
    )

    # --- supplier_credit_note_lines -----------------------------------------
    op.create_table(
        "supplier_credit_note_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_credit_note_id",
            sa.Integer(),
            sa.ForeignKey("supplier_credit_notes.id"),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_supplier_credit_note_lines_amount_positive",
        "supplier_credit_note_lines",
        "amount > 0",
    )
    op.create_check_constraint(
        "ck_supplier_credit_note_lines_qty_positive",
        "supplier_credit_note_lines",
        "quantity IS NULL OR quantity > 0",
    )
    op.create_index(
        "ix_supplier_credit_note_lines_supplier_credit_note_id",
        "supplier_credit_note_lines",
        ["supplier_credit_note_id"],
    )

    # --- supplier_credit_allocations -----------------------------------------
    op.create_table(
        "supplier_credit_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_credit_note_id",
            sa.Integer(),
            sa.ForeignKey("supplier_credit_notes.id"),
            nullable=False,
        ),
        sa.Column(
            "purchase_invoice_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoices.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_supplier_credit_allocations_credit_invoice",
        "supplier_credit_allocations",
        ["supplier_credit_note_id", "purchase_invoice_id"],
    )
    op.create_check_constraint(
        "ck_supplier_credit_allocations_amount_positive",
        "supplier_credit_allocations",
        "amount > 0",
    )
    op.create_index(
        "ix_supplier_credit_allocations_supplier_credit_note_id",
        "supplier_credit_allocations",
        ["supplier_credit_note_id"],
    )
    op.create_index(
        "ix_supplier_credit_allocations_purchase_invoice_id",
        "supplier_credit_allocations",
        ["purchase_invoice_id"],
    )

    # --- Seed the new ap.credit permission and grant it --------------------
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

    # --- Guard FIRST, before any destructive step (same discipline as
    # 36173e29a9f0's own downgrade — see that migration's comment for the
    # real crash this pattern was originally written to prevent). M7 adds
    # no new Chart-of-Accounts rows, so there is nothing to check there,
    # but several new facts CANNOT be losslessly represented in the M6
    # schema this downgrade returns to:
    #   1. A supplier payment that allocates across more than one invoice
    #      (the M6 schema has room for exactly one purchase_invoice_id).
    #   2. Any invoice with amount_credited > 0 (the M6 schema has no
    #      credit-note concept at all — dropping the column would destroy
    #      real financial history, not just a redundant cache).
    #   3. Any invoice with purchase_order_id IS NULL (the M6 schema
    #      requires it NOT NULL — a genuinely multi-PO invoice cannot be
    #      forced back into a single FK).
    #   4. Any supplier credit note ever created, or any
    #      SUPPLIER_CREDIT_NOTE journal entry (both are dropped/disallowed
    #      by the M6 schema/constraint this downgrades to).
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "  SELECT supplier_payment_id FROM supplier_payment_allocations "
        "  GROUP BY supplier_payment_id HAVING COUNT(*) > 1"
        ") THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more supplier payments allocate across multiple "
        "invoices, which cannot be represented by the single purchase_invoice_id "
        "column this downgrade restores. Reverse those payments first if this "
        "downgrade is genuinely required.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM purchase_invoices WHERE amount_credited > 0) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more purchase invoices have a nonzero "
        "amount_credited — dropping that column would silently destroy real "
        "supplier-credit-note financial history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM purchase_invoices WHERE purchase_order_id IS NULL) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more purchase invoices have no purchase_order_id "
        "(a genuinely multi-PO invoice) — the M6 schema this downgrades to requires "
        "it NOT NULL and cannot represent this data.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM supplier_credit_notes) OR EXISTS ("
        "  SELECT 1 FROM journal_entries WHERE source_type = 'SUPPLIER_CREDIT_NOTE'"
        ") THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more supplier credit notes exist — the M6 schema "
        "this downgrades to has no credit-note tables or source_type at all.'; "
        "END IF; "
        "END $$;"
    )

    op.drop_table("supplier_credit_allocations")
    op.drop_table("supplier_credit_note_lines")
    op.drop_table("supplier_credit_notes")

    # --- Restore supplier_payments.purchase_invoice_id from allocations ---
    op.add_column(
        "supplier_payments",
        sa.Column(
            "purchase_invoice_id",
            sa.Integer(),
            sa.ForeignKey("purchase_invoices.id"),
            nullable=True,
        ),
    )
    conn.execute(
        sa.text(
            "UPDATE supplier_payments sp SET purchase_invoice_id = spa.purchase_invoice_id "
            "FROM supplier_payment_allocations spa WHERE spa.supplier_payment_id = sp.id"
        )
    )
    op.alter_column("supplier_payments", "purchase_invoice_id", nullable=False)
    op.create_index(
        "ix_supplier_payments_purchase_invoice_id", "supplier_payments", ["purchase_invoice_id"]
    )
    op.drop_table("supplier_payment_allocations")

    op.drop_table("purchase_invoice_receipt_matches")

    op.drop_constraint(
        "ck_purchase_invoices_settlement_bounds", "purchase_invoices", type_="check"
    )
    op.drop_constraint(
        "ck_purchase_invoices_amount_credited_non_negative", "purchase_invoices", type_="check"
    )
    op.drop_constraint(
        "ck_purchase_invoices_amount_paid_non_negative", "purchase_invoices", type_="check"
    )
    op.create_check_constraint(
        "ck_purchase_invoices_amount_paid_bounds",
        "purchase_invoices",
        "amount_paid >= 0 AND amount_paid <= grand_total",
    )
    op.drop_column("purchase_invoices", "amount_credited")
    op.alter_column("purchase_invoices", "purchase_order_id", nullable=False)

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

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )
