"""M11 Phase 11: performance. Proves the reports module issues a
BOUNDED number of SQL statements regardless of catalog/dataset size
(no N+1 query pattern) — not just "it finished quickly," which can
mask an O(n) query count that only shows up at real-world scale.

`inventory_turnover` originally looked up each product's opening/
closing inventory valuation with two separate queries per product
inside a Python loop — a textbook N+1 that would silently degrade as
the catalog grows. It was rewritten to batch both valuations into one
query each (see app.modules.reports.service._inventory_values_as_of's
docstring). This file's job is to make sure that fix can never
silently regress: it counts actual SQL statements sent to Postgres
and asserts that count stays constant as N grows, which would fail
immediately if the per-product loop ever came back.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.modules.reports import service as reports_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


@contextmanager
def _count_queries(db: Session) -> Iterator[list[str]]:
    """Counts every statement actually sent to Postgres on this test's
    connection for the duration of the `with` block."""
    statements: list[str] = []

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)


def _seed_products_with_sales(db: Session, store, cashier, count: int) -> None:
    products = [
        make_product(
            db,
            store,
            sku=f"PERF-{unique_suffix()}",
            current_price=Decimal("5.00"),
            current_cost=Decimal("2.00"),
            current_qty_on_hand=Decimal("100"),
        )
        for _ in range(count)
    ]
    db.commit()
    for product in products:
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"perf-{unique_suffix()}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("5.00"))],
        )
    db.commit()


def test_inventory_turnover_query_count_does_not_scale_with_product_count(db: Session) -> None:
    """The regression test for the N+1 fix: query count for 3 products
    must equal query count for 30 products. If a per-product query ever
    reappears in inventory_turnover, this assertion fails immediately —
    proof the defense actually depends on the batching, not a lucky
    coincidence of small test data."""
    store = make_store(db)
    cashier = make_user(db, store)

    _seed_products_with_sales(db, store, cashier, count=3)
    with _count_queries(db) as small_run:
        reports_service.inventory_turnover(
            db, store_ids=[store.id], date_from=date(2000, 1, 1), date_to=date.today()
        )
    small_count = len(small_run)

    _seed_products_with_sales(db, store, cashier, count=30)
    with _count_queries(db) as large_run:
        reports_service.inventory_turnover(
            db, store_ids=[store.id], date_from=date(2000, 1, 1), date_to=date.today()
        )
    large_count = len(large_run)

    assert small_count == large_count, (
        f"query count grew from {small_count} to {large_count} as product count "
        "grew 10x -- this indicates a reintroduced N+1 (one query per product)"
    )
    # A small, fixed number regardless of catalog size: the COGS-by-product
    # aggregate plus one batched opening-value and one batched
    # closing-value query -- never one pair of queries per product.
    assert small_count <= 5


def test_sales_summary_query_count_does_not_scale_with_transaction_count(db: Session) -> None:
    """sales_summary must issue the same fixed number of aggregate
    queries whether there are 3 sales or 30 -- it never loops over
    individual Sale/SaleItem rows in Python."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("5.00"), current_qty_on_hand=Decimal("1000")
    )
    db.commit()

    def _make_sales(n: int) -> None:
        for _ in range(n):
            sales_service.finalize_sale(
                db,
                store_id=store.id,
                cashier_id=cashier.id,
                client_transaction_id=f"perf2-{unique_suffix()}",
                caller_store_id=None,
                lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
                payments=[PaymentInput(payment_method="CASH", amount=Decimal("5.00"))],
            )
        db.commit()

    _make_sales(3)
    with _count_queries(db) as small_run:
        reports_service.sales_summary(db, store_ids=[store.id])
    small_count = len(small_run)

    _make_sales(30)
    with _count_queries(db) as large_run:
        reports_service.sales_summary(db, store_ids=[store.id])
    large_count = len(large_run)

    assert small_count == large_count  # gross, returns, void-breakdown queries
    assert small_count <= 5


def test_sales_by_product_query_count_does_not_scale_with_product_count(db: Session) -> None:
    """sales_by_product resolves its returns-by-sale_item_id back to
    product_id via ONE batched IN(...) lookup, never one lookup per
    returned line."""
    store = make_store(db)
    cashier = make_user(db, store)

    def _sell_and_return(n: int) -> None:
        for _ in range(n):
            product = make_product(
                db,
                store,
                sku=f"PERF3-{unique_suffix()}",
                current_price=Decimal("5.00"),
                current_qty_on_hand=Decimal("10"),
            )
            db.commit()
            sale = sales_service.finalize_sale(
                db,
                store_id=store.id,
                cashier_id=cashier.id,
                client_transaction_id=f"perf3-{unique_suffix()}",
                caller_store_id=None,
                lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
                payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
            )
            db.commit()
            sales_service.create_sale_return(
                db,
                sale_id=sale.id,
                store_id=store.id,
                return_date=date.today(),
                client_transaction_id=f"perf3ret-{unique_suffix()}",
                created_by=cashier.id,
                refund_method="CASH",
                caller_store_id=None,
                lines=[
                    sales_service.SaleReturnLineInput(
                        sale_item_id=sale.items[0].id, quantity=Decimal("1"), restock=True
                    )
                ],
            )
            db.commit()

    _sell_and_return(2)
    with _count_queries(db) as small_run:
        reports_service.sales_by_product(db, store_ids=[store.id])
    small_count = len(small_run)

    _sell_and_return(20)
    with _count_queries(db) as large_run:
        reports_service.sales_by_product(db, store_ids=[store.id])
    large_count = len(large_run)

    assert small_count == large_count
