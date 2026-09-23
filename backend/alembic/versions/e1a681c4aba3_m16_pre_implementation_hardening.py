"""m16 pre-implementation hardening

Revision ID: e1a681c4aba3
Revises: db482a11ee31
Create Date: 2026-09-22 00:00:00.000000

See docs/M16_DESIGN.md "Phase 0 — pre-implementation hardening". Adds:

- `sale_returns.return_date` — nullable, the same caller-supplied business
  date already passed to accounting_service.post_sale_return_journal's
  posting_date, now also persisted on the operational row itself so
  reports/service.py can bucket a return by the SAME date the GL uses
  instead of `created_at` (fixes a real operational-vs-GL reporting
  divergence for backdated returns). NOT backfilled for pre-M16 rows —
  application code coalesces to created_at's date for any row where this
  is NULL.
- `ix_sales_shift_id` / `ix_sale_returns_shift_id` — M15 added
  `sales.shift_id`/`sale_returns.shift_id` without an index, breaking
  this codebase's own established per-FK-index convention; both columns
  are actively queried on every shift close
  (app.modules.shifts.service._compute_expected_cash).
- `supplier_payment_reversals` / `supplier_credit_note_reversals` — new
  tables recording that a SupplierPayment/SupplierCreditNote was
  reversed (mirrors `payroll_reversals` exactly: an append-only row, not
  a status flip on the original).
- Widens `ck_journal_entries_source_type` to accept
  `SUPPLIER_PAYMENT_REVERSAL` / `SUPPLIER_CREDIT_NOTE_REVERSAL`.
- One new permission (`ap.reverse`), granted to Admin (implicitly, via
  `list(ALL_PERMISSIONS)`) and Manager (explicit) — mirrors
  db482a11ee31's own `_NEW_PERMISSIONS`/`_ROLE_GRANTS`/
  `ON CONFLICT DO UPDATE ... RETURNING` pattern.

No changes to any M0-M15 table's existing columns, constraints, or data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a681c4aba3"
down_revision: Union[str, None] = "db482a11ee31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The full source_type set as it existed immediately before this migration
# (accounting/models.py's AUTOMATED_SOURCE_TYPES + "MANUAL" as of M15) --
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
    "CASH_SHIFT_VARIANCE",
    "MANUAL",
)
_NEW_SOURCE_TYPES = _OLD_SOURCE_TYPES[:-1] + (
    "SUPPLIER_PAYMENT_REVERSAL",
    "SUPPLIER_CREDIT_NOTE_REVERSAL",
    "MANUAL",
)

_NEW_PERMISSIONS = [
    ("ap.reverse", "Reverse a supplier payment or credit note with a compensating entry"),
]

_ROLE_GRANTS = {
    "Admin": [code for code, _ in _NEW_PERMISSIONS],
    "Manager": [code for code, _ in _NEW_PERMISSIONS],
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- sale_returns.return_date -----------------------------------------
    op.add_column("sale_returns", sa.Column("return_date", sa.Date(), nullable=True))

    # --- Missing shift_id indexes (M15 gap) --------------------------------
    op.create_index("ix_sales_shift_id", "sales", ["shift_id"])
    op.create_index("ix_sale_returns_shift_id", "sale_returns", ["shift_id"])

    # --- Widen the journal source_type CHECK -------------------------------
    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_NEW_SOURCE_TYPES) + "')",
    )

    # --- supplier_payment_reversals -----------------------------------------
    op.create_table(
        "supplier_payment_reversals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_payment_id",
            sa.Integer(),
            sa.ForeignKey("supplier_payments.id"),
            nullable=False,
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
    )
    op.create_unique_constraint(
        "uq_supplier_payment_reversals_payment",
        "supplier_payment_reversals",
        ["supplier_payment_id"],
    )

    # --- supplier_credit_note_reversals -------------------------------------
    op.create_table(
        "supplier_credit_note_reversals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_credit_note_id",
            sa.Integer(),
            sa.ForeignKey("supplier_credit_notes.id"),
            nullable=False,
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
    )
    op.create_unique_constraint(
        "uq_supplier_credit_note_reversals_credit_note",
        "supplier_credit_note_reversals",
        ["supplier_credit_note_id"],
    )

    # --- RBAC: one new permission -------------------------------------------
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

    # Guard FIRST, before any destructive step (same discipline every
    # prior milestone's downgrade follows): real M16 correction history
    # the M15 schema cannot represent must refuse the downgrade loudly,
    # not silently destroy it.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM supplier_payment_reversals) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more supplier_payment_reversals rows exist -- dropping "
        "this table would silently discard real payment-correction history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM supplier_credit_note_reversals) THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: one or more supplier_credit_note_reversals rows exist -- "
        "dropping this table would silently discard real credit-note-correction history.'; "
        "END IF; "
        "END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM journal_entries WHERE source_type IN "
        "  ('SUPPLIER_PAYMENT_REVERSAL', 'SUPPLIER_CREDIT_NOTE_REVERSAL')) "
        "THEN "
        "RAISE EXCEPTION "
        "'Cannot downgrade: journal_entries has M16 reversal rows that would violate the "
        "narrower ck_journal_entries_source_type constraint.'; "
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

    op.drop_table("supplier_credit_note_reversals")
    op.drop_table("supplier_payment_reversals")

    op.drop_constraint("ck_journal_entries_source_type", "journal_entries", type_="check")
    op.create_check_constraint(
        "ck_journal_entries_source_type",
        "journal_entries",
        "source_type IN ('" + "', '".join(_OLD_SOURCE_TYPES) + "')",
    )

    op.drop_index("ix_sale_returns_shift_id", "sale_returns")
    op.drop_index("ix_sales_shift_id", "sales")

    # No guard needed for return_date: it is purely additive/optional and
    # duplicates a value already permanently recorded on the posted
    # JournalEntry.posting_date for any return that posted a journal (the
    # vast majority) -- the worst case on downgrade is losing the
    # operational-side convenience copy for zero-value returns that never
    # posted a journal at all, which by definition had no dollar impact.
    op.drop_column("sale_returns", "return_date")
