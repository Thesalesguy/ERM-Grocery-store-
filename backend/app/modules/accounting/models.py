"""ORM models for the double-entry accounting core: Chart of Accounts and
the immutable journal (JournalEntry/JournalLine).

See docs/M4_ACCOUNTING_CORE.md for the full design. Summary of the load-
bearing decisions (each documented in full in that doc):

- `Account` is global reference data (like `ProductCategory`/`TaxRate`/
  `Supplier`), code-seeded by migration — not created through the API in
  this milestone. The store dimension for accounting lives on
  `JournalEntry.store_id`, not on the account.
- `JournalLine.debit`/`credit` are `Numeric(14, 6)` — NOT the app's usual
  2-decimal money precision. This reuses the WAC quantum
  (`Decimal("0.000001")`, `app/modules/inventory/service.py`) already
  established for cost precision, so an inventory-valuation journal line
  never needs a second, incompatible rounding rule. Money-side amounts
  (already-exact 2dp Sale values) round-trip losslessly into 6dp.
- There is deliberately no mutable `status` column. `UPDATE`/`DELETE` are
  revoked from the application's runtime role on both tables in this
  migration (extending the M1 privilege-model pattern used for
  `audit_logs`/`inventory_movements`), so a POSTED->REVERSED status flip
  would be a column that can never actually be updated — a lie waiting
  to happen. Instead `entry_type` is fixed forever at insert
  ('STANDARD' or 'REVERSAL'), and "was this entry reversed" is a derived
  fact: does a REVERSAL-type entry exist whose `reversal_of_id` points at
  it? Never a stored one.
- Balance (`SUM(debit) = SUM(credit)`), non-zero, and non-empty are
  enforced by a deferred Postgres constraint trigger on `journal_lines`
  (see the migration), not by Python alone — belt-and-suspenders, but the
  DB is the actual backstop.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

ACCOUNT_TYPES = ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE")
NORMAL_BALANCES = ("DEBIT", "CREDIT")

# Every automatically-generated journal entry names the operational event
# that produced it. SALE_RETURN is included even though no operational
# sale-return workflow exists yet (docs/M4_ACCOUNTING_CORE.md Section 2:
# SaleReturn/SaleReturnItem have existed as ORM models since M1, but no
# service function or endpoint was ever built in M2 or M3) — so the schema
# is ready for it, documented as a known gap rather than silently omitted.
SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
)
ENTRY_TYPES = ("STANDARD", "REVERSAL")


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('" + "', '".join(ACCOUNT_TYPES) + "')",
            name="ck_accounts_type",
        ),
        CheckConstraint(
            "normal_balance IN ('" + "', '".join(NORMAL_BALANCES) + "')",
            name="ck_accounts_normal_balance",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False)
    normal_balance: Mapped[str] = mapped_column(String(10), nullable=False)
    # True for every account seeded by migration (all of them, in M4) —
    # reserved so a future admin-managed custom account can be
    # distinguished from a system account application code depends on by
    # stable code (see accounting/constants.py). Nothing in M4 lets a user
    # create or deactivate an account, but the column exists so that
    # extension doesn't require a schema change.
    is_system: Mapped[bool] = mapped_column(nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(500))


class JournalEntry(TimestampMixin, Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('" + "', '".join(ENTRY_TYPES) + "')",
            name="ck_journal_entries_entry_type",
        ),
        CheckConstraint(
            "source_type IN ('" + "', '".join(SOURCE_TYPES) + "')",
            name="ck_journal_entries_source_type",
        ),
        CheckConstraint(
            "(entry_type = 'REVERSAL' AND reversal_of_id IS NOT NULL) OR "
            "(entry_type = 'STANDARD' AND reversal_of_id IS NULL)",
            name="ck_journal_entries_reversal_consistency",
        ),
        # Backstop against duplicate posting for the SAME operational
        # event (docs/M4_ACCOUNTING_CORE.md Section 7/21): the primary
        # defense is the operational layer's own idempotency (a retried
        # finalize_sale/receive_goods/... returns its existing row via its
        # client_transaction_id fast path before posting is ever reached
        # again), so this constraint is normally never hit — it exists so
        # a future code path that posts a second time for the same source
        # row fails loudly at the DB rather than silently duplicating
        # revenue/inventory. A reversal shares the same source, so it is
        # explicitly excluded (partial index).
        Index(
            "uq_journal_entries_source",
            "source_type",
            "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL AND entry_type = 'STANDARD'"),
        ),
        Index("ix_journal_entries_store_posting_date", "store_id", "posting_date"),
        Index("ix_journal_entries_source", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # The business date the transaction happened on (docs/M4_ACCOUNTING_CORE.md
    # Section 16) — distinct from created_at (system insert timestamp,
    # from TimestampMixin). Reports filter/group by this, not created_at.
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(10), nullable=False, default="STANDARD")
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int | None] = mapped_column()
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    memo: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class JournalLine(TimestampMixin, Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0", name="ck_journal_lines_debit_non_negative"),
        CheckConstraint("credit >= 0", name="ck_journal_lines_credit_non_negative"),
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_lines_exactly_one_side",
        ),
        Index("ix_journal_lines_journal_entry_id", "journal_entry_id"),
        Index("ix_journal_lines_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    credit: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    # Optional drill-down metadata — e.g. an Inventory/COGS line can name
    # the product it's about, without making product_id part of the
    # accounting identity (a line about a store-wide tax total has none).
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    description: Mapped[str | None] = mapped_column(String(255))
