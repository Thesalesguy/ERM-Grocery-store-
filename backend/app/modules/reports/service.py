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
