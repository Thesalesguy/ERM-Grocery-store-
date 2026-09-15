"""M11 Phase 4: financial reporting wrappers — proves every figure comes
from the EXISTING accounting.service GL functions (never a second
computation), reachable at store-list scope, with the documented
Assets/Liabilities-only balance sheet and comparative-period support.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.reports import service as reports_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def _sale(
    db: Session,
    store,
    cashier,
    *,
    price=Decimal("10.00"),
    cost=Decimal("4.000000"),
    qty=Decimal("3"),
):
    product = make_product(
        db, store, current_price=price, current_cost=cost, current_qty_on_hand=Decimal("1000")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=qty)],
        payments=[PaymentInput(payment_method="CASH", amount=(price * qty))],
    )
    db.commit()
    return sale, product


def test_account_type_summary_matches_trial_balance_slice(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("2"))

    revenue = reports_service.account_type_summary(db, account_type="REVENUE", store_ids=[store.id])
    tb = accounting_service.trial_balance(db, store_id=store.id)
    expected_rows = [r for r in tb if r.account_type == "REVENUE"]
    assert len(revenue.rows) == len(expected_rows)
    assert revenue.total_credit == sum((r.total_credit for r in expected_rows), start=Decimal("0"))


def test_balance_sheet_has_no_equity_section_by_design(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("2"))

    bs = reports_service.balance_sheet_summary(db, store_ids=[store.id])
    account_types_present = {r.account_type for r in bs.assets} | {
        r.account_type for r in bs.liabilities
    }
    assert account_types_present <= {"ASSET", "LIABILITY"}
    assert not hasattr(bs, "equity")


def test_balance_sheet_assets_equal_cash_plus_inventory_after_a_sale(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("2"))

    bs = reports_service.balance_sheet_summary(db, store_ids=[store.id])
    # Cash increased by 20.00 (sale proceeds); inventory decreased by 8.00 (COGS) --
    # net asset change should be +12.00 relative to a store with no activity.
    empty_store = make_store(db)
    empty_bs = reports_service.balance_sheet_summary(db, store_ids=[empty_store.id])
    assert bs.total_assets - empty_bs.total_assets == Decimal("12.00")


def test_cash_payment_method_summary_reconciles_to_gl(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("2"))

    rows = reports_service.cash_payment_method_summary(db, store_ids=[store.id])
    cash_row = next(r for r in rows if r.payment_method == "CASH")
    assert cash_row.operational_amount == Decimal("20.00")
    assert cash_row.gl_balance == Decimal("20.00")
    assert cash_row.discrepancy == Decimal("0")


def test_profit_and_loss_comparative_calls_the_same_function_twice(db: Session) -> None:
    from datetime import date

    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("1"))

    comparison = reports_service.profit_and_loss_comparative(
        db,
        store_ids=[store.id],
        date_from=date(2000, 1, 1),
        date_to=date(2999, 1, 1),
        prior_date_from=date(1990, 1, 1),
        prior_date_to=date(1999, 12, 31),
    )
    assert comparison.current.net_sales == Decimal("10.00")
    assert comparison.prior.net_sales == Decimal("0")


def test_trial_balance_store_ids_subset_matches_sum_of_individual_stores(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = make_user(db, store_a)
    cashier_b = make_user(db, store_b)
    _sale(db, store_a, cashier_a, price=Decimal("10.00"), qty=Decimal("1"))
    _sale(db, store_b, cashier_b, price=Decimal("25.00"), qty=Decimal("1"))

    combined = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])
    combined_revenue = next(r for r in combined if r.account_code == "4000")

    a_only = accounting_service.trial_balance(db, store_id=store_a.id)
    b_only = accounting_service.trial_balance(db, store_id=store_b.id)
    a_revenue = next(r for r in a_only if r.account_code == "4000")
    b_revenue = next(r for r in b_only if r.account_code == "4000")

    assert combined_revenue.total_credit == a_revenue.total_credit + b_revenue.total_credit


def test_trial_balance_rejects_both_store_id_and_store_ids(db: Session) -> None:
    import pytest

    from app.core.exceptions import ValidationAppError

    store = make_store(db)
    with pytest.raises(ValidationAppError):
        accounting_service.trial_balance(db, store_id=store.id, store_ids=[store.id])
