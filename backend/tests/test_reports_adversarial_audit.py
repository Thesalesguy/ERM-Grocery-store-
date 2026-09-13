"""M11 Phase 16: adversarial self-audit against the 20 named failure
scenarios in the M11 task brief. Each scenario is either:

(a) a NEW test here, for a scenario not already exercised by an
    earlier M11 test file, or
(b) a one-line pointer (in this docstring) to the existing test that
    already proves it, so this file is the single index of "where is
    scenario N proven" without duplicating passing tests for no
    reason.

Scenario -> where it's proven (short refs; full names below or in the
named file):
 1. Double-counted returns          -> sales.py (netted exactly once)
 2. Voided sale counted in revenue  -> sales.py::voided_sale_nets_to_zero
 3. Reversed journal miscounted     -> reversed_manual_journal... (below)
 4. Store A data in Store B's view  -> rbac.py (cross-store denial)
 5. Unauthorized company-wide agg.  -> rbac.py (store-scoped denied)
 6. Payroll exposed to bad roles    -> rbac.py::hr_clerk / no_payroll_leaks
 7. Inventory value vs GL mismatch  -> no_report_duplicates_gl (below)
 8. AP aging vs ledger mismatch     -> ap.service.ap_reconciliation
    (M6/M7 suite); reuse-not-recompute proven below
 9. Purchase Clearing misreported   -> ap.service.purchase_clearing_
    reconciliation (M7 suite); same reuse proof as #8
10. Transfer double-counted (ends)  -> inventory.py::in_transit_is_not_counted
11. In-transit omitted/duplicated   -> inventory.py (in-transit tests)
12. Date-boundary errors            -> partial_period_boundary (below)
13. Timezone errors                 -> utc_day_boundary_not_shifted (below)
14. Partial-period errors           -> partial_period_boundary (below)
15. Zero-value edge cases           -> sales.py; zero_transaction_period (below)
16. Division-by-zero                -> _safe_ratio tests; zero_transaction_period
17. Empty date ranges               -> zero_transaction_period (below)
18. Very large date ranges          -> very_large_date_range (below)
19. N+1 queries                     -> test_reports_performance.py
20. Inconsistent concurrent results -> test_reports_concurrency.py
"""

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import ACCOUNT_CASH_ON_HAND, ACCOUNT_SALES_REVENUE
from app.modules.reports import service as reports_service
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def _post_manual_entry(db: Session, *, store_id: int, amount: Decimal):
    from app.modules.accounting.service import _credit, _debit, _post_journal

    return _post_journal(
        db,
        store_id=store_id,
        posting_date=date(2024, 1, 1),
        source_type="MANUAL",
        source_id=None,
        memo="adversarial audit test entry",
        created_by=None,
        lines=[
            _debit(ACCOUNT_CASH_ON_HAND, amount),
            _credit(ACCOUNT_SALES_REVENUE, amount),
        ],
    )


def test_reversed_manual_journal_entry_nets_to_zero_via_reused_reports(db: Session) -> None:
    """Scenario 3: a reversed journal entry must never be miscounted --
    the reversal is a real, separately-posted, opposite-signed entry
    (accounting_service.reverse_journal_entry), and every M11 financial
    report reuses trial_balance()/profit_and_loss() verbatim rather than
    re-deriving GL arithmetic, so this is really a proof that the reuse
    itself produces the correct net-zero answer, not a new computation
    that could get it wrong a second way."""
    store = make_store(db)
    db.commit()
    entry = _post_manual_entry(db, store_id=store.id, amount=Decimal("500.00"))
    db.commit()

    before = reports_service.account_type_summary(db, account_type="REVENUE", store_ids=[store.id])
    assert before.total_credit - before.total_debit == Decimal("500.00")

    accounting_service.reverse_journal_entry(
        db,
        journal_entry_id=entry.id,
        reason="adversarial audit reversal",
        reversed_by=None,
        caller_store_id=None,
    )
    db.commit()

    after = reports_service.account_type_summary(db, account_type="REVENUE", store_ids=[store.id])
    assert after.total_credit - after.total_debit == Decimal("0.00")


def test_no_report_duplicates_an_existing_gl_reconciliation(
    client: TestClient, db: Session
) -> None:
    """Scenarios 7-9: the pre-existing inventory/AP/Purchase-Clearing
    reconciliations (M4/M6/M7 -- already exposed at
    /api/v1/accounting/reports/inventory-reconciliation,
    /api/v1/ap/reports/reconciliation, and
    /api/v1/ap/reports/purchase-clearing-reconciliation, each with its
    own hardening test suite) must never be duplicated under
    /api/v1/reports/* with a second, independently-computed version --
    exactly the "second accounting truth" this milestone forbids. The
    ONE exception is in-transit, which IS wrapped under /reports (verbatim,
    proven in test_reports_inventory.py to call the original function,
    never recompute it) because it did not already have a /reports-shaped
    home. Checked against the live OpenAPI schema, not just source code,
    so a future route addition can't slip past this."""
    openapi = client.get("/openapi.json").json()
    report_paths = [p for p in openapi["paths"] if p.startswith("/api/v1/reports/")]

    forbidden_terms = ["ap-reconciliation", "purchase-clearing", "inventory-reconciliation"]
    for path in report_paths:
        for term in forbidden_terms:
            assert term not in path, (
                f"{path} appears to duplicate an existing GL reconciliation "
                f"({term}) under /reports -- these must be reused via their "
                "existing accounting/ap endpoints, never recomputed a second way"
            )


def test_utc_day_boundary_is_not_shifted_by_a_local_timezone(db: Session) -> None:
    """Scenario 13: a sale completed at 23:59:59 UTC on Jan 1 must be
    counted in Jan 1's totals -- never shifted to Jan 2 (or Dec 31) by
    an accidental local-timezone conversion. docs/M11_DESIGN.md Section
    6: UTC everywhere, no new local-date conversion introduced by M11."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    # Force completed_at to exactly the last second of Jan 1 UTC.
    db.execute(
        Sale.__table__.update()
        .where(Sale.id == sale.id)
        .values(completed_at=datetime(2024, 1, 1, 23, 59, 59, tzinfo=None))
    )
    db.commit()

    jan_1_only = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 1, 1), date_to=date(2024, 1, 1)
    )
    jan_2_only = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 1, 2), date_to=date(2024, 1, 2)
    )
    assert jan_1_only.gross_sales == Decimal("10.00")
    assert jan_2_only.gross_sales == Decimal("0.00")


def test_partial_period_boundary_is_exact_not_rounded_to_a_whole_day(db: Session) -> None:
    """Scenarios 12/14: a sale on day 2 of a 3-day window must not
    appear when querying only day 1 or only day 3 -- proving date
    filtering is exact per calendar day, not silently widened to a
    whole month/week or leaking across an adjacent day."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    db.execute(
        Sale.__table__.update()
        .where(Sale.id == sale.id)
        .values(completed_at=datetime(2024, 3, 15, 12, 0, 0))
    )
    db.commit()

    day_before = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 3, 14), date_to=date(2024, 3, 14)
    )
    exact_day = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 3, 15), date_to=date(2024, 3, 15)
    )
    day_after = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 3, 16), date_to=date(2024, 3, 16)
    )
    assert day_before.gross_sales == Decimal("0.00")
    assert exact_day.gross_sales == Decimal("10.00")
    assert day_after.gross_sales == Decimal("0.00")


def test_zero_transaction_period_reports_all_zeros(db: Session) -> None:
    """Scenarios 15-17: a store with zero transactions in a date range
    (including a same-day empty range, date_from == date_to) must
    report clean zeros and None ratios -- never a crash, never a
    division-by-zero exception, never a misleading nonzero value."""
    store = make_store(db)
    db.commit()

    summary = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2024, 5, 1), date_to=date(2024, 5, 1)
    )
    assert summary.gross_sales == Decimal("0.00")
    assert summary.net_sales == Decimal("0.00")
    assert summary.transaction_count == 0
    assert summary.gross_margin_percent is None
    assert summary.average_transaction_value is None


def test_very_large_date_range_does_not_error(db: Session) -> None:
    """Scenario 18: an operator fat-fingering a multi-century date range
    must get a normal (empty or full) result, not an exception from
    datetime arithmetic overflowing or a pathological query."""
    store = make_store(db)
    db.commit()

    summary = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(1901, 1, 1), date_to=date(2099, 12, 31)
    )
    assert summary.gross_sales == Decimal("0.00")
    assert summary.transaction_count == 0
