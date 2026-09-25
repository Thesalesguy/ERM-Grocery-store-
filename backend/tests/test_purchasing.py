"""Purchasing, goods receiving, and Weighted Average Cost.

The WAC tests here match the exact worked examples in
docs/TECHNICAL_BLUEPRINT.md Section D and the M1 task instructions:

    100 units @ 10  ->  WAC = 10
    + 50 units @ 14 ->  WAC = (100*10 + 50*14) / 150 = 11.333333...
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError
from app.modules.inventory import service as inventory_service
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    unique_suffix,
)


def _make_po_with_item(db: Session, *, quantity_ordered: Decimal, unit_cost: Decimal):
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=quantity_ordered,
        unit_cost=unit_cost,
    )
    db.add(item)
    db.commit()
    return store, po, item, product


def test_creating_purchase_order_does_not_touch_inventory(db: Session) -> None:
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("100"), unit_cost=Decimal("10.00")
    )

    db.refresh(product)
    assert product.current_qty_on_hand == 0
    assert product.current_cost == 0
    assert po.status == "ORDERED"


def test_wac_matches_worked_example_100_at_10_plus_50_at_14(db: Session) -> None:
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("150"), unit_cost=Decimal("10.00")
    )

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("100"), Decimal("10.00"))],
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("100")
    assert product.current_cost == Decimal("10.000000")

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("14.00"))],
    )
    db.commit()
    db.refresh(product)

    assert product.current_qty_on_hand == Decimal("150")
    assert product.current_cost == Decimal("11.333333")
    assert po.status == "RECEIVED"


def test_wac_after_third_purchase_at_a_different_cost(db: Session) -> None:
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("200"), unit_cost=Decimal("10.00")
    )

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("100"), Decimal("10.00"))],
    )
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("14.00"))],
    )
    db.commit()

    # Third receipt at yet another cost: (150 * 11.333333 + 50 * 20) / 200
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 3),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("20.00"))],
    )
    db.commit()
    db.refresh(product)

    expected = inventory_service.compute_new_wac(
        existing_qty=Decimal("150"),
        existing_wac=Decimal("11.333333"),
        received_qty=Decimal("50"),
        received_unit_cost=Decimal("20.00"),
    )
    assert product.current_qty_on_hand == Decimal("200")
    assert product.current_cost == expected


def test_partial_goods_receipt_leaves_status_partially_received(db: Session) -> None:
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("100"), unit_cost=Decimal("5.00")
    )

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("40"), Decimal("5.00"))],
    )
    db.commit()
    db.refresh(item)
    db.refresh(po)

    assert item.quantity_received == Decimal("40")
    assert po.status == "PARTIALLY_RECEIVED"


def test_over_receipt_is_allowed(db: Session) -> None:
    """docs/TECHNICAL_BLUEPRINT.md Section F: over-receipt is allowed, not
    blocked — there is deliberately no CHECK forbidding it."""
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("10"), unit_cost=Decimal("5.00")
    )

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("15"), Decimal("5.00"))],
    )
    db.commit()
    db.refresh(item)
    db.refresh(po)

    assert item.quantity_received == Decimal("15")
    assert po.status == "RECEIVED"


def test_cannot_receive_against_a_cancelled_purchase_order(db: Session) -> None:
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("10"), unit_cost=Decimal("5.00")
    )
    po.status = "CANCELLED"
    db.commit()

    with pytest.raises(ConflictError):
        purchasing_service.receive_goods(
            db,
            client_transaction_id=f"txn-{unique_suffix()}",
            caller_store_id=None,
            purchase_order_id=po.id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("5.00"))],
        )


def test_wac_resets_cleanly_after_stock_reaches_zero_and_reopens(db: Session) -> None:
    """Sell all stock down to exactly zero, then receive a fresh purchase
    at a new cost. Since existing_qty is 0 going into the new receipt, the
    formula naturally collapses to the new purchase's own cost — this is
    the same "opening stock" case, just reached a second time."""
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("10"), unit_cost=Decimal("10.00")
    )
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10")

    # Sell all 10 units down to exactly zero.
    locked = inventory_service.lock_product_for_update(db, product.id)
    inventory_service.record_movement(
        db,
        product=locked,
        store_id=store.id,
        movement_type="SALE",
        quantity_delta=Decimal("-10"),
        unit_cost_at_movement=locked.current_cost,
        reference_type="sale",
        reference_id=None,
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("0")
    assert product.current_cost == Decimal("10.000000")  # unchanged by a sale

    # Fresh purchase at a new, unrelated cost after hitting zero.
    po2 = make_purchase_order(db, store, make_supplier(db))
    item2 = PurchaseOrderItem(
        purchase_order_id=po2.id,
        product_id=product.id,
        quantity_ordered=Decimal("20"),
        unit_cost=Decimal("7.50"),
    )
    db.add(item2)
    db.commit()

    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po2.id,
        received_date=date(2024, 2, 1),
        lines=[GoodsReceiptLineInput(item2.id, Decimal("20"), Decimal("7.50"))],
    )
    db.commit()
    db.refresh(product)

    assert product.current_qty_on_hand == Decimal("20")
    assert product.current_cost == Decimal("7.500000")


def test_insufficient_stock_blocked_without_allow_negative_stock(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("5"))
    db.commit()

    locked = inventory_service.lock_product_for_update(db, product.id)
    with pytest.raises(ConflictError):
        inventory_service.record_movement(
            db,
            product=locked,
            store_id=store.id,
            movement_type="SALE",
            quantity_delta=Decimal("-10"),
            unit_cost_at_movement=Decimal("0"),
            reference_type="sale",
            reference_id=None,
        )
    db.rollback()


def test_purchase_return_data_model(db: Session) -> None:
    """Schema-only proof (no service layer yet): a purchase return
    references the specific cost lot it's reversing, per
    docs/TECHNICAL_BLUEPRINT.md Section D edge case 6."""
    from app.modules.purchasing.models import PurchaseReturn, PurchaseReturnItem

    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("10"), unit_cost=Decimal("12.00")
    )
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("12.00"))],
    )
    db.commit()

    purchase_return = PurchaseReturn(
        purchase_order_id=po.id,
        store_id=store.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        return_date=date(2024, 1, 5),
        reason="Damaged in transit",
    )
    db.add(purchase_return)
    db.flush()
    db.add(
        PurchaseReturnItem(
            purchase_return_id=purchase_return.id,
            product_id=product.id,
            quantity=Decimal("3"),
            unit_cost=Decimal("12.00"),  # the lot's original received cost
        )
    )
    db.commit()

    assert purchase_return.items[0].unit_cost == Decimal("12.00")


def test_cross_store_purchase_return_is_rejected(db: Session) -> None:
    """M19: create_purchase_return's own _enforce_store_access, proven
    at the service layer directly (not just through the route) --
    matching how test_purchasing.py already tests PO creation/receiving
    isolation independently of the HTTP boundary."""
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store_b, current_qty_on_hand=Decimal("10"))
    po = make_purchase_order(db, store_b, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        quantity_received=Decimal("10"),
        unit_cost=Decimal("5.00"),
    )
    db.add(item)
    db.commit()

    with pytest.raises(ForbiddenError):
        purchasing_service.create_purchase_return(
            db,
            purchase_order_id=po.id,
            store_id=store_b.id,
            return_date=date(2024, 1, 6),
            lines=[
                purchasing_service.PurchaseReturnLineInput(
                    product_id=product.id, quantity=Decimal("2")
                )
            ],
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=store_a.id,
        )


def test_wac_matches_worked_example_at_fractional_quantities(db: Session) -> None:
    """M19: proves compute_new_wac's exact-Decimal formula holds at
    fractional precision (a weighed product), not just whole units --
    the arithmetic is identical, this only proves it wasn't silently
    assuming integer quantities anywhere along the way."""
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("2.375"), unit_cost=Decimal("10.00")
    )
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("2.375"), Decimal("10.00"))],
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("2.375")
    assert product.current_cost == Decimal("10.000000")

    po2 = make_purchase_order(db, store, make_supplier(db))
    item2 = PurchaseOrderItem(
        purchase_order_id=po2.id,
        product_id=product.id,
        quantity_ordered=Decimal("1.625"),
        unit_cost=Decimal("14.00"),
    )
    db.add(item2)
    db.commit()
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po2.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item2.id, Decimal("1.625"), Decimal("14.00"))],
    )
    db.commit()
    db.refresh(product)
    # (2.375*10 + 1.625*14) / (2.375+1.625) = (23.75 + 22.75) / 4 = 11.625
    assert product.current_qty_on_hand == Decimal("4.000")
    assert product.current_cost == Decimal("11.625000")


def test_cancelling_a_partially_received_po_retains_the_inventory_already_received(
    db: Session,
) -> None:
    """M19: PARTIALLY_RECEIVED is in _CANCELLABLE_PO_STATUSES -- proves
    what happens to inventory that already physically arrived when the
    remainder is cancelled: it is retained, matching this codebase's
    append-only-ledger philosophy (cancellation stops FUTURE receiving,
    it never reverses a receipt that already posted)."""
    store, po, item, product = _make_po_with_item(
        db, quantity_ordered=Decimal("10"), unit_cost=Decimal("5.00")
    )
    purchasing_service.receive_goods(
        db,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("4"), Decimal("5.00"))],
    )
    db.commit()
    db.refresh(po)
    assert po.status == "PARTIALLY_RECEIVED"
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("4")

    purchasing_service.cancel_purchase_order(
        db, po.id, actor_id=None, reason="Remainder no longer needed", caller_store_id=None
    )
    db.commit()
    db.refresh(po)
    db.refresh(product)
    assert po.status == "CANCELLED"
    # The 4 units already received are retained -- cancellation is not a
    # reversal of what already physically arrived.
    assert product.current_qty_on_hand == Decimal("4")
