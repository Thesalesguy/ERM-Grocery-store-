"""M11 Phase 5: inventory analytics — proves the shrinkage sign rule,
in-transit non-double-counting, and stock-count-variance status
exclusions defined in docs/M11_DESIGN.md Section 5.3.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.inventory import service as inventory_service
from app.modules.reports import service as reports_service
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def test_inventory_value_by_store(db: Session) -> None:
    store = make_store(db)
    make_product(db, store, current_qty_on_hand=Decimal("10"), current_cost=Decimal("2.500000"))
    make_product(db, store, current_qty_on_hand=Decimal("5"), current_cost=Decimal("4.000000"))
    db.commit()

    rows = reports_service.inventory_value_by_store(db, store_ids=[store.id])
    row = next(r for r in rows if r.key == store.id)
    assert row.quantity_on_hand == Decimal("15")
    assert row.value == Decimal("45.000000")


def test_shrinkage_only_counts_negative_adjustments(db: Session) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("100"))
    db.commit()

    # A loss (shrinkage) and a gain (found stock) -- must not cancel out
    # or blend into one "net adjustment" figure.
    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-5"),
        reason_code="THEFT",
        notes="test",
        created_by=user.id,
    )
    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("3"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="test",
        created_by=user.id,
    )
    db.commit()

    rows = reports_service.shrinkage_summary(db, store_ids=[store.id])
    assert len(rows) == 1
    assert rows[0].reason_code == "THEFT"
    assert rows[0].quantity == Decimal("-5")


def test_stockouts_and_negative_stock(db: Session) -> None:
    store = make_store(db)
    make_product(db, store, current_qty_on_hand=Decimal("0"), is_active=True)
    make_product(db, store, current_qty_on_hand=Decimal("10"), is_active=True)
    make_product(
        db, store, current_qty_on_hand=Decimal("-2"), allow_negative_stock=True, is_active=True
    )
    db.commit()

    stockouts = reports_service.stockouts(db, store_ids=[store.id])
    assert len(stockouts) == 1

    negatives = reports_service.negative_stock_products(db, store_ids=[store.id])
    assert len(negatives) == 1
    assert negatives[0].quantity_on_hand == Decimal("-2")


def test_in_transit_is_not_counted_at_either_store(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku="SKU-IT", current_qty_on_hand=Decimal("50"), current_cost=Decimal("7.50")
    )
    make_product(db, store_b, sku="SKU-IT")
    db.commit()

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("20"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("20"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    rows = reports_service.inventory_in_transit(db, store_ids=[store_a.id, store_b.id])
    assert len(rows) == 1
    assert rows[0].quantity_in_transit == Decimal("20")
    assert rows[0].value_in_transit == Decimal("150.000000")

    # Neither store's on-hand cache counts it -- confirms no double-counting
    # at source (already reduced by the ship) or destination (not yet received).
    values = reports_service.inventory_value_by_store(db, store_ids=[store_a.id, store_b.id])
    by_store = {r.key: r.quantity_on_hand for r in values}
    assert by_store[store_a.id] == Decimal("30")  # 50 - 20 shipped
    assert by_store.get(store_b.id, Decimal("0")) == Decimal("0")  # not yet received


def test_stock_count_variance_excludes_non_posted_counts(db: Session) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    db.commit()

    count = inventory_service.create_stock_count(
        db,
        store_id=store.id,
        product_ids=[product.id],
        created_by=user.id,
    )
    db.commit()
    assert count.status == "DRAFT"

    # A DRAFT count has no variance report yet -- must return empty, not
    # a variance computed from unposted/incomplete data.
    rows = reports_service.stock_count_variance(db, stock_count_id=count.id)
    assert rows == []


def test_in_transit_reconciliation_reuses_the_existing_m8_function(db: Session) -> None:
    """docs/M11_DESIGN.md Section 8: this must call
    transfers.service.inventory_in_transit_reconciliation directly, never
    recompute the same comparison independently."""
    from app.modules.transfers import service as transfers_service

    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku="SKU-REC", current_qty_on_hand=Decimal("50"), current_cost=Decimal("9.00")
    )
    make_product(db, store_b, sku="SKU-REC")
    db.commit()

    from app.modules.transfers.service import ShipLineInput, TransferLineInput

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[
            ShipLineInput(transfer_line_id=transfer.lines[0].id, quantity_to_ship=Decimal("10"))
        ],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    expected = transfers_service.inventory_in_transit_reconciliation(db)
    result = reports_service.in_transit_reconciliation(db)
    assert result.gl_balance == expected.gl_in_transit_balance
    assert result.operational_value == expected.outstanding_in_transit_total
    assert result.discrepancy == expected.discrepancy
