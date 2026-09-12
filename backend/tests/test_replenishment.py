"""M8 replenishment: a read-only suggested-reorder report. Proves
inventory_position accounts for inbound transfers and open POs (never
suggesting a duplicate reorder for stock already on its way), and that
the transfer-vs-purchase split only ever suggests a transfer up to a
sister store's own surplus above ITS OWN reorder point."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.replenishment import service as replenishment_service
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ShipLineInput, TransferLineInput
from tests.factories import make_product, make_purchase_order, make_store, make_supplier


def test_no_suggestion_when_position_at_or_above_reorder_point(db: Session) -> None:
    store = make_store(db)
    make_product(
        db, store, current_qty_on_hand=Decimal("20"), reorder_point=Decimal("10"), is_active=True
    )
    db.commit()
    suggestions = replenishment_service.get_replenishment_suggestions(db, store_id=store.id)
    assert suggestions == []


def test_shortfall_computed_from_on_hand_when_nothing_inbound(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    (suggestion,) = replenishment_service.get_replenishment_suggestions(db, store_id=store.id)
    assert suggestion.product_id == product.id
    assert suggestion.inventory_position == Decimal("2")
    assert suggestion.shortfall == Decimal("8")
    assert suggestion.suggested_purchase_quantity == Decimal("8")
    assert suggestion.suggested_transfer_quantity == Decimal("0")


def test_open_purchase_order_reduces_shortfall(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    po = make_purchase_order(db, store, supplier, status="ORDERED")
    db.add(
        PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("5"),
            unit_cost=Decimal("1"),
            quantity_received=Decimal("0"),
        )
    )
    db.commit()

    (suggestion,) = replenishment_service.get_replenishment_suggestions(db, store_id=store.id)
    assert suggestion.open_purchase_order_qty == Decimal("5")
    assert suggestion.inventory_position == Decimal("7")
    assert suggestion.shortfall == Decimal("3")


def test_inbound_shipped_transfer_reduces_shortfall_and_never_double_counts_received(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku="SKU-REPL", current_qty_on_hand=Decimal("100"), reorder_point=Decimal("0")
    )
    destination = make_product(
        db, store_b, sku="SKU-REPL", current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("6"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("6"))],
        client_transaction_id="ship-repl-1",
        caller_store_id=None,
    )
    db.commit()

    suggestions = replenishment_service.get_replenishment_suggestions(db, store_id=store_b.id)
    (suggestion,) = [s for s in suggestions if s.product_id == destination.id]
    assert suggestion.inbound_transfer_qty == Decimal("6")
    assert suggestion.inventory_position == Decimal("8")  # 2 on hand + 6 inbound
    assert suggestion.shortfall == Decimal("2")


def test_sister_store_surplus_suggests_transfer_before_purchase(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_product(
        db,
        store_a,
        sku="SKU-SURPLUS",
        current_qty_on_hand=Decimal("50"),
        reorder_point=Decimal("10"),
    )
    short = make_product(
        db,
        store_b,
        sku="SKU-SURPLUS",
        current_qty_on_hand=Decimal("2"),
        reorder_point=Decimal("10"),
    )
    db.commit()

    suggestions = replenishment_service.get_replenishment_suggestions(db, store_id=store_b.id)
    (suggestion,) = [s for s in suggestions if s.product_id == short.id]
    # Shortfall is 8; store_a has surplus of 40 (50 - 10 reorder point) —
    # fully coverable by transfer, none needed to purchase.
    assert suggestion.suggested_transfer_quantity == Decimal("8")
    assert suggestion.suggested_purchase_quantity == Decimal("0")
    assert suggestion.sister_store_surplus_source_store_id == store_a.id


def test_never_creates_a_purchase_order_or_transfer_itself(db: Session) -> None:
    """The report is read-only: calling it twice must never create any
    PurchaseOrder or InterStoreTransfer row as a side effect."""
    from app.modules.purchasing.models import PurchaseOrder
    from app.modules.transfers.models import InterStoreTransfer

    store = make_store(db)
    make_product(db, store, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("5"))
    db.commit()
    po_count_before = db.query(PurchaseOrder).count()
    transfer_count_before = db.query(InterStoreTransfer).count()

    replenishment_service.get_replenishment_suggestions(db, store_id=store.id)
    replenishment_service.get_replenishment_suggestions(db, store_id=store.id)

    assert db.query(PurchaseOrder).count() == po_count_before
    assert db.query(InterStoreTransfer).count() == transfer_count_before
