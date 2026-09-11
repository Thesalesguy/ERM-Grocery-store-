"""Inventory-movement ledger: creation, invariants, and DB constraints."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import InventoryMovement
from tests.factories import make_product, make_store


def test_record_movement_creates_ledger_row_and_updates_cache(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    db.commit()

    locked = inventory_service.lock_product_for_update(db, product.id)
    movement = inventory_service.record_movement(
        db,
        product=locked,
        store_id=store.id,
        movement_type="PURCHASE_RECEIPT",
        quantity_delta=Decimal("25"),
        unit_cost_at_movement=Decimal("4.50"),
        reference_type="purchase_order",
        reference_id=None,
        new_product_cost=Decimal("4.50"),
    )
    db.commit()
    db.refresh(product)

    assert movement.resulting_quantity_on_hand == Decimal("25")
    assert product.current_qty_on_hand == Decimal("25")
    assert product.current_cost == Decimal("4.50")


def test_qty_on_hand_matches_ledger_sum(db: Session) -> None:
    """docs/TECHNICAL_BLUEPRINT.md Section G invariant: the cached
    current_qty_on_hand must always equal SUM(inventory_movements) for
    that product."""
    store = make_store(db)
    product = make_product(db, store)
    db.commit()

    for delta, cost, movement_type in [
        (Decimal("100"), Decimal("10"), "PURCHASE_RECEIPT"),
        (Decimal("-30"), Decimal("10"), "SALE"),
        (Decimal("50"), Decimal("12"), "PURCHASE_RECEIPT"),
        (Decimal("-5"), Decimal("0"), "STOCK_ADJUSTMENT_OUT"),
    ]:
        locked = inventory_service.lock_product_for_update(db, product.id)
        inventory_service.record_movement(
            db,
            product=locked,
            store_id=store.id,
            movement_type=movement_type,
            quantity_delta=delta,
            unit_cost_at_movement=cost,
            reference_type="stock_adjustment",
            reference_id=None,
        )
    db.commit()
    db.refresh(product)

    ledger_total = inventory_service.get_quantity_on_hand_from_ledger(db, product.id)
    assert ledger_total == Decimal("115")
    assert product.current_qty_on_hand == ledger_total


def test_zero_delta_rejected_by_check_constraint(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    db.commit()

    db.add(
        InventoryMovement(
            store_id=store.id,
            product_id=product.id,
            movement_type="SALE",
            quantity_delta=Decimal("0"),
            unit_cost_at_movement=Decimal("0"),
            resulting_quantity_on_hand=Decimal("0"),
            reference_type="sale",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_movement_type_and_sign_must_agree(db: Session) -> None:
    """A SALE (a decreasing type) with a positive delta violates
    ck_inventory_movements_direction, even bypassing the service layer —
    this is enforced at the database level, not just in application code."""
    store = make_store(db)
    product = make_product(db, store)
    db.commit()

    db.add(
        InventoryMovement(
            store_id=store.id,
            product_id=product.id,
            movement_type="SALE",
            quantity_delta=Decimal("5"),  # wrong sign for a SALE
            unit_cost_at_movement=Decimal("1"),
            resulting_quantity_on_hand=Decimal("5"),
            reference_type="sale",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_unknown_movement_type_rejected(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    db.commit()

    db.add(
        InventoryMovement(
            store_id=store.id,
            product_id=product.id,
            movement_type="TRANSFER_IN",  # not in MOVEMENT_TYPES for M1
            quantity_delta=Decimal("5"),
            unit_cost_at_movement=Decimal("1"),
            resulting_quantity_on_hand=Decimal("5"),
            reference_type="stock_adjustment",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_stock_adjustment_data_model(db: Session) -> None:
    from app.modules.inventory.models import StockAdjustment

    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    db.commit()

    adjustment = StockAdjustment(
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-2"),
        reason_code="DAMAGE",
        notes="Two units dropped and broken",
    )
    db.add(adjustment)
    db.commit()

    locked = inventory_service.lock_product_for_update(db, product.id)
    inventory_service.record_movement(
        db,
        product=locked,
        store_id=store.id,
        movement_type="STOCK_ADJUSTMENT_OUT",
        quantity_delta=adjustment.quantity_delta,
        unit_cost_at_movement=locked.current_cost,
        reference_type="stock_adjustment",
        reference_id=adjustment.id,
        reason=adjustment.notes,
    )
    db.commit()
    db.refresh(product)

    assert product.current_qty_on_hand == Decimal("8")
