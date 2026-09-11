"""The double-entry posting engine, reversal, and reporting queries.

See docs/M4_ACCOUNTING_CORE.md for the full design. The load-bearing rule
followed throughout this file: **never independently compute two sides of
a pair that must match** — a value that has to appear as both a debit and
a credit (e.g. COGS/Inventory on a sale, Inventory/Purchase Clearing on a
receipt) is computed exactly once and reused for both lines, so the entry
balances by construction rather than by two arithmetic paths happening to
agree. The deferred DB trigger (see the M4 migration) is the backstop for
a coding mistake, not the primary correctness mechanism.

Every `post_*_journal` function is called *inline*, inside the same
operational service function that creates the sale/receipt/return/
adjustment it accounts for (see app.modules.sales.service.finalize_sale,
app.modules.purchasing.service.receive_goods/create_purchase_return,
app.modules.inventory.service.create_stock_adjustment), before that
function's own final `db.flush()`. None of them commit. This is what
makes posting atomic with the operational event (M4 task Section 19) —
they share the same uncommitted transaction; if anything after posting
raises, everything (sale, movements, and journal alike) is discarded when
the route handler's session closes without ever calling db.commit().
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting.constants import (
    ACCOUNT_COGS,
    ACCOUNT_INVENTORY,
    ACCOUNT_INVENTORY_ADJUSTMENT_GAIN,
    ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
    ACCOUNT_PURCHASE_CLEARING,
    ACCOUNT_SALES_DISCOUNTS,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_TAX_PAYABLE,
    PAYMENT_METHOD_ACCOUNT_CODE,
)
from app.modules.accounting.models import (
    AUTOMATED_SOURCE_TYPES,
    Account,
    JournalEntry,
    JournalLine,
)
from app.modules.audit import service as audit_service

if TYPE_CHECKING:
    # Import cycle avoidance: sales/purchasing/inventory service modules
    # import THIS module to post accounting entries, so this module must
    # not import them back at runtime — only for type-checking, which
    # never executes these imports.
    from app.modules.inventory.models import StockAdjustment
    from app.modules.purchasing.models import GoodsReceipt, PurchaseReturn
    from app.modules.sales.models import Sale
    from app.modules.sales.service import PaymentInput, _ComputedLine

# Same precision/rounding boundary already established for Weighted
# Average Cost in app/modules/inventory/service.py — reused here rather
# than inventing a second one, so an inventory-valuation journal line and
# the WAC it was computed from are quantized identically, and the
# Inventory GL therefore reconciles to the qty*WAC operational value
# exactly at this precision (docs/M4_ACCOUNTING_CORE.md Section 11).
_LEDGER_QUANTUM = Decimal("0.000001")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_LEDGER_QUANTUM, rounding=ROUND_HALF_UP)


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Local duplicate of auth.service.enforce_store_access, for
    service-layer callers/tests that call these functions directly
    without going through the route layer — the exact pattern already
    used by app.modules.purchasing.service._enforce_store_access."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access this {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


@dataclass(frozen=True)
class _LineSpec:
    account_code: str
    debit: Decimal
    credit: Decimal
    product_id: int | None = None
    description: str | None = None


def _debit(
    account_code: str,
    amount: Decimal,
    *,
    product_id: int | None = None,
    description: str | None = None,
) -> _LineSpec:
    return _LineSpec(
        account_code,
        debit=amount,
        credit=Decimal("0"),
        product_id=product_id,
        description=description,
    )


def _credit(
    account_code: str,
    amount: Decimal,
    *,
    product_id: int | None = None,
    description: str | None = None,
) -> _LineSpec:
    return _LineSpec(
        account_code,
        debit=Decimal("0"),
        credit=amount,
        product_id=product_id,
        description=description,
    )


def _resolve_accounts(db: Session, codes: set[str]) -> dict[str, Account]:
    rows = db.execute(select(Account).where(Account.code.in_(codes))).scalars().all()
    found = {a.code: a for a in rows}
    missing = codes - set(found)
    if missing:
        # Only possible if the seed migration hasn't run or a code
        # constant drifted from the seeded rows — a configuration bug,
        # not a user-facing condition.
        raise ConflictError(
            f"Missing chart-of-accounts entries for codes: {sorted(missing)}",
            error_code="ACCOUNTS_NOT_SEEDED",
        )
    return found


def _generate_journal_number(store_id: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"JE{store_id}-{timestamp}-{secrets.token_hex(3).upper()}"


def _post_journal(
    db: Session,
    *,
    store_id: int,
    posting_date: date,
    source_type: str,
    source_id: int | None,
    memo: str | None,
    created_by: int | None,
    lines: list[_LineSpec],
    entry_type: str = "STANDARD",
    reversal_of_id: int | None = None,
) -> JournalEntry:
    if not lines:
        raise ValidationAppError(
            "A journal entry must have at least one line", error_code="EMPTY_JOURNAL_ENTRY"
        )
    accounts = _resolve_accounts(db, {line.account_code for line in lines})

    entry = JournalEntry(
        journal_number=_generate_journal_number(store_id),
        store_id=store_id,
        posting_date=posting_date,
        entry_type=entry_type,
        source_type=source_type,
        source_id=source_id,
        reversal_of_id=reversal_of_id,
        memo=memo,
        created_by=created_by,
    )
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        # Hitting uq_journal_entries_source here means this exact
        # (source_type, source_id) pair already has a STANDARD entry.
        # Unlike a client_transaction_id race (a value a client can
        # legitimately resubmit), source_id is a freshly-minted primary
        # key from a row created earlier in this very transaction — a
        # collision means a real bug (double-posting), not a retry to
        # recover from. Let it propagate and roll back the whole
        # operation rather than silently returning "the winner."
        raise

    for line in lines:
        db.add(
            JournalLine(
                journal_entry_id=entry.id,
                account_id=accounts[line.account_code].id,
                debit=line.debit,
                credit=line.credit,
                product_id=line.product_id,
                description=line.description,
            )
        )
    db.flush()
    return entry


# --- Sales -----------------------------------------------------------------


def post_sale_journal(
    db: Session,
    *,
    sale: "Sale",
    computed_lines: "list[_ComputedLine]",
    payments: "list[PaymentInput]",
    created_by: int | None,
) -> JournalEntry:
    """docs/M4_ACCOUNTING_CORE.md Section 6 has the full worked derivation
    and the algebraic proof that this entry always balances exactly.

    Debits: one line per payment (by method's clearing/cash account) +
    Sales Discounts (if any) + COGS.
    Credits: Cash on Hand for change given (if any) + Sales Revenue
    (gross, pre-discount) + Tax Payable (if any) + Inventory.

    COGS/Inventory is computed once from computed_lines (the exact same
    quantity/unit_cost_at_sale values already used to post each SALE
    inventory movement) and used for both sides of that pair — see the
    module docstring's "never independently compute two sides" rule.
    """
    lines: list[_LineSpec] = []
    for payment in payments:
        lines.append(
            _debit(
                PAYMENT_METHOD_ACCOUNT_CODE[payment.payment_method],
                payment.amount,
                description=f"{payment.payment_method} tendered",
            )
        )
    if sale.discount_total > 0:
        lines.append(_debit(ACCOUNT_SALES_DISCOUNTS, sale.discount_total))
    if sale.change_due and sale.change_due > 0:
        lines.append(
            _credit(
                PAYMENT_METHOD_ACCOUNT_CODE["CASH"], sale.change_due, description="Change given"
            )
        )
    if sale.subtotal > 0:
        lines.append(_credit(ACCOUNT_SALES_REVENUE, sale.subtotal))
    if sale.tax_total > 0:
        lines.append(_credit(ACCOUNT_TAX_PAYABLE, sale.tax_total))

    cogs_total = Decimal("0")
    for computed in computed_lines:
        cogs_total += _quantize(computed.quantity * computed.unit_cost)
    if cogs_total > 0:
        lines.append(_debit(ACCOUNT_COGS, cogs_total, description=f"Sale {sale.sale_number}"))
        lines.append(_credit(ACCOUNT_INVENTORY, cogs_total, description=f"Sale {sale.sale_number}"))

    return _post_journal(
        db,
        store_id=sale.store_id,
        posting_date=sale.completed_at.date() if sale.completed_at else date.today(),
        source_type="SALE",
        source_id=sale.id,
        memo=f"Sale {sale.sale_number}",
        created_by=created_by,
        lines=lines,
    )


# --- Purchasing --------------------------------------------------------------


def post_goods_receipt_journal(
    db: Session,
    *,
    goods_receipt: "GoodsReceipt",
    received_lines: list[tuple[Decimal, Decimal]],  # (quantity_received, unit_cost) per line
    created_by: int | None,
) -> JournalEntry | None:
    """Dr Inventory / Cr Purchase Clearing for the value actually received
    (docs/M4_ACCOUNTING_CORE.md Section 7). The amount is computed once
    from the same (quantity, unit_cost) pairs already used to post each
    PURCHASE_RECEIPT inventory movement, and used for both lines.

    Returns None and posts nothing if the total received value is zero
    (a supplier giving stock away for free is a legitimate, explicitly
    supported M3 scenario — a zero-value event has no financial ledger
    effect to record, and the DB balance trigger would reject a
    zero-total entry anyway; the operational receipt must still succeed,
    it just has no accounting consequence)."""
    total = Decimal("0")
    for quantity, unit_cost in received_lines:
        total += _quantize(quantity * unit_cost)
    if total <= 0:
        return None
    lines = [
        _debit(ACCOUNT_INVENTORY, total, description=f"Goods receipt {goods_receipt.id}"),
        _credit(ACCOUNT_PURCHASE_CLEARING, total, description=f"Goods receipt {goods_receipt.id}"),
    ]
    return _post_journal(
        db,
        store_id=goods_receipt.store_id,
        posting_date=goods_receipt.received_date,
        source_type="PURCHASE_RECEIPT",
        source_id=goods_receipt.id,
        memo=f"Goods receipt {goods_receipt.id}",
        created_by=created_by,
        lines=lines,
    )


def post_purchase_return_journal(
    db: Session,
    *,
    purchase_return: "PurchaseReturn",
    returned_lines: list[tuple[Decimal, Decimal]],  # (quantity, unit_cost_at_movement) per line
    created_by: int | None,
) -> JournalEntry | None:
    """Dr Purchase Clearing / Cr Inventory. `unit_cost_at_movement` here is
    the SAME current-WAC-at-return-time value M3 already uses for the
    PURCHASE_RETURN inventory movement — this journal never uses a
    different valuation than the operational movement it accounts for
    (docs/M4_ACCOUNTING_CORE.md Section 8, and the M3 known limitation on
    purchase-return cost basis applies unchanged here).

    Returns None and posts nothing if the returned value is zero (the
    product's current WAC is zero) — same zero-value reasoning as
    post_goods_receipt_journal above."""
    total = Decimal("0")
    for quantity, unit_cost in returned_lines:
        total += _quantize(quantity * unit_cost)
    if total <= 0:
        return None
    lines = [
        _debit(
            ACCOUNT_PURCHASE_CLEARING, total, description=f"Purchase return {purchase_return.id}"
        ),
        _credit(ACCOUNT_INVENTORY, total, description=f"Purchase return {purchase_return.id}"),
    ]
    return _post_journal(
        db,
        store_id=purchase_return.store_id,
        posting_date=purchase_return.return_date,
        source_type="PURCHASE_RETURN",
        source_id=purchase_return.id,
        memo=f"Purchase return {purchase_return.id}",
        created_by=created_by,
        lines=lines,
    )


# --- Inventory adjustments --------------------------------------------------


def post_stock_adjustment_journal(
    db: Session,
    *,
    stock_adjustment: "StockAdjustment",
    unit_cost: Decimal,  # the same product.current_cost used for the movement
    created_by: int | None,
) -> JournalEntry | None:
    """Positive quantity_delta (found more stock than recorded):
    Dr Inventory / Cr Inventory Adjustment Gain.
    Negative (found less): Dr Inventory Shrinkage Expense / Cr Inventory.
    (docs/M4_ACCOUNTING_CORE.md Section 9.)

    Returns None and posts nothing if the product's current cost is zero
    — the adjustment still happens operationally (this is a real,
    explicitly supported scenario: correcting the counted quantity of a
    product that has always been zero-cost), it simply has no dollar
    value to record."""
    amount = _quantize(abs(stock_adjustment.quantity_delta) * unit_cost)
    if amount <= 0:
        return None
    if stock_adjustment.quantity_delta > 0:
        lines = [
            _debit(
                ACCOUNT_INVENTORY, amount, description=f"Stock adjustment {stock_adjustment.id}"
            ),
            _credit(
                ACCOUNT_INVENTORY_ADJUSTMENT_GAIN,
                amount,
                description=f"Stock adjustment {stock_adjustment.id}",
            ),
        ]
    else:
        lines = [
            _debit(
                ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
                amount,
                description=f"Stock adjustment {stock_adjustment.id}",
            ),
            _credit(
                ACCOUNT_INVENTORY, amount, description=f"Stock adjustment {stock_adjustment.id}"
            ),
        ]
    return _post_journal(
        db,
        store_id=stock_adjustment.store_id,
        posting_date=(
            stock_adjustment.created_at.date() if stock_adjustment.created_at else date.today()
        ),
        source_type="STOCK_ADJUSTMENT",
        source_id=stock_adjustment.id,
        memo=f"Stock adjustment {stock_adjustment.id}",
        created_by=created_by,
        lines=lines,
    )


# --- Reversal ----------------------------------------------------------------


def reverse_journal_entry(
    db: Session,
    *,
    journal_entry_id: int,
    reason: str,
    reversed_by: int | None,
    caller_store_id: int | None,
) -> JournalEntry:
    """Creates a new REVERSAL entry with every debit/credit swapped from
    the original — never mutates the original row (it structurally
    cannot: UPDATE is revoked from erp_app on both ledger tables).

    Serialization: two concurrent reversal requests for the same entry
    are serialized with `pg_advisory_xact_lock(journal_entry_id)`, not
    `SELECT ... FOR UPDATE` — a Postgres row lock requires UPDATE
    privilege on the table, which erp_app deliberately does not have
    here (verified: attempting `SELECT ... FOR UPDATE` as erp_app against
    a table with UPDATE revoked raises "permission denied", not just a
    lock — confirmed manually against a scratch table before choosing
    this approach). An advisory lock needs no table privileges and is
    released automatically at transaction end either way.

    Idempotent: reversing an already-reversed entry returns the existing
    reversal rather than creating a second one.

    Automated-source block (docs/M4_HARDENING_AUDIT.md Section 1 —
    CRITICAL finding, fixed here): an entry whose source_type is one of
    the five automatically-posted types (SALE, PURCHASE_RECEIPT,
    PURCHASE_RETURN, SALE_RETURN, STOCK_ADJUSTMENT) is refused with
    OPERATIONAL_REVERSAL_REQUIRED. Reversing one of these is
    *accounting-only*: it flips revenue/COGS/cash/inventory-value lines
    but does not touch the Sale row, inventory quantity, payment record,
    or WAC that produced them — a live test against a running instance
    proved this creates a real, permanent inventory-GL-vs-operational
    discrepancy. Until an operational void/return workflow exists that
    atomically reverses (operational state + inventory + payment/refund
    + accounting + audit) together, no automated entry is reversible
    through this function — only a MANUAL entry (source_type='MANUAL',
    not currently created by any endpoint — see models.py) can be. This
    keeps the reversal *mechanism* itself real and tested rather than
    removing it, per the explicit "do not silently remove functionality"
    instruction — it is simply not a legal operation against any entry
    the system currently knows how to produce automatically.
    """
    entry = db.get(JournalEntry, journal_entry_id)
    if entry is None:
        raise NotFoundError(f"Journal entry {journal_entry_id} not found")
    _enforce_store_access(caller_store_id, entry.store_id, "journal entry")

    if entry.source_type in AUTOMATED_SOURCE_TYPES:
        raise ConflictError(
            f"Journal entry {entry.id} was posted automatically from a "
            f"{entry.source_type} — reversing it here would correct the accounting "
            "without undoing the operational transaction (inventory, payment, stock) "
            "that produced it, silently diverging the two. An operational void/return "
            "workflow for this source type does not exist yet; reverse the operational "
            "transaction through that workflow once it does, not the journal alone.",
            error_code="OPERATIONAL_REVERSAL_REQUIRED",
        )

    db.execute(select(func.pg_advisory_xact_lock(entry.id)))

    if entry.entry_type == "REVERSAL":
        raise ConflictError("Cannot reverse a reversal entry", error_code="CANNOT_REVERSE_REVERSAL")
    existing_reversal = db.execute(
        select(JournalEntry).where(JournalEntry.reversal_of_id == entry.id)
    ).scalar_one_or_none()
    if existing_reversal is not None:
        return existing_reversal

    original_lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )

    if not original_lines:
        raise ConflictError(
            f"Journal entry {entry.id} has no lines to reverse", error_code="EMPTY_JOURNAL_ENTRY"
        )

    reversal = JournalEntry(
        journal_number=_generate_journal_number(entry.store_id),
        store_id=entry.store_id,
        posting_date=date.today(),
        entry_type="REVERSAL",
        source_type=entry.source_type,
        source_id=entry.source_id,
        reversal_of_id=entry.id,
        memo=reason,
        created_by=reversed_by,
    )
    db.add(reversal)
    db.flush()

    for line in original_lines:
        db.add(
            JournalLine(
                journal_entry_id=reversal.id,
                account_id=line.account_id,
                debit=line.credit,
                credit=line.debit,
                product_id=line.product_id,
                description=line.description,
            )
        )
    db.flush()

    audit_service.log_event(
        db,
        user_id=reversed_by,
        action="JOURNAL_ENTRY_REVERSED",
        entity_type="journal_entry",
        entity_id=entry.id,
        after={"reversal_entry_id": reversal.id, "reason": reason},
    )
    db.flush()
    return reversal


# --- Reads / reports -----------------------------------------------------


def get_journal_entry(db: Session, journal_entry_id: int) -> JournalEntry:
    entry = db.get(JournalEntry, journal_entry_id)
    if entry is None:
        raise NotFoundError(f"Journal entry {journal_entry_id} not found")
    return entry


def list_journal_entries(
    db: Session,
    *,
    store_id: int | None = None,
    source_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JournalEntry]:
    query = (
        select(JournalEntry)
        .order_by(JournalEntry.posting_date.desc(), JournalEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(JournalEntry.store_id == store_id)
    if source_type is not None:
        query = query.where(JournalEntry.source_type == source_type)
    if date_from is not None:
        query = query.where(JournalEntry.posting_date >= date_from)
    if date_to is not None:
        query = query.where(JournalEntry.posting_date <= date_to)
    return list(db.execute(query).scalars().all())


def list_journal_lines(db: Session, journal_entry_id: int) -> list[JournalLine]:
    return list(
        db.execute(
            select(JournalLine)
            .where(JournalLine.journal_entry_id == journal_entry_id)
            .order_by(JournalLine.id)
        )
        .scalars()
        .all()
    )


def list_accounts(db: Session, *, active_only: bool = False) -> list[Account]:
    query = select(Account).order_by(Account.code)
    if active_only:
        query = query.where(Account.is_active.is_(True))
    return list(db.execute(query).scalars().all())


# --- Reports ---------------------------------------------------------------
#
# Every report below aggregates directly from journal_lines/journal_entries
# (M4 task Section 26: "Reports must use posted journal entries. Do not
# calculate P&L from sales tables directly."). There is no DRAFT status to
# filter out (docs/M4_ACCOUNTING_CORE.md Section 6: nothing is ever
# inserted except a fully valid, DB-trigger-verified, already-posted
# entry), and a reversed original is deliberately NOT excluded — both it
# and its REVERSAL entry are summed like anything else, which nets their
# combined contribution to exactly zero. Excluding the original while
# keeping the reversal would leave the reversal's lines unbalanced in the
# report; summing everything is the standard, simplest, and only correct
# treatment (see the module docstring for the full reasoning).


@dataclass(frozen=True)
class TrialBalanceRow:
    account_code: str
    account_name: str
    account_type: str
    normal_balance: str
    total_debit: Decimal
    total_credit: Decimal


def trial_balance(
    db: Session,
    *,
    store_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[TrialBalanceRow]:
    query = (
        select(
            Account.code,
            Account.name,
            Account.account_type,
            Account.normal_balance,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .select_from(Account)
        .join(JournalLine, JournalLine.account_id == Account.id, isouter=True)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id, isouter=True)
        .group_by(Account.id)
        .order_by(Account.code)
    )
    query = _apply_entry_filters(query, store_id=store_id, date_from=date_from, date_to=date_to)
    rows = db.execute(query).all()
    return [
        TrialBalanceRow(
            account_code=code,
            account_name=name,
            account_type=account_type,
            normal_balance=normal_balance,
            total_debit=Decimal(total_debit),
            total_credit=Decimal(total_credit),
        )
        for code, name, account_type, normal_balance, total_debit, total_credit in rows
    ]


def _apply_entry_filters(
    query: Any, *, store_id: int | None, date_from: date | None, date_to: date | None
) -> Any:
    # store_id/date filters apply to the JournalEntry side of the outer
    # join — for an account no matching entry touched, the join columns
    # are NULL and the filter would incorrectly drop the whole account
    # row, so each filter is OR'd with "no entry joined at all."
    if store_id is not None:
        query = query.where((JournalEntry.store_id == store_id) | (JournalEntry.id.is_(None)))
    if date_from is not None:
        query = query.where((JournalEntry.posting_date >= date_from) | (JournalEntry.id.is_(None)))
    if date_to is not None:
        query = query.where((JournalEntry.posting_date <= date_to) | (JournalEntry.id.is_(None)))
    return query


@dataclass(frozen=True)
class ProfitAndLoss:
    store_id: int | None
    date_from: date | None
    date_to: date | None
    net_sales: Decimal
    cogs: Decimal
    gross_profit: Decimal
    other_income: Decimal
    operating_expenses: Decimal
    net_income: Decimal


def profit_and_loss(
    db: Session,
    *,
    store_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> ProfitAndLoss:
    rows = {
        row.account_code: row
        for row in trial_balance(db, store_id=store_id, date_from=date_from, date_to=date_to)
    }

    def net_credit(code: str) -> Decimal:
        row = rows.get(code)
        if row is None:
            return Decimal("0")
        return row.total_credit - row.total_debit

    def net_debit(code: str) -> Decimal:
        return -net_credit(code)

    net_sales = net_credit(ACCOUNT_SALES_REVENUE) - net_debit(ACCOUNT_SALES_DISCOUNTS)
    cogs = net_debit(ACCOUNT_COGS)
    gross_profit = net_sales - cogs
    other_income = net_credit(ACCOUNT_INVENTORY_ADJUSTMENT_GAIN)
    operating_expenses = net_debit(ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE)
    net_income = gross_profit + other_income - operating_expenses

    return ProfitAndLoss(
        store_id=store_id,
        date_from=date_from,
        date_to=date_to,
        net_sales=net_sales,
        cogs=cogs,
        gross_profit=gross_profit,
        other_income=other_income,
        operating_expenses=operating_expenses,
        net_income=net_income,
    )


@dataclass(frozen=True)
class InventoryReconciliationRow:
    store_id: int
    gl_inventory_balance: Decimal
    operational_valuation: Decimal
    discrepancy: Decimal


def inventory_reconciliation(
    db: Session, *, store_id: int | None = None, as_of: date | None = None
) -> list[InventoryReconciliationRow]:
    """Compares the Inventory account's GL balance (from posted journal
    lines) against the live operational valuation
    (Σ products.current_qty_on_hand * products.current_cost) per store —
    the M4 task Section 13 invariant. They are expected to match exactly
    to the ledger quantum (Decimal('0.000001')): every posting function in
    this module derives its Inventory-account amount from the identical
    (quantity, unit_cost) values already used to update current_qty_on_hand
    /current_cost via app.modules.inventory.service.record_movement — see
    docs/M4_ACCOUNTING_CORE.md Section 11 for the full proof.
    """
    from app.modules.products.models import Product

    inventory_account = _resolve_accounts(db, {ACCOUNT_INVENTORY})[ACCOUNT_INVENTORY]

    gl_query = (
        select(
            JournalEntry.store_id,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalLine, JournalLine.journal_entry_id == JournalEntry.id)
        .where(JournalLine.account_id == inventory_account.id)
        .group_by(JournalEntry.store_id)
    )
    if store_id is not None:
        gl_query = gl_query.where(JournalEntry.store_id == store_id)
    if as_of is not None:
        gl_query = gl_query.where(JournalEntry.posting_date <= as_of)
    gl_by_store: dict[int, Decimal] = {
        row[0]: Decimal(row[1]) - Decimal(row[2]) for row in db.execute(gl_query).all()
    }

    val_query = select(
        Product.store_id,
        func.coalesce(func.sum(Product.current_qty_on_hand * Product.current_cost), 0),
    ).group_by(Product.store_id)
    if store_id is not None:
        val_query = val_query.where(Product.store_id == store_id)
    valuation_by_store: dict[int, Decimal] = {
        row[0]: Decimal(row[1]) for row in db.execute(val_query).all()
    }

    all_store_ids = set(gl_by_store) | set(valuation_by_store)
    results = []
    for sid in sorted(all_store_ids):
        gl = gl_by_store.get(sid, Decimal("0"))
        val = _quantize(valuation_by_store.get(sid, Decimal("0")))
        results.append(
            InventoryReconciliationRow(
                store_id=sid,
                gl_inventory_balance=gl,
                operational_valuation=val,
                discrepancy=gl - val,
            )
        )
    return results
