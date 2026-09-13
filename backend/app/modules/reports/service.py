"""Management analytics/reporting (M11). Read-only: no function in this
module ever writes to a transactional or GL table. See
docs/M11_DESIGN.md for the full architecture and the exact metric
definitions every function below implements verbatim.

No second accounting truth: financial figures either call the existing
app.modules.accounting.service / app.modules.ap.service functions
directly (Trial Balance, P&L, AP aging/reconciliation, Purchase
Clearing reconciliation) or aggregate straight from the same
authoritative tables those functions themselves read. Nothing here
recomputes GL arithmetic independently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import PAYMENT_METHOD_ACCOUNT_CODE
from app.modules.audit.models import AuditLog
from app.modules.auth.service import CurrentUser
from app.modules.sales.models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem

# --- Section 3 (docs/M11_DESIGN.md): store/company authorization scoping ---


def resolve_authorized_store_ids(
    current_user: CurrentUser, requested_store_ids: Sequence[int] | None
) -> list[int] | None:
    """The one and only place a report resolves "which stores." `None`
    returned means no restriction (every store the caller can ever see —
    for an unrestricted caller, literally every store in the system).
    A list returned means exactly and only those stores, each one
    already authorized. Never returns a store the caller cannot access;
    fails closed with ForbiddenError instead of silently dropping it
    (mirrors enforce_store_access's own fail-closed convention)."""
    if current_user.store_id is None:
        return list(requested_store_ids) if requested_store_ids is not None else None
    if requested_store_ids is None:
        return [current_user.store_id]
    unauthorized = sorted({sid for sid in requested_store_ids if sid != current_user.store_id})
    if unauthorized:
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"view store(s) {unauthorized}",
            error_code="STORE_ACCESS_DENIED",
        )
    return [current_user.store_id]


def _validate_date_range(date_from: date | None, date_to: date | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationAppError(
            "date_from must not be after date_to", error_code="INVALID_DATE_RANGE"
        )


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Division that returns None (never raises, never a misleading 0)
    when the denominator is zero — docs/M11_DESIGN.md Section 5.1's
    explicit zero-division rule."""
    if denominator == 0:
        return None
    return numerator / denominator


def _day_range_bounds(
    date_from: date | None, date_to: date | None
) -> tuple[datetime | None, datetime | None]:
    """A `date_to` filter on a timestamp column must include the whole
    day, not stop at midnight — converts an inclusive [date_from, date_to]
    day range into a half-open [start, end) timestamp range."""
    start = datetime.combine(date_from, datetime.min.time()) if date_from is not None else None
    end = (
        datetime.combine(date_to, datetime.min.time()) + timedelta(days=1)
        if date_to is not None
        else None
    )
    return start, end


# --- Section 5.1 (docs/M11_DESIGN.md): sales & profitability ---------------

# IMPORTANT, verified against the actual running code (not the schema's
# aspirational CHECK constraint): app.modules.sales.service.void_sale
# calls create_sale_return with _is_void=True, but that flag ONLY
# changes the audit-log action string ("VOID_COMPLETED" vs
# "SALE_RETURN_COMPLETED") — it never sets Sale.status to 'VOIDED' (a
# 100%-voided sale ends at status='REFUNDED', identically to an
# ordinary full customer return), and Sale.voided_by/voided_reason are
# never populated by any current code path. 'VOIDED' is a schema value
# with no producer anywhere in this codebase today — a pre-existing
# M1-M5 characteristic, not something M11 may "fix" (that would be
# reopening M0-M10). Financial correctness does not depend on this
# distinction: a void nets to zero revenue through the exact same
# returns-netting math as a full customer return, so nothing is
# double-counted either way. The ONLY reliable way to separately
# identify "this return was actually a same-day void" is the audit
# trail (audit_logs.action='VOID_COMPLETED', entity_type='sale_return',
# entity_id=SaleReturn.id — indexed, append-only, and exactly what
# create_sale_return itself logs) — offered below as a supplementary,
# clearly-labeled breakdown of the returns total, never as a second,
# additive bucket (a void is a return, not returns-plus-something).
_GROSS_SALE_STATUSES = ("COMPLETED", "REFUNDED", "PARTIALLY_REFUNDED")


@dataclass(frozen=True)
class SalesSummary:
    store_ids: list[int] | None
    date_from: date | None
    date_to: date | None
    gross_sales: Decimal
    discounts: Decimal
    returns: Decimal
    void_count: int
    void_amount: Decimal
    net_sales: Decimal
    tax: Decimal
    cogs: Decimal
    gross_profit: Decimal
    gross_margin_percent: Decimal | None
    transaction_count: int
    units_sold: Decimal
    average_transaction_value: Decimal | None


def _apply_sale_scope(
    query: Any, *, store_ids: list[int] | None, start: datetime | None, end: datetime | None
) -> Any:
    if store_ids is not None:
        query = query.where(Sale.store_id.in_(store_ids))
    if start is not None:
        query = query.where(Sale.completed_at >= start)
    if end is not None:
        query = query.where(Sale.completed_at < end)
    return query


def _apply_return_scope(
    query: Any, *, store_ids: list[int] | None, start: datetime | None, end: datetime | None
) -> Any:
    if store_ids is not None:
        query = query.where(SaleReturn.store_id.in_(store_ids))
    if start is not None:
        query = query.where(SaleReturn.created_at >= start)
    if end is not None:
        query = query.where(SaleReturn.created_at < end)
    return query


def sales_summary(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SalesSummary:
    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)

    gross_query = (
        select(
            func.coalesce(
                func.sum(SaleItem.line_total + SaleItem.discount_amount - SaleItem.tax_amount), 0
            ),
            func.coalesce(func.sum(SaleItem.discount_amount), 0),
            func.coalesce(func.sum(SaleItem.tax_amount), 0),
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.unit_cost_at_sale), 0),
            func.coalesce(func.sum(SaleItem.quantity), 0),
            func.count(func.distinct(Sale.id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
    )
    gross_query = _apply_sale_scope(gross_query, store_ids=store_ids, start=start, end=end)
    gross_sales, discounts, tax, gross_cogs, units_gross, txn_count = db.execute(gross_query).one()

    # Returns: joined through to the parent Sale and excluded when that
    # sale was VOIDED (a void's own "return" row is the void mechanism
    # itself, not a customer return event -- see M11_DESIGN.md Section
    # 5.1 and the mutation test proving this exclusion is load-bearing).
    returns_query = (
        select(
            func.coalesce(
                func.sum(
                    SaleReturnItem.unit_price_refunded * SaleReturnItem.quantity
                    - SaleReturnItem.discount_refunded
                    + SaleReturnItem.tax_refunded
                ),
                0,
            ),
            func.coalesce(func.sum(SaleReturnItem.tax_refunded), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            SaleReturnItem.restock.is_(True),
                            SaleReturnItem.quantity * SaleReturnItem.unit_cost_refunded,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(SaleReturnItem.quantity), 0),
        )
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.sale_id)
        .where(Sale.status != "VOIDED")
    )
    returns_query = _apply_return_scope(returns_query, store_ids=store_ids, start=start, end=end)
    returns_total, returns_tax, returned_cogs, units_returned = db.execute(returns_query).one()

    # Supplementary breakdown: how much of the returns total above was a
    # same-day void rather than a genuine customer return, per the audit
    # trail (the only reliable signal — see the module-level note above).
    void_query = (
        select(
            func.count(func.distinct(SaleReturn.id)),
            func.coalesce(
                func.sum(
                    SaleReturnItem.unit_price_refunded * SaleReturnItem.quantity
                    - SaleReturnItem.discount_refunded
                    + SaleReturnItem.tax_refunded
                ),
                0,
            ),
        )
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.sale_id)
        .join(
            AuditLog,
            (AuditLog.entity_type == "sale_return") & (AuditLog.entity_id == SaleReturn.id),
        )
        .where(Sale.status != "VOIDED", AuditLog.action == "VOID_COMPLETED")
    )
    void_query = _apply_return_scope(void_query, store_ids=store_ids, start=start, end=end)
    void_count, void_amount = db.execute(void_query).one()

    gross_sales = Decimal(gross_sales)
    discounts = Decimal(discounts)
    tax = Decimal(tax) - Decimal(returns_tax)
    returns_total = Decimal(returns_total)
    net_sales = gross_sales - returns_total
    cogs = Decimal(gross_cogs) - Decimal(returned_cogs)
    gross_profit = net_sales - cogs
    units_sold = Decimal(units_gross) - Decimal(units_returned)

    return SalesSummary(
        store_ids=store_ids,
        date_from=date_from,
        date_to=date_to,
        gross_sales=gross_sales,
        discounts=discounts,
        returns=returns_total,
        void_count=int(void_count),
        void_amount=Decimal(void_amount),
        net_sales=net_sales,
        tax=tax,
        cogs=cogs,
        gross_profit=gross_profit,
        gross_margin_percent=_safe_ratio(gross_profit * 100, net_sales),
        transaction_count=int(txn_count),
        units_sold=units_sold,
        average_transaction_value=_safe_ratio(net_sales, Decimal(txn_count)),
    )


@dataclass(frozen=True)
class SalesByDimensionRow:
    key: int | str
    label: str
    gross_sales: Decimal
    returns: Decimal
    net_sales: Decimal
    cogs: Decimal
    units_sold: Decimal
    transaction_count: int


def _returns_by(
    db: Session,
    group_col: Any,
    *,
    store_ids: list[int] | None,
    start: datetime | None,
    end: datetime | None,
) -> dict[Any, tuple[Decimal, Decimal, Decimal]]:
    """Returns {group_key: (returns_amount, cogs_reversed, units_returned)}
    for non-void-sale returns, grouped by an arbitrary SaleItem-joinable
    column (product_id or store_id) — the shared helper behind every
    by-dimension breakdown's netting step."""
    query = (
        select(
            group_col,
            func.coalesce(
                func.sum(
                    SaleReturnItem.unit_price_refunded * SaleReturnItem.quantity
                    - SaleReturnItem.discount_refunded
                    + SaleReturnItem.tax_refunded
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            SaleReturnItem.restock.is_(True),
                            SaleReturnItem.quantity * SaleReturnItem.unit_cost_refunded,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(SaleReturnItem.quantity), 0),
        )
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.sale_id)
        .where(Sale.status != "VOIDED")
        .group_by(group_col)
    )
    query = _apply_return_scope(query, store_ids=store_ids, start=start, end=end)
    return {
        row[0]: (Decimal(row[1]), Decimal(row[2]), Decimal(row[3])) for row in db.execute(query)
    }


def sales_by_store(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[SalesByDimensionRow]:
    """Net sales grouped by store. The same store must produce the same
    figure here as in `sales_summary(store_ids=[that store])` alone —
    proven in tests/test_reports_sales.py (aggregation consistency)."""
    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)

    gross_query = (
        select(
            Sale.store_id,
            func.coalesce(
                func.sum(SaleItem.line_total + SaleItem.discount_amount - SaleItem.tax_amount), 0
            ),
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.unit_cost_at_sale), 0),
            func.coalesce(func.sum(SaleItem.quantity), 0),
            func.count(func.distinct(Sale.id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
        .group_by(Sale.store_id)
    )
    gross_query = _apply_sale_scope(gross_query, store_ids=store_ids, start=start, end=end)
    gross_rows = {
        row[0]: (Decimal(row[1]), Decimal(row[2]), Decimal(row[3]), int(row[4]))
        for row in db.execute(gross_query)
    }
    returns_by_store = _returns_by(
        db, SaleReturn.store_id, store_ids=store_ids, start=start, end=end
    )

    results = []
    for store_id in sorted(set(gross_rows) | set(returns_by_store)):
        gross, cogs_gross, units_gross, txn_count = gross_rows.get(
            store_id, (Decimal("0"), Decimal("0"), Decimal("0"), 0)
        )
        returns_amt, cogs_reversed, units_returned = returns_by_store.get(
            store_id, (Decimal("0"), Decimal("0"), Decimal("0"))
        )
        results.append(
            SalesByDimensionRow(
                key=store_id,
                label=str(store_id),
                gross_sales=gross,
                returns=returns_amt,
                net_sales=gross - returns_amt,
                cogs=cogs_gross - cogs_reversed,
                units_sold=units_gross - units_returned,
                transaction_count=txn_count,
            )
        )
    return results


def sales_by_product(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[SalesByDimensionRow]:
    from app.modules.products.models import Product

    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)

    gross_query = (
        select(
            SaleItem.product_id,
            Product.name,
            func.coalesce(
                func.sum(SaleItem.line_total + SaleItem.discount_amount - SaleItem.tax_amount), 0
            ),
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.unit_cost_at_sale), 0),
            func.coalesce(func.sum(SaleItem.quantity), 0),
            func.count(func.distinct(Sale.id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
        .group_by(SaleItem.product_id, Product.name)
    )
    gross_query = _apply_sale_scope(gross_query, store_ids=store_ids, start=start, end=end)
    gross_rows = {
        row[0]: (row[1], Decimal(row[2]), Decimal(row[3]), Decimal(row[4]), int(row[5]))
        for row in db.execute(gross_query)
    }
    returns_by_product = _returns_by(
        db, SaleReturnItem.sale_item_id, store_ids=store_ids, start=start, end=end
    )
    # Returns are keyed by sale_item_id above (no direct product_id column
    # on SaleReturnItem) — resolve to product_id via the parent SaleItem.
    if returns_by_product:
        item_to_product = {
            row[0]: row[1]
            for row in db.execute(
                select(SaleItem.id, SaleItem.product_id).where(
                    SaleItem.id.in_(list(returns_by_product))
                )
            )
        }
        returns_by_product_id: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
        for sale_item_id, (amt, cogs_rev, units) in returns_by_product.items():
            pid = item_to_product.get(sale_item_id)
            if pid is None:
                continue
            prev = returns_by_product_id.get(pid, (Decimal("0"), Decimal("0"), Decimal("0")))
            returns_by_product_id[pid] = (prev[0] + amt, prev[1] + cogs_rev, prev[2] + units)
    else:
        returns_by_product_id = {}

    results = []
    for product_id in sorted(set(gross_rows) | set(returns_by_product_id)):
        name, gross, cogs_gross, units_gross, txn_count = gross_rows.get(
            product_id, (None, Decimal("0"), Decimal("0"), Decimal("0"), 0)
        )
        returns_amt, cogs_reversed, units_returned = returns_by_product_id.get(
            product_id, (Decimal("0"), Decimal("0"), Decimal("0"))
        )
        results.append(
            SalesByDimensionRow(
                key=product_id,
                label=name or f"Product {product_id}",
                gross_sales=gross,
                returns=returns_amt,
                net_sales=gross - returns_amt,
                cogs=cogs_gross - cogs_reversed,
                units_sold=units_gross - units_returned,
                transaction_count=txn_count,
            )
        )
    return results


@dataclass(frozen=True)
class PaymentMethodRow:
    payment_method: str
    amount: Decimal
    payment_count: int


def sales_by_payment_method(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PaymentMethodRow]:
    """Gross tender mix — how customers actually paid for non-void,
    non-OPEN sales in the period. Deliberately NOT netted against
    refunds (a refund may use a different method than the original
    tender; netting them together would misrepresent both). See
    docs/M11_DESIGN.md Section 5.2 for the separate GL-clearing-account
    reconciliation of this same figure."""
    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)
    query = (
        select(
            Payment.payment_method,
            func.coalesce(func.sum(Payment.amount), 0),
            func.count(Payment.id),
        )
        .join(Sale, Sale.id == Payment.sale_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
        .group_by(Payment.payment_method)
    )
    query = _apply_sale_scope(query, store_ids=store_ids, start=start, end=end)
    return [
        PaymentMethodRow(payment_method=method, amount=Decimal(amount), payment_count=int(count))
        for method, amount, count in db.execute(query)
    ]


@dataclass(frozen=True)
class SalesTrendPoint:
    sale_date: date
    gross_sales: Decimal
    returns: Decimal
    net_sales: Decimal
    transaction_count: int


def sales_trend_by_day(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date,
    date_to: date,
) -> list[SalesTrendPoint]:
    """Day-by-day trend. Backed by a live query today (docs/M11_DESIGN.md
    Section 11 documents exactly when/why this would move to the
    daily_sales_summary materialized view instead)."""
    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)

    gross_query = (
        select(
            func.date(Sale.completed_at),
            func.coalesce(
                func.sum(SaleItem.line_total + SaleItem.discount_amount - SaleItem.tax_amount), 0
            ),
            func.count(func.distinct(Sale.id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
        .group_by(func.date(Sale.completed_at))
    )
    gross_query = _apply_sale_scope(gross_query, store_ids=store_ids, start=start, end=end)
    gross_by_day = {row[0]: (Decimal(row[1]), int(row[2])) for row in db.execute(gross_query)}

    returns_query = (
        select(
            func.date(SaleReturn.created_at),
            func.coalesce(
                func.sum(
                    SaleReturnItem.unit_price_refunded * SaleReturnItem.quantity
                    - SaleReturnItem.discount_refunded
                    + SaleReturnItem.tax_refunded
                ),
                0,
            ),
        )
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.sale_id)
        .where(Sale.status != "VOIDED")
        .group_by(func.date(SaleReturn.created_at))
    )
    returns_query = _apply_return_scope(returns_query, store_ids=store_ids, start=start, end=end)
    returns_by_day = {row[0]: Decimal(row[1]) for row in db.execute(returns_query)}

    all_days = sorted(set(gross_by_day) | set(returns_by_day))
    points = []
    for day in all_days:
        gross, txn_count = gross_by_day.get(day, (Decimal("0"), 0))
        returns_amt = returns_by_day.get(day, Decimal("0"))
        points.append(
            SalesTrendPoint(
                sale_date=day,
                gross_sales=gross,
                returns=returns_amt,
                net_sales=gross - returns_amt,
                transaction_count=txn_count,
            )
        )
    return points


# --- Section 5.2 (docs/M11_DESIGN.md): financial reporting ------------------
#
# Every function below either calls the EXISTING accounting/ap service
# functions directly (never re-deriving their arithmetic) or slices the
# existing trial_balance() rows by account_type. No new GL computation.


@dataclass(frozen=True)
class AccountTypeSummary:
    account_type: str
    rows: list[Any]
    total_debit: Decimal
    total_credit: Decimal


def account_type_summary(
    db: Session,
    *,
    account_type: str,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> AccountTypeSummary:
    """Revenue/COGS/Expense "summary" reports (docs/M11_DESIGN.md Section
    5.2): a presentation slice of trial_balance()'s own rows, grouped by
    account_type — zero new arithmetic."""
    rows = [
        r
        for r in accounting_service.trial_balance(
            db, store_ids=store_ids, date_from=date_from, date_to=date_to
        )
        if r.account_type == account_type
    ]
    return AccountTypeSummary(
        account_type=account_type,
        rows=rows,
        total_debit=sum((r.total_debit for r in rows), start=Decimal("0")),
        total_credit=sum((r.total_credit for r in rows), start=Decimal("0")),
    )


@dataclass(frozen=True)
class BalanceSheetSummary:
    """Assets and Liabilities ONLY — see docs/M11_DESIGN.md Section 5.2:
    this system's chart of accounts has no Equity/Retained-Earnings
    account (nothing in M4-M10 ever posts to one), so a true balance
    sheet (Assets = Liabilities + Equity) cannot be produced. Labeled
    everywhere as "Balance Sheet Summary (Assets & Liabilities)", never
    as a complete balance sheet."""

    store_ids: list[int] | None
    as_of: date | None
    assets: list[Any]
    liabilities: list[Any]
    total_assets: Decimal
    total_liabilities: Decimal


def balance_sheet_summary(
    db: Session, *, store_ids: list[int] | None, as_of: date | None = None
) -> BalanceSheetSummary:
    rows = accounting_service.trial_balance(db, store_ids=store_ids, date_to=as_of)
    assets = [r for r in rows if r.account_type == "ASSET"]
    liabilities = [r for r in rows if r.account_type == "LIABILITY"]

    def _balance(row: Any) -> Decimal:
        return (
            row.total_debit - row.total_credit
            if row.normal_balance == "DEBIT"
            else row.total_credit - row.total_debit
        )

    return BalanceSheetSummary(
        store_ids=store_ids,
        as_of=as_of,
        assets=assets,
        liabilities=liabilities,
        total_assets=sum((_balance(r) for r in assets), start=Decimal("0")),
        total_liabilities=sum((_balance(r) for r in liabilities), start=Decimal("0")),
    )


@dataclass(frozen=True)
class PaymentMethodReconciliationRow:
    payment_method: str
    operational_amount: Decimal
    gl_account_code: str
    gl_balance: Decimal
    discrepancy: Decimal


def cash_payment_method_summary(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PaymentMethodReconciliationRow]:
    """Section 8 reconciliation: operational tender mix (Payment.amount,
    Section 5.1) side by side with each method's own GL clearing-account
    balance (trial_balance()). Expected to match exactly — every sale
    payment posts to precisely one clearing account via
    PAYMENT_METHOD_ACCOUNT_CODE (accounting/constants.py); a discrepancy
    here is exposed, never silently corrected."""
    operational_rows = {
        r.payment_method: r.amount
        for r in sales_by_payment_method(
            db, store_ids=store_ids, date_from=date_from, date_to=date_to
        )
    }
    gl_rows = {
        r.account_code: r
        for r in accounting_service.trial_balance(
            db, store_ids=store_ids, date_from=date_from, date_to=date_to
        )
    }
    results = []
    for method, account_code in PAYMENT_METHOD_ACCOUNT_CODE.items():
        operational_amount = operational_rows.get(method, Decimal("0"))
        gl_row = gl_rows.get(account_code)
        gl_balance = (gl_row.total_debit - gl_row.total_credit) if gl_row else Decimal("0")
        results.append(
            PaymentMethodReconciliationRow(
                payment_method=method,
                operational_amount=operational_amount,
                gl_account_code=account_code,
                gl_balance=gl_balance,
                discrepancy=operational_amount - gl_balance,
            )
        )
    return results


@dataclass(frozen=True)
class ComparativePeriod:
    current: Any
    prior: Any


def profit_and_loss_comparative(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date,
    date_to: date,
    prior_date_from: date,
    prior_date_to: date,
) -> ComparativePeriod:
    """Section 5.2's comparative-period capability: the SAME underlying
    function called twice, never a parallel "trend" computation."""
    current = accounting_service.profit_and_loss(
        db, store_ids=store_ids, date_from=date_from, date_to=date_to
    )
    prior = accounting_service.profit_and_loss(
        db, store_ids=store_ids, date_from=prior_date_from, date_to=prior_date_to
    )
    return ComparativePeriod(current=current, prior=prior)


# --- Section 5.3 (docs/M11_DESIGN.md): inventory analytics ------------------


@dataclass(frozen=True)
class InventoryValueRow:
    key: int
    label: str
    quantity_on_hand: Decimal
    value: Decimal


def inventory_value_by_store(
    db: Session, *, store_ids: list[int] | None
) -> list[InventoryValueRow]:
    from app.modules.products.models import Product

    query = select(
        Product.store_id,
        func.coalesce(func.sum(Product.current_qty_on_hand), 0),
        func.coalesce(func.sum(Product.current_qty_on_hand * Product.current_cost), 0),
    ).group_by(Product.store_id)
    if store_ids is not None:
        query = query.where(Product.store_id.in_(store_ids))
    return [
        InventoryValueRow(
            key=store_id, label=str(store_id), quantity_on_hand=Decimal(qty), value=Decimal(val)
        )
        for store_id, qty, val in db.execute(query)
    ]


def inventory_value_by_category(
    db: Session, *, store_ids: list[int] | None
) -> list[InventoryValueRow]:
    from app.modules.products.models import Product, ProductCategory

    query = (
        select(
            Product.category_id,
            func.coalesce(ProductCategory.name, "Uncategorized"),
            func.coalesce(func.sum(Product.current_qty_on_hand), 0),
            func.coalesce(func.sum(Product.current_qty_on_hand * Product.current_cost), 0),
        )
        .select_from(Product)
        .join(ProductCategory, ProductCategory.id == Product.category_id, isouter=True)
        .group_by(Product.category_id, ProductCategory.name)
    )
    if store_ids is not None:
        query = query.where(Product.store_id.in_(store_ids))
    return [
        InventoryValueRow(
            key=category_id if category_id is not None else 0,
            label=name,
            quantity_on_hand=Decimal(qty),
            value=Decimal(val),
        )
        for category_id, name, qty, val in db.execute(query)
    ]


@dataclass(frozen=True)
class MovementSummaryRow:
    movement_type: str
    quantity: Decimal
    movement_count: int


def inventory_movement_summary(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[MovementSummaryRow]:
    from app.modules.inventory.models import InventoryMovement

    start, end = _day_range_bounds(date_from, date_to)
    query = select(
        InventoryMovement.movement_type,
        func.coalesce(func.sum(InventoryMovement.quantity_delta), 0),
        func.count(InventoryMovement.id),
    ).group_by(InventoryMovement.movement_type)
    if store_ids is not None:
        query = query.where(InventoryMovement.store_id.in_(store_ids))
    if start is not None:
        query = query.where(InventoryMovement.created_at >= start)
    if end is not None:
        query = query.where(InventoryMovement.created_at < end)
    return [
        MovementSummaryRow(movement_type=mtype, quantity=Decimal(qty), movement_count=int(count))
        for mtype, qty, count in db.execute(query)
    ]


@dataclass(frozen=True)
class ShrinkageRow:
    store_id: int
    reason_code: str
    quantity: (
        Decimal  # always <= 0 (stock found MISSING); a positive adjustment is a gain, not shrinkage
    )
    adjustment_count: int


def shrinkage_summary(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ShrinkageRow]:
    """Shrinkage = adjustments with a NEGATIVE quantity_delta only (stock
    found missing) — a positive adjustment is a GAIN
    (ACCOUNT_INVENTORY_ADJUSTMENT_GAIN), never counted as shrinkage
    (ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE). Mixing the two signs into one
    number would hide real loss behind unrelated found-stock gains."""
    from app.modules.inventory.models import StockAdjustment

    start, end = _day_range_bounds(date_from, date_to)
    query = (
        select(
            StockAdjustment.store_id,
            StockAdjustment.reason_code,
            func.coalesce(func.sum(StockAdjustment.quantity_delta), 0),
            func.count(StockAdjustment.id),
        )
        .where(StockAdjustment.quantity_delta < 0)
        .group_by(StockAdjustment.store_id, StockAdjustment.reason_code)
    )
    if store_ids is not None:
        query = query.where(StockAdjustment.store_id.in_(store_ids))
    if start is not None:
        query = query.where(StockAdjustment.created_at >= start)
    if end is not None:
        query = query.where(StockAdjustment.created_at < end)
    return [
        ShrinkageRow(
            store_id=sid, reason_code=reason, quantity=Decimal(qty), adjustment_count=int(count)
        )
        for sid, reason, qty, count in db.execute(query)
    ]


@dataclass(frozen=True)
class TurnoverRow:
    product_id: int
    product_name: str
    cogs: Decimal
    average_inventory_value: Decimal | None
    turnover: Decimal | None
    days_on_hand: Decimal | None


def inventory_turnover(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date,
    date_to: date,
) -> list[TurnoverRow]:
    """turnover = COGS for the period / average inventory value (opening
    + closing, from InventoryMovement.resulting_quantity_on_hand and the
    unit cost recorded at those two movements, / 2). None (never 0 or an
    exception) when average inventory is zero — docs/M11_DESIGN.md
    Section 5.3's explicit undefined-not-zero rule."""
    from app.modules.inventory.models import InventoryMovement
    from app.modules.products.models import Product

    _validate_date_range(date_from, date_to)
    start, end = _day_range_bounds(date_from, date_to)

    cogs_query = (
        select(
            SaleItem.product_id,
            Product.name,
            func.coalesce(func.sum(SaleItem.quantity * SaleItem.unit_cost_at_sale), 0),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(Sale.status.in_(_GROSS_SALE_STATUSES))
        .group_by(SaleItem.product_id, Product.name)
    )
    cogs_query = _apply_sale_scope(cogs_query, store_ids=store_ids, start=start, end=end)
    cogs_by_product = {row[0]: (row[1], Decimal(row[2])) for row in db.execute(cogs_query)}

    def _inventory_values_as_of(as_of: datetime, product_ids: list[int]) -> dict[int, Decimal]:
        """Batched last-movement-before-`as_of` valuation for every product
        in one query (docs/M11_DESIGN.md Section 11): a per-product query
        in a Python loop here would be a textbook N+1 that degrades
        linearly with catalog size. PostgreSQL's DISTINCT ON picks exactly
        one (the most recent) row per product_id in a single index-backed
        scan."""
        if not product_ids:
            return {}
        rows = db.execute(
            select(
                InventoryMovement.product_id,
                InventoryMovement.resulting_quantity_on_hand,
                InventoryMovement.unit_cost_at_movement,
            )
            .distinct(InventoryMovement.product_id)
            .where(
                InventoryMovement.product_id.in_(product_ids),
                InventoryMovement.created_at < as_of,
            )
            .order_by(
                InventoryMovement.product_id,
                InventoryMovement.created_at.desc(),
                InventoryMovement.id.desc(),
            )
        )
        return {pid: Decimal(qty) * Decimal(cost) for pid, qty, cost in rows}

    product_ids = list(cogs_by_product)
    opening_values = _inventory_values_as_of(start or datetime.min, product_ids)
    closing_values = _inventory_values_as_of(end or datetime.max, product_ids)

    results = []
    for product_id, (name, cogs) in cogs_by_product.items():
        opening = opening_values.get(product_id, Decimal("0"))
        closing = closing_values.get(product_id, Decimal("0"))
        average = (opening + closing) / 2
        turnover = _safe_ratio(cogs, average)
        days_on_hand = _safe_ratio(Decimal("365"), turnover) if turnover is not None else None
        results.append(
            TurnoverRow(
                product_id=product_id,
                product_name=name,
                cogs=cogs,
                average_inventory_value=average if average != 0 else None,
                turnover=turnover,
                days_on_hand=days_on_hand,
            )
        )
    return results


@dataclass(frozen=True)
class SlowMovingRow:
    product_id: int
    product_name: str
    store_id: int
    quantity_on_hand: Decimal
    units_sold_in_window: Decimal


def slow_moving_products(
    db: Session,
    *,
    store_ids: list[int] | None,
    window_days: int = 90,
    threshold_units: Decimal = Decimal("1"),
    as_of: date | None = None,
) -> list[SlowMovingRow]:
    """Deterministic rule, not a scored estimate (docs/M11_DESIGN.md
    Section 5.3): current stock > 0 and fewer than `threshold_units`
    SALE-type movements in the trailing `window_days`."""
    from app.modules.inventory.models import InventoryMovement
    from app.modules.products.models import Product

    as_of = as_of or date.today()
    window_start = datetime.combine(as_of, datetime.min.time()) - timedelta(days=window_days)

    sold_query = (
        select(InventoryMovement.product_id, func.sum(-InventoryMovement.quantity_delta))
        .where(
            InventoryMovement.movement_type == "SALE", InventoryMovement.created_at >= window_start
        )
        .group_by(InventoryMovement.product_id)
    )
    if store_ids is not None:
        sold_query = sold_query.where(InventoryMovement.store_id.in_(store_ids))
    sold_by_product = {row[0]: Decimal(row[1]) for row in db.execute(sold_query)}

    stock_query = select(Product).where(
        Product.current_qty_on_hand > 0, Product.is_active.is_(True)
    )
    if store_ids is not None:
        stock_query = stock_query.where(Product.store_id.in_(store_ids))

    results = []
    for product in db.execute(stock_query).scalars():
        sold = sold_by_product.get(product.id, Decimal("0"))
        if sold < threshold_units:
            results.append(
                SlowMovingRow(
                    product_id=product.id,
                    product_name=product.name,
                    store_id=product.store_id,
                    quantity_on_hand=product.current_qty_on_hand,
                    units_sold_in_window=sold,
                )
            )
    return results


@dataclass(frozen=True)
class StockoutRow:
    product_id: int
    product_name: str
    store_id: int


def stockouts(db: Session, *, store_ids: list[int] | None) -> list[StockoutRow]:
    from app.modules.products.models import Product

    query = select(Product).where(Product.current_qty_on_hand == 0, Product.is_active.is_(True))
    if store_ids is not None:
        query = query.where(Product.store_id.in_(store_ids))
    return [
        StockoutRow(product_id=p.id, product_name=p.name, store_id=p.store_id)
        for p in db.execute(query).scalars()
    ]


@dataclass(frozen=True)
class NegativeStockRow:
    product_id: int
    product_name: str
    store_id: int
    quantity_on_hand: Decimal


def negative_stock_products(db: Session, *, store_ids: list[int] | None) -> list[NegativeStockRow]:
    """Only possible where Product.allow_negative_stock=true (a DB CHECK
    forbids it otherwise) — surfaced as its own exception list since a
    negative on-hand quantity is an operational anomaly worth seeing even
    where explicitly permitted."""
    from app.modules.products.models import Product

    query = select(Product).where(Product.current_qty_on_hand < 0)
    if store_ids is not None:
        query = query.where(Product.store_id.in_(store_ids))
    return [
        NegativeStockRow(
            product_id=p.id,
            product_name=p.name,
            store_id=p.store_id,
            quantity_on_hand=p.current_qty_on_hand,
        )
        for p in db.execute(query).scalars()
    ]


@dataclass(frozen=True)
class InTransitRow:
    transfer_id: int
    from_store_id: int
    to_store_id: int
    source_product_id: int
    destination_product_id: int
    quantity_in_transit: Decimal
    value_in_transit: Decimal


def inventory_in_transit(db: Session, *, store_ids: list[int] | None) -> list[InTransitRow]:
    """shipped_quantity - received_quantity per line, valued at
    unit_cost_at_shipment (frozen at ship time) -- matches
    ACCOUNT_INVENTORY_IN_TRANSIT's own valuation exactly. In-transit
    stock is counted at neither the source nor destination store's
    on-hand quantity (docs/M11_DESIGN.md Section 5.3) -- this is its own,
    separate bucket."""
    from app.modules.transfers.models import InterStoreTransfer, InterStoreTransferLine

    query = (
        select(
            InterStoreTransferLine.transfer_id,
            InterStoreTransfer.from_store_id,
            InterStoreTransfer.to_store_id,
            InterStoreTransferLine.source_product_id,
            InterStoreTransferLine.destination_product_id,
            InterStoreTransferLine.shipped_quantity - InterStoreTransferLine.received_quantity,
            InterStoreTransferLine.unit_cost_at_shipment,
        )
        .join(InterStoreTransfer, InterStoreTransfer.id == InterStoreTransferLine.transfer_id)
        .where(
            InterStoreTransferLine.shipped_quantity > InterStoreTransferLine.received_quantity,
            InterStoreTransfer.status != "CANCELLED",
        )
    )
    if store_ids is not None:
        query = query.where(
            (InterStoreTransfer.from_store_id.in_(store_ids))
            | (InterStoreTransfer.to_store_id.in_(store_ids))
        )
    results = []
    for tid, from_sid, to_sid, src_pid, dst_pid, qty, cost in db.execute(query):
        qty = Decimal(qty)
        cost = Decimal(cost) if cost is not None else Decimal("0")
        results.append(
            InTransitRow(
                transfer_id=tid,
                from_store_id=from_sid,
                to_store_id=to_sid,
                source_product_id=src_pid,
                destination_product_id=dst_pid,
                quantity_in_transit=qty,
                value_in_transit=qty * cost,
            )
        )
    return results


@dataclass(frozen=True)
class StockCountVarianceRow:
    stock_count_id: int
    product_id: int
    expected_quantity: Decimal
    counted_quantity: Decimal
    variance: Decimal


def stock_count_variance(db: Session, *, stock_count_id: int) -> list[StockCountVarianceRow]:
    """Only a POSTED count has produced a real adjustment/GL effect
    (docs/M11_DESIGN.md Section 5.3/9) -- a DRAFT/OPEN/COUNTED/REVIEWED
    count is excluded entirely, and a line with counted_quantity IS NULL
    (not yet counted) is excluded from variance, never treated as a
    variance of -expected_quantity."""
    from app.modules.inventory.models import StockCount, StockCountLine

    count = db.get(StockCount, stock_count_id)
    if count is None or count.status != "POSTED":
        return []
    rows = db.execute(
        select(StockCountLine).where(
            StockCountLine.stock_count_id == stock_count_id,
            StockCountLine.counted_quantity.is_not(None),
        )
    ).scalars()
    results = []
    for line in rows:
        assert line.counted_quantity is not None  # guaranteed by the query filter above
        expected = line.expected_quantity or Decimal("0")
        results.append(
            StockCountVarianceRow(
                stock_count_id=stock_count_id,
                product_id=line.product_id,
                expected_quantity=expected,
                counted_quantity=line.counted_quantity,
                variance=line.counted_quantity - expected,
            )
        )
    return results


@dataclass(frozen=True)
class GlReconciliationRow:
    """Generic (label, GL balance, operational value, discrepancy) shape
    shared by every GL-vs-operational reconciliation this module adds
    (in-transit inventory, payroll payable) — mirrors
    accounting.service.inventory_reconciliation's own shape exactly.
    Exposed, never silently corrected."""

    label: str
    gl_balance: Decimal
    operational_value: Decimal
    discrepancy: Decimal


def in_transit_reconciliation(db: Session) -> GlReconciliationRow:
    """Section 8: wraps the EXISTING
    transfers.service.inventory_in_transit_reconciliation (M8) — found
    during Phase 8 review to already implement exactly this GL-vs-
    operational comparison. Reused verbatim rather than duplicated (this
    module's own inventory_in_transit() breakdown above is genuinely new
    — a per-line drill-down the M8 function doesn't provide — but the
    aggregate reconciliation itself must not be recomputed a second way).
    Deliberately no store_ids parameter: M8's own design decision is
    that this account represents value IN TRANSIT BETWEEN stores, not
    within one, so it is reported company-wide only, matching the
    original function's own documented reasoning."""
    from app.modules.transfers import service as transfers_service

    result = transfers_service.inventory_in_transit_reconciliation(db)
    return GlReconciliationRow(
        label="Inventory In Transit",
        gl_balance=result.gl_in_transit_balance,
        operational_value=result.outstanding_in_transit_total,
        discrepancy=result.discrepancy,
    )


# --- Section 5.4 (docs/M11_DESIGN.md): purchasing & supplier analytics -----


@dataclass(frozen=True)
class PurchaseSpendRow:
    key: int
    label: str
    quantity_received: Decimal
    spend: Decimal


def purchase_spend_by_supplier(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PurchaseSpendRow]:
    """Actual received cost (GoodsReceiptItem.unit_cost), not the PO's
    estimated unit_cost — docs/M11_DESIGN.md Section 5.4."""
    from app.modules.purchasing.models import (
        GoodsReceipt,
        GoodsReceiptItem,
        PurchaseOrder,
        Supplier,
    )

    query = (
        select(
            PurchaseOrder.supplier_id,
            Supplier.name,
            func.coalesce(func.sum(GoodsReceiptItem.quantity_received), 0),
            func.coalesce(
                func.sum(GoodsReceiptItem.quantity_received * GoodsReceiptItem.unit_cost), 0
            ),
        )
        .select_from(GoodsReceiptItem)
        .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptItem.goods_receipt_id)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .group_by(PurchaseOrder.supplier_id, Supplier.name)
    )
    if store_ids is not None:
        query = query.where(GoodsReceipt.store_id.in_(store_ids))
    if date_from is not None:
        query = query.where(GoodsReceipt.received_date >= date_from)
    if date_to is not None:
        query = query.where(GoodsReceipt.received_date <= date_to)
    return [
        PurchaseSpendRow(key=sid, label=name, quantity_received=Decimal(qty), spend=Decimal(spend))
        for sid, name, qty, spend in db.execute(query)
    ]


def purchase_spend_by_store(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PurchaseSpendRow]:
    from app.modules.purchasing.models import GoodsReceipt, GoodsReceiptItem

    query = (
        select(
            GoodsReceipt.store_id,
            func.coalesce(func.sum(GoodsReceiptItem.quantity_received), 0),
            func.coalesce(
                func.sum(GoodsReceiptItem.quantity_received * GoodsReceiptItem.unit_cost), 0
            ),
        )
        .select_from(GoodsReceiptItem)
        .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptItem.goods_receipt_id)
        .group_by(GoodsReceipt.store_id)
    )
    if store_ids is not None:
        query = query.where(GoodsReceipt.store_id.in_(store_ids))
    if date_from is not None:
        query = query.where(GoodsReceipt.received_date >= date_from)
    if date_to is not None:
        query = query.where(GoodsReceipt.received_date <= date_to)
    return [
        PurchaseSpendRow(
            key=sid, label=str(sid), quantity_received=Decimal(qty), spend=Decimal(spend)
        )
        for sid, qty, spend in db.execute(query)
    ]


def purchase_spend_by_product(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PurchaseSpendRow]:
    from app.modules.products.models import Product
    from app.modules.purchasing.models import (
        GoodsReceipt,
        GoodsReceiptItem,
        PurchaseOrderItem,
    )

    query = (
        select(
            PurchaseOrderItem.product_id,
            Product.name,
            func.coalesce(func.sum(GoodsReceiptItem.quantity_received), 0),
            func.coalesce(
                func.sum(GoodsReceiptItem.quantity_received * GoodsReceiptItem.unit_cost), 0
            ),
        )
        .select_from(GoodsReceiptItem)
        .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptItem.goods_receipt_id)
        .join(PurchaseOrderItem, PurchaseOrderItem.id == GoodsReceiptItem.purchase_order_item_id)
        .join(Product, Product.id == PurchaseOrderItem.product_id)
        .group_by(PurchaseOrderItem.product_id, Product.name)
    )
    if store_ids is not None:
        query = query.where(GoodsReceipt.store_id.in_(store_ids))
    if date_from is not None:
        query = query.where(GoodsReceipt.received_date >= date_from)
    if date_to is not None:
        query = query.where(GoodsReceipt.received_date <= date_to)
    return [
        PurchaseSpendRow(key=pid, label=name, quantity_received=Decimal(qty), spend=Decimal(spend))
        for pid, name, qty, spend in db.execute(query)
    ]


@dataclass(frozen=True)
class PoFulfillmentRow:
    purchase_order_id: int
    purchase_number: str
    status: str
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_outstanding: Decimal


def po_fulfillment(db: Session, *, store_ids: list[int] | None) -> list[PoFulfillmentRow]:
    """CANCELLED orders are excluded entirely -- an outstanding quantity
    on a cancelled order was never going to arrive and must not appear
    as a real open commitment (docs/M11_DESIGN.md Section 5.4)."""
    from app.modules.purchasing.models import PurchaseOrder, PurchaseOrderItem

    query = (
        select(
            PurchaseOrder.id,
            PurchaseOrder.purchase_number,
            PurchaseOrder.status,
            func.coalesce(func.sum(PurchaseOrderItem.quantity_ordered), 0),
            func.coalesce(func.sum(PurchaseOrderItem.quantity_received), 0),
        )
        .select_from(PurchaseOrder)
        .join(PurchaseOrderItem, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
        .where(PurchaseOrder.status != "CANCELLED")
        .group_by(PurchaseOrder.id, PurchaseOrder.purchase_number, PurchaseOrder.status)
    )
    if store_ids is not None:
        query = query.where(PurchaseOrder.store_id.in_(store_ids))
    return [
        PoFulfillmentRow(
            purchase_order_id=pid,
            purchase_number=number,
            status=status,
            quantity_ordered=Decimal(ordered),
            quantity_received=Decimal(received),
            quantity_outstanding=Decimal(ordered) - Decimal(received),
        )
        for pid, number, status, ordered, received in db.execute(query)
    ]


@dataclass(frozen=True)
class SupplierDeliveryPerformanceRow:
    supplier_id: int
    supplier_name: str
    receipt_count: int
    average_lead_time_days: Decimal | None


def supplier_delivery_performance(
    db: Session, *, store_ids: list[int] | None
) -> list[SupplierDeliveryPerformanceRow]:
    """A simple average lead time (order_date -> received_date),
    deliberately not a weighted or scored rating — docs/M11_DESIGN.md
    Section 5.4/18: explainable, directly traceable to two dates on two
    real records, never an unexplained "AI-like" score."""
    from app.modules.purchasing.models import GoodsReceipt, PurchaseOrder, Supplier

    query = (
        select(
            PurchaseOrder.supplier_id,
            Supplier.name,
            GoodsReceipt.received_date,
            PurchaseOrder.order_date,
        )
        .select_from(GoodsReceipt)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
    )
    if store_ids is not None:
        query = query.where(GoodsReceipt.store_id.in_(store_ids))

    by_supplier: dict[int, tuple[str, list[int]]] = {}
    for supplier_id, name, received_date, order_date in db.execute(query):
        lead_days = (received_date - order_date).days
        label, days_list = by_supplier.setdefault(supplier_id, (name, []))
        days_list.append(lead_days)

    return [
        SupplierDeliveryPerformanceRow(
            supplier_id=supplier_id,
            supplier_name=label,
            receipt_count=len(days_list),
            average_lead_time_days=(
                Decimal(sum(days_list)) / Decimal(len(days_list)) if days_list else None
            ),
        )
        for supplier_id, (label, days_list) in by_supplier.items()
    ]


@dataclass(frozen=True)
class PurchasePriceVarianceRow:
    product_id: int | None
    total_variance: Decimal  # positive = unfavorable (invoiced more than received cost)


def purchase_price_variance_report(
    db: Session,
    *,
    store_ids: list[int] | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PurchasePriceVarianceRow]:
    """Reuses the already-posted ACCOUNT_PURCHASE_PRICE_VARIANCE journal
    lines (ap/service.py, at invoice-posting time) as its source -- never
    an independent recomputation of "invoiced price vs. receipt cost"
    outside of what accounting already posted (docs/M11_DESIGN.md
    Section 5.4)."""
    from app.modules.accounting.constants import ACCOUNT_PURCHASE_PRICE_VARIANCE
    from app.modules.accounting.models import JournalEntry, JournalLine

    accounts = accounting_service.list_accounts(db)
    ppv_account = next((a for a in accounts if a.code == ACCOUNT_PURCHASE_PRICE_VARIANCE), None)
    if ppv_account is None:
        return []

    query = (
        select(
            JournalLine.product_id,
            func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0),
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(JournalLine.account_id == ppv_account.id)
        .group_by(JournalLine.product_id)
    )
    if store_ids is not None:
        query = query.where(JournalEntry.store_id.in_(store_ids))
    if date_from is not None:
        query = query.where(JournalEntry.posting_date >= date_from)
    if date_to is not None:
        query = query.where(JournalEntry.posting_date <= date_to)
    return [
        PurchasePriceVarianceRow(product_id=pid, total_variance=Decimal(variance))
        for pid, variance in db.execute(query)
    ]


# --- Section 5.5 (docs/M11_DESIGN.md): labor & payroll analytics -----------
#
# Source: PayrollPeriod/PayrollEmployeeResult for POSTED periods ONLY --
# a DRAFT/OPEN/CALCULATED/APPROVED period is not yet a final,
# authoritative payroll outcome and is excluded from every financial
# payroll metric (docs/M11_DESIGN.md Section 5.5/10). A separate,
# clearly labeled "pending payroll" count is offered alongside, never
# merged into the posted totals.


@dataclass(frozen=True)
class HeadcountRow:
    store_id: int
    active_employee_count: int


def headcount_by_store(
    db: Session, *, store_ids: list[int] | None, as_of: date | None = None
) -> list[HeadcountRow]:
    from app.modules.hr.models import EmploymentAssignment, EmploymentStatusPeriod

    as_of = as_of or date.today()

    def _current_filter(model: Any) -> Any:
        return (model.effective_to.is_(None)) | (model.effective_to >= as_of)

    query = (
        select(
            EmploymentAssignment.store_id,
            func.count(func.distinct(EmploymentAssignment.employee_id)),
        )
        .select_from(EmploymentAssignment)
        .join(
            EmploymentStatusPeriod,
            EmploymentStatusPeriod.employee_id == EmploymentAssignment.employee_id,
        )
        .where(
            EmploymentAssignment.effective_from <= as_of,
            _current_filter(EmploymentAssignment),
            EmploymentStatusPeriod.effective_from <= as_of,
            _current_filter(EmploymentStatusPeriod),
            EmploymentStatusPeriod.status == "ACTIVE",
        )
        .group_by(EmploymentAssignment.store_id)
    )
    if store_ids is not None:
        query = query.where(EmploymentAssignment.store_id.in_(store_ids))
    return [
        HeadcountRow(store_id=sid, active_employee_count=int(count))
        for sid, count in db.execute(query)
    ]


@dataclass(frozen=True)
class PayrollCostSummary:
    store_ids: list[int] | None
    period_start: date
    period_end: date
    gross_pay: Decimal
    deductions: Decimal
    net_pay: Decimal
    employer_contributions: Decimal
    labor_cost: Decimal  # gross_pay + employer_contributions -- see docstring below
    regular_hours: Decimal
    overtime_hours: Decimal
    pending_period_count: int
    statutory_disclaimer: str = (
        "Deductions shown are the configurable amounts recorded in this system's "
        "payroll engine. No statutory tax, social-security, or other "
        "jurisdiction-specific formula is calculated or implied."
    )


def payroll_cost_summary(
    db: Session,
    *,
    store_ids: list[int] | None,
    period_start: date,
    period_end: date,
) -> PayrollCostSummary:
    """`period_start`/`period_end` are PayrollPeriod's OWN period
    boundaries (never `posted_at`) so this compares like-for-like
    operating periods against sales (docs/M11_DESIGN.md Section 5.5).
    `labor_cost` = gross pay + employer contributions ONLY -- exactly
    and only what this payroll engine represents; it excludes any
    statutory employer obligation not modeled in this system, and is
    never presented as a complete real-world employer cost."""
    from app.modules.payroll.models import (
        PayrollDeductionLine,
        PayrollEmployeeResult,
        PayrollPeriod,
    )

    query = select(PayrollPeriod).where(
        PayrollPeriod.status == "POSTED",
        PayrollPeriod.period_start >= period_start,
        PayrollPeriod.period_end <= period_end,
    )
    if store_ids is not None:
        query = query.where(PayrollPeriod.store_id.in_(store_ids))
    posted_periods = list(db.execute(query).scalars())
    period_ids = [p.id for p in posted_periods]

    gross_pay = sum((p.total_gross for p in posted_periods), start=Decimal("0"))
    deductions = sum((p.total_deductions for p in posted_periods), start=Decimal("0"))
    net_pay = sum((p.total_net_pay for p in posted_periods), start=Decimal("0"))

    employer_contributions = Decimal("0")
    regular_hours = Decimal("0")
    overtime_hours = Decimal("0")
    if period_ids:
        hours_row = db.execute(
            select(
                func.coalesce(func.sum(PayrollEmployeeResult.regular_hours), 0),
                func.coalesce(func.sum(PayrollEmployeeResult.overtime_hours), 0),
            ).where(PayrollEmployeeResult.payroll_period_id.in_(period_ids))
        ).one()
        regular_hours, overtime_hours = Decimal(hours_row[0]), Decimal(hours_row[1])

        employer_contributions = Decimal(
            db.execute(
                select(func.coalesce(func.sum(PayrollDeductionLine.amount), 0))
                .select_from(PayrollDeductionLine)
                .join(
                    PayrollEmployeeResult,
                    PayrollEmployeeResult.id == PayrollDeductionLine.payroll_employee_result_id,
                )
                .where(
                    PayrollEmployeeResult.payroll_period_id.in_(period_ids),
                    PayrollDeductionLine.is_employer_contribution.is_(True),
                )
            ).scalar_one()
        )

    pending_query = select(func.count(PayrollPeriod.id)).where(
        PayrollPeriod.status.in_(("DRAFT", "OPEN", "CALCULATED", "APPROVED")),
        PayrollPeriod.period_start >= period_start,
        PayrollPeriod.period_end <= period_end,
    )
    if store_ids is not None:
        pending_query = pending_query.where(PayrollPeriod.store_id.in_(store_ids))
    pending_count = db.execute(pending_query).scalar_one()

    return PayrollCostSummary(
        store_ids=store_ids,
        period_start=period_start,
        period_end=period_end,
        gross_pay=gross_pay,
        deductions=deductions,
        net_pay=net_pay,
        employer_contributions=employer_contributions,
        labor_cost=gross_pay + employer_contributions,
        regular_hours=regular_hours,
        overtime_hours=overtime_hours,
        pending_period_count=int(pending_count),
    )


@dataclass(frozen=True)
class LaborCostPercentRow:
    labor_cost: Decimal
    net_sales: Decimal
    labor_cost_percent: Decimal | None


def labor_cost_percent_of_sales(
    db: Session, *, store_ids: list[int] | None, period_start: date, period_end: date
) -> LaborCostPercentRow:
    payroll = payroll_cost_summary(
        db, store_ids=store_ids, period_start=period_start, period_end=period_end
    )
    sales = sales_summary(db, store_ids=store_ids, date_from=period_start, date_to=period_end)
    return LaborCostPercentRow(
        labor_cost=payroll.labor_cost,
        net_sales=sales.net_sales,
        labor_cost_percent=_safe_ratio(payroll.labor_cost * 100, sales.net_sales),
    )


def payroll_gl_reconciliation(
    db: Session, *, store_ids: list[int] | None = None
) -> GlReconciliationRow:
    """Section 8: Σ PayrollPeriod.total_net_pay (POSTED) vs.
    ACCOUNT_PAYROLL_PAYABLE net credit -- same exact-match, expose-not-
    correct pattern as every other reconciliation in this module."""
    from app.modules.accounting.constants import ACCOUNT_PAYROLL_PAYABLE
    from app.modules.payroll.models import PayrollPeriod

    rows = accounting_service.trial_balance(db, store_ids=store_ids)
    gl_row = next((r for r in rows if r.account_code == ACCOUNT_PAYROLL_PAYABLE), None)
    gl_balance = (gl_row.total_credit - gl_row.total_debit) if gl_row else Decimal("0")

    query = select(func.coalesce(func.sum(PayrollPeriod.total_net_pay), 0)).where(
        PayrollPeriod.status == "POSTED"
    )
    if store_ids is not None:
        query = query.where(PayrollPeriod.store_id.in_(store_ids))
    operational = Decimal(db.execute(query).scalar_one())

    return GlReconciliationRow(
        label="Payroll Payable",
        gl_balance=gl_balance,
        operational_value=operational,
        discrepancy=gl_balance - operational,
    )


# --- Section 8 (docs/M11_DESIGN.md): KPI dashboard --------------------------


@dataclass(frozen=True)
class KpiDashboard:
    """One coherent management dashboard. Every field is a documented,
    traceable slice of a report already defined above in this module —
    nothing here is a new, independent computation, and every formula is
    named in its own comment below."""

    store_ids: list[int] | None
    period_start: date
    period_end: date
    net_sales: Decimal
    gross_margin_percent: Decimal | None
    cogs_percent_of_net_sales: Decimal | None
    average_transaction_value: Decimal | None
    inventory_value: Decimal
    inventory_turnover_note: str
    stockout_count: int
    shrinkage_units: Decimal
    ap_outstanding: Decimal
    ap_overdue: Decimal
    purchase_spend: Decimal
    labor_cost: Decimal
    labor_cost_percent: Decimal | None
    pending_payroll_periods: int


def kpi_dashboard(
    db: Session, *, store_ids: list[int] | None, period_start: date, period_end: date
) -> KpiDashboard:
    from app.modules.ap import service as ap_service

    sales = sales_summary(db, store_ids=store_ids, date_from=period_start, date_to=period_end)
    inventory_rows = inventory_value_by_store(db, store_ids=store_ids)
    inventory_value = sum((r.value for r in inventory_rows), start=Decimal("0"))
    stockout_count = len(stockouts(db, store_ids=store_ids))
    shrinkage_rows = shrinkage_summary(
        db, store_ids=store_ids, date_from=period_start, date_to=period_end
    )
    # quantity is stored negative (stock found missing) -- shrinkage_units
    # is reported as a positive magnitude for dashboard display.
    shrinkage_units = -sum((r.quantity for r in shrinkage_rows), start=Decimal("0"))

    aging_rows = ap_service.ap_aging(db, store_ids=store_ids, as_of=period_end)
    ap_outstanding = sum((r.total for r in aging_rows), start=Decimal("0"))
    ap_overdue = sum(
        (r.days_1_30 + r.days_31_60 + r.days_61_90 + r.days_over_90 for r in aging_rows),
        start=Decimal("0"),
    )

    spend_rows = purchase_spend_by_store(
        db, store_ids=store_ids, date_from=period_start, date_to=period_end
    )
    purchase_spend = sum((r.spend for r in spend_rows), start=Decimal("0"))

    payroll = payroll_cost_summary(
        db, store_ids=store_ids, period_start=period_start, period_end=period_end
    )

    return KpiDashboard(
        store_ids=store_ids,
        period_start=period_start,
        period_end=period_end,
        net_sales=sales.net_sales,
        gross_margin_percent=sales.gross_margin_percent,
        cogs_percent_of_net_sales=_safe_ratio(sales.cogs * 100, sales.net_sales),
        average_transaction_value=sales.average_transaction_value,
        inventory_value=inventory_value,
        inventory_turnover_note=(
            "See /reports/inventory/turnover for a per-product breakdown "
            "(turnover is undefined at the company level without an "
            "opening/closing valuation snapshot for every product in scope)."
        ),
        stockout_count=stockout_count,
        shrinkage_units=shrinkage_units,
        ap_outstanding=ap_outstanding,
        ap_overdue=ap_overdue,
        purchase_spend=purchase_spend,
        labor_cost=payroll.labor_cost,
        labor_cost_percent=_safe_ratio(payroll.labor_cost * 100, sales.net_sales),
        pending_payroll_periods=payroll.pending_period_count,
    )
