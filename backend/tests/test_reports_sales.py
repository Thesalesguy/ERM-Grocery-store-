"""M11 Phase 3: sales & profitability dashboard — targeted tests proving
each metric definition in docs/M11_DESIGN.md Section 5.1 exactly,
including the load-bearing VOID/return exclusion rules.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.reports import service as reports_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
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


def test_completed_sale_counts_in_gross_and_net(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    sale, _ = _sale(
        db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("3")
    )

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.gross_sales == Decimal("30.00")
    assert summary.net_sales == Decimal("30.00")
    assert summary.cogs == Decimal("12.00")
    assert summary.gross_profit == Decimal("18.00")
    assert summary.transaction_count == 1
    assert summary.units_sold == Decimal("3")
    assert summary.gross_margin_percent == Decimal("60")
    assert summary.average_transaction_value == Decimal("30.00")


def test_voided_sale_nets_to_zero_and_is_flagged_via_audit_trail(db: Session) -> None:
    """The actual (verified) system behavior: void_sale never sets
    Sale.status='VOIDED' (that enum value has no producer anywhere in
    this codebase) -- a full void ends at status='REFUNDED', identical
    to an ordinary 100% customer return. Financial safety does not
    depend on the unused status value: the void nets to exactly zero
    revenue through the same returns-netting math, so nothing is
    double-counted. The audit-log-derived void_count/void_amount
    breakdown is the only reliable way to see it was a void."""
    store = make_store(db)
    cashier = make_user(db, store)
    sale, _ = _sale(
        db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("2")
    )
    sales_service.void_sale(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        refund_method="CASH",
        client_transaction_id=f"void-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(sale)
    assert sale.status == "REFUNDED"  # confirms the documented (not assumed) behavior

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.gross_sales == Decimal("20.00")
    assert summary.returns == Decimal("20.00")
    assert summary.net_sales == Decimal(
        "0.00"
    )  # the only property that must hold: no revenue leaks through
    assert summary.cogs == Decimal("0.00")
    assert summary.void_count == 1
    assert summary.void_amount == Decimal("20.00")


def test_ordinary_full_return_is_not_flagged_as_a_void(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    sale, _ = _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("2"))
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"), restock=True)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.returns == Decimal("20.00")
    assert summary.void_count == 0
    assert summary.void_amount == Decimal("0")


def test_partial_return_nets_correctly_without_double_counting(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    sale, _ = _sale(
        db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("5")
    )
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"), restock=True)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.gross_sales == Decimal("50.00")  # full original sale still counts
    assert summary.returns == Decimal("20.00")  # only the 2 returned units
    assert summary.net_sales == Decimal("30.00")
    assert summary.cogs == Decimal("12.00")  # (5-2) * 4.00
    assert summary.units_sold == Decimal("3")


def test_non_restocked_return_does_not_reverse_cogs(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    sale, _ = _sale(
        db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("4")
    )
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"), restock=False)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.returns == Decimal("10.00")
    assert summary.cogs == Decimal(
        "16.00"
    )  # unchanged -- the damaged unit's cost was never reversed


def test_zero_price_sale_still_counts_units_and_cogs(db: Session) -> None:
    """A $0 promotional give-away line: the sale still needs a payment
    row satisfying ck_payments_amount_positive (a $0 tender isn't a
    valid Payment), so a cash payment is tendered and refunded as
    change -- finalize_sale's own documented overpayment/change_due
    path, not a special case this test invents."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("0.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("1000"),
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("0.01"))],
    )
    db.commit()

    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.gross_sales == Decimal("0.00")
    assert summary.cogs == Decimal("8.00")
    assert summary.units_sold == Decimal("2")
    assert summary.transaction_count == 1
    assert summary.gross_profit == Decimal("-8.00")


def test_empty_period_returns_none_not_zero_or_error_for_ratios(db: Session) -> None:
    store = make_store(db)
    summary = reports_service.sales_summary(db, store_ids=[store.id])
    assert summary.transaction_count == 0
    assert summary.gross_margin_percent is None
    assert summary.average_transaction_value is None


def test_store_isolation_in_aggregation(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = make_user(db, store_a)
    cashier_b = make_user(db, store_b)
    _sale(db, store_a, cashier_a, price=Decimal("10.00"), qty=Decimal("1"))
    _sale(db, store_b, cashier_b, price=Decimal("100.00"), qty=Decimal("1"))

    summary_a = reports_service.sales_summary(db, store_ids=[store_a.id])
    assert summary_a.gross_sales == Decimal("10.00")

    summary_both = reports_service.sales_summary(db, store_ids=[store_a.id, store_b.id])
    assert summary_both.gross_sales == Decimal("110.00")


def test_date_range_filters_by_completed_at(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("1"))

    far_future = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2999, 1, 1), date_to=date(2999, 1, 2)
    )
    assert far_future.gross_sales == Decimal("0")

    far_past = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2000, 1, 1), date_to=date(2000, 1, 2)
    )
    assert far_past.gross_sales == Decimal("0")


def test_invalid_date_range_rejected(db: Session) -> None:
    import pytest

    from app.core.exceptions import ValidationAppError

    store = make_store(db)
    with pytest.raises(ValidationAppError):
        reports_service.sales_summary(
            db, store_ids=[store.id], date_from=date(2024, 2, 1), date_to=date(2024, 1, 1)
        )


# --- Breakdown functions: consistency with the top-line summary ------------


def test_sales_by_store_sums_to_the_multi_store_summary(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = make_user(db, store_a)
    cashier_b = make_user(db, store_b)
    _sale(db, store_a, cashier_a, price=Decimal("10.00"), qty=Decimal("2"))
    _sale(db, store_b, cashier_b, price=Decimal("25.00"), qty=Decimal("1"))

    rows = reports_service.sales_by_store(db, store_ids=[store_a.id, store_b.id])
    total_net = sum((r.net_sales for r in rows), start=Decimal("0"))
    summary = reports_service.sales_summary(db, store_ids=[store_a.id, store_b.id])
    assert total_net == summary.net_sales
    by_id = {r.key: r for r in rows}
    assert by_id[store_a.id].net_sales == Decimal("20.00")
    assert by_id[store_b.id].net_sales == Decimal("25.00")


def test_sales_by_product_nets_returns_for_that_product_only(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    sale, product = _sale(
        db, store, cashier, price=Decimal("10.00"), cost=Decimal("4.000000"), qty=Decimal("5")
    )
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"), restock=True)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    rows = reports_service.sales_by_product(db, store_ids=[store.id])
    row = next(r for r in rows if r.key == product.id)
    assert row.gross_sales == Decimal("50.00")
    assert row.returns == Decimal("20.00")
    assert row.net_sales == Decimal("30.00")
    assert row.units_sold == Decimal("3")


def test_sales_by_payment_method_reflects_tender_mix(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("1000"),
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[
            PaymentInput(payment_method="CASH", amount=Decimal("15.00")),
            PaymentInput(payment_method="CARD", amount=Decimal("5.00")),
        ],
    )
    db.commit()

    rows = reports_service.sales_by_payment_method(db, store_ids=[store.id])
    by_method = {r.payment_method: r.amount for r in rows}
    assert by_method["CASH"] == Decimal("15.00")
    assert by_method["CARD"] == Decimal("5.00")


def test_sales_trend_by_day_sums_to_the_summary(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    _sale(db, store, cashier, price=Decimal("10.00"), qty=Decimal("2"))

    points = reports_service.sales_trend_by_day(
        db, store_ids=[store.id], date_from=date(2000, 1, 1), date_to=date(2999, 1, 1)
    )
    total_net = sum((p.net_sales for p in points), start=Decimal("0"))
    summary = reports_service.sales_summary(
        db, store_ids=[store.id], date_from=date(2000, 1, 1), date_to=date(2999, 1, 1)
    )
    assert total_net == summary.net_sales


# --- Authorization scoping (docs/M11_DESIGN.md Section 3) -------------------


def test_resolve_authorized_store_ids_unrestricted_user() -> None:
    from app.modules.auth.service import CurrentUser

    admin = CurrentUser(
        id=1, username="admin", full_name="Admin", store_id=None, permissions=frozenset()
    )
    assert reports_service.resolve_authorized_store_ids(admin, None) is None
    assert reports_service.resolve_authorized_store_ids(admin, [1, 2, 3]) == [1, 2, 3]


def test_resolve_authorized_store_ids_store_scoped_user_own_store_only() -> None:
    from app.modules.auth.service import CurrentUser

    manager = CurrentUser(
        id=2, username="mgr", full_name="Mgr", store_id=7, permissions=frozenset()
    )
    assert reports_service.resolve_authorized_store_ids(manager, None) == [7]
    assert reports_service.resolve_authorized_store_ids(manager, [7]) == [7]


def test_resolve_authorized_store_ids_store_scoped_user_denied_other_store() -> None:
    import pytest

    from app.core.exceptions import ForbiddenError
    from app.modules.auth.service import CurrentUser

    manager = CurrentUser(
        id=3, username="mgr2", full_name="Mgr2", store_id=7, permissions=frozenset()
    )
    with pytest.raises(ForbiddenError):
        reports_service.resolve_authorized_store_ids(manager, [7, 8])
    with pytest.raises(ForbiddenError):
        reports_service.resolve_authorized_store_ids(manager, [8])
