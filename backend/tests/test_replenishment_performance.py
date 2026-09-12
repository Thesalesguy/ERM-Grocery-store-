"""M9 hardening pass, Phase 13: query-count regression test for
get_exceptions — proves the N+1 defect found during hardening (a
per-product `db.get(Supplier, ...)` plus `get_current_supplier_product`
call, each issuing its own query) stays fixed. Counts real SQL statements
via SQLAlchemy's `before_cursor_execute` event against a real PostgreSQL
connection, comparing 5 products vs 20 products — a correctly bulk-query
implementation issues the SAME number of statements regardless of product
count; an N+1 implementation's count grows linearly with it."""

from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.modules.replenishment import service as replenishment_service
from tests.factories import make_product, make_store, make_supplier, make_supplier_product


def _count_queries(db: Session, fn) -> int:
    count = 0

    def _before_cursor_execute(*args, **kwargs):
        nonlocal count
        count += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
    return count


def _seed_products_with_suppliers(db: Session, store, n: int) -> None:
    supplier = make_supplier(db)
    for i in range(n):
        product = make_product(
            db,
            store,
            sku=f"SKU-PERF-{i}",
            current_qty_on_hand=Decimal("5"),
            default_supplier_id=supplier.id,
        )
        make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()


def test_get_exceptions_query_count_does_not_grow_with_product_count(db: Session) -> None:
    store_small = make_store(db)
    _seed_products_with_suppliers(db, store_small, 5)
    small_count = _count_queries(
        db, lambda: replenishment_service.get_exceptions(db, store_id=store_small.id)
    )

    store_large = make_store(db)
    _seed_products_with_suppliers(db, store_large, 20)
    large_count = _count_queries(
        db, lambda: replenishment_service.get_exceptions(db, store_id=store_large.id)
    )

    assert large_count == small_count, (
        f"get_exceptions issued {small_count} queries for 5 products but {large_count} for "
        "20 products — query count must be constant (bulk-fetched), not linear in product "
        "count (N+1)"
    )
    # A generous fixed ceiling — this is a small, deliberately bounded
    # function (overdue POs, products, suppliers, current-price pairs,
    # stale plans, plus SQLAlchemy's own transaction bookkeeping
    # statements), never one that scales with row count.
    assert small_count <= 10
