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
#
# MANUAL (docs/M4_HARDENING_AUDIT.md Section 1): the one source_type NOT
# produced by an operational service function. It exists solely so the
# reversal mechanism has a legitimate target — every one of the other
# five values is posted automatically alongside a real inventory/payment/
# stock change that reversal does NOT undo, so reversing any of them
# through this journal-only endpoint would silently diverge the
# operational and accounting ledgers (a real divergence, proven live
# against a running instance during the M4 hardening audit). No endpoint
# currently creates a MANUAL entry (accounting.post is still reserved,
# unused) — the value exists so `reverse_journal_entry`'s automated-
# source block (service.py `_AUTOMATED_SOURCE_TYPES`) has something to
# permit, and so the reversal code path stays provably correct rather
# than untestable dead code.
# M6 adds three more automated source types (docs/M6_AP_VENDOR_ACCOUNTING.md
# "AP accounting"): PURCHASE_INVOICE (posted when a supplier invoice is
# matched/posted — clears Purchase Clearing, establishes Accounts
# Payable), PURCHASE_INVOICE_VOID (the operational-void counterpart —
# never reversed through reverse_journal_entry's generic mechanism, same
# reasoning as every other automated type below), and SUPPLIER_PAYMENT
# (posted when a supplier payment is recorded — reduces Accounts
# Payable). Each is produced by its own dedicated app.modules.ap.service
# function, alongside the real operational state change it accounts for
# — never posted standalone.
# M7 adds SUPPLIER_CREDIT_NOTE (docs/M7_ADVANCED_AP_SETTLEMENT.md Section
# 13): posted when a supplier credit note is created — reduces Accounts
# Payable against either Inventory (GOODS_RETURN) or Purchase Discounts
# (COMMERCIAL_DISCOUNT). A credit note has no operational void in M7 (see
# the design doc's "Deferred" section), so unlike PURCHASE_INVOICE it has
# no *_VOID counterpart yet — it is still included here (not left
# reversible through the generic mechanism) because reversing it here
# would not undo amount_allocated/amount_credited on the invoices it
# touched, the same divergence risk every other automated type guards
# against.
AUTOMATED_SOURCE_TYPES = (
    "SALE",
    "PURCHASE_RECEIPT",
    "PURCHASE_RETURN",
    "SALE_RETURN",
    "STOCK_ADJUSTMENT",
    "PURCHASE_INVOICE",
    "PURCHASE_INVOICE_VOID",
    "SUPPLIER_PAYMENT",
    "SUPPLIER_CREDIT_NOTE",
    # M8 (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 9"):
    # INTER_STORE_TRANSFER_SHIP posts once per transfer (shipping is a
    # single event — Design Decision 8); INTER_STORE_TRANSFER_RECEIVE
    # posts once per InterStoreTransferReceipt (a transfer may have
    # several). Both blocked from generic reversal for the same reason
    # every automated type is: reversing the journal alone would not
    # undo the real inventory movement (TRANSFER_OUT/TRANSFER_IN) or the
    # transfer's own shipped_quantity/received_quantity running totals.
    "INTER_STORE_TRANSFER_SHIP",
    "INTER_STORE_TRANSFER_RECEIVE",
    # M10 (docs/M10_DESIGN.md Section 10): PAYROLL_POSTING is posted once
    # per PayrollPeriod (never per employee — "one real financial event,
    # one entry", the same rule SUPPLIER_PAYMENT follows even though it
    # aggregates many allocations) by app.modules.payroll.service's
    # posting transition. PAYROLL_REVERSAL exists so a posted period can
    # be reversed by a dedicated function (never the generic
    # reverse_journal_entry — same reasoning as every other automated
    # type above: a bare journal reversal would not undo
    # PayrollPeriod.status or the real pay obligation it represents).
    "PAYROLL_POSTING",
    "PAYROLL_REVERSAL",
    # M15 (docs/M15_DESIGN.md "GL treatment"): posted once per shift close,
    # only when the physical count differs from expected cash (a zero
    # variance posts nothing — no financial event occurred). Blocked from
    # generic reverse_journal_entry for the same reason every automated
    # type above is: a bare journal reversal would not undo
    # CashierShift.status/variance_amount or the real cash-handling event
    # it represents. There is deliberately no CASH_SHIFT_VARIANCE_REVERSAL
    # counterpart yet — M15 builds no shift-reopen/correction workflow
    # (docs/M15_DESIGN.md "What M15 deliberately did not build"), so
    # nothing in this milestone would ever call one.
    # (M16 verified this entry is already present and the generic
    # reverse_journal_entry gate already correctly refuses it — see
    # docs/M16_DESIGN.md "Phase 0 item 2" for the regression test proving
    # this invariant; the M16 discovery audit's claim that this was
    # missing did not hold up against the actual code.)
    "CASH_SHIFT_VARIANCE",
    # M16 (docs/M16_DESIGN.md "AP payment/credit-note correction path"):
    # SUPPLIER_PAYMENT_REVERSAL / SUPPLIER_CREDIT_NOTE_REVERSAL are posted
    # once per reversal by app.modules.ap.service's dedicated
    # reverse_supplier_payment/reverse_supplier_credit_note functions --
    # never through reverse_journal_entry's generic mechanism, same
    # reasoning as every automated type above: a bare journal reversal
    # would not undo the invoice(s)' amount_paid/amount_credited or
    # create the SupplierPaymentReversal/SupplierCreditNoteReversal row
    # that records the correction. Reusing `source_id` = the ORIGINAL
    # payment/credit note's id (mirroring PAYROLL_REVERSAL's own
    # source_id=period.id convention) means the existing partial unique
    # index on (source_type, source_id) also gives, for free, "this
    # payment/credit note can only ever be reversed once" at the DB
    # level -- the same backstop
    # app.modules.ap.models.SupplierPaymentReversal's own
    # UNIQUE(supplier_payment_id) constraint already provides at the
    # domain-row level.
    "SUPPLIER_PAYMENT_REVERSAL",
    "SUPPLIER_CREDIT_NOTE_REVERSAL",
)
SOURCE_TYPES = AUTOMATED_SOURCE_TYPES + ("MANUAL",)
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
