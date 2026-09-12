"""M8 hardening: forced failures at multiple stages of post_stock_count,
ship_transfer, and receive_transfer, each proving a complete rollback with
no half-posted adjustment/movement/journal — same discipline as
tests/test_ap_failure_injection.py and tests/test_sales_returns_failure_injection.py.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import StockAdjustment, StockCount
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransfer, InterStoreTransferReceipt
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def test_failure_during_stock_count_posting_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("20"), current_cost=Decimal("3"))
    db.commit()
    count = inventory_service.create_stock_count(
        db, store_id=store.id, product_ids=[product.id], created_by=None, caller_store_id=None
    )
    inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("15"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside stock-adjustment accounting posting")

    monkeypatch.setattr(accounting_service, "post_stock_adjustment_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    db.rollback()

    reloaded = db.get(StockCount, count.id)
    assert reloaded.status == "REVIEWED"  # never advanced to POSTED
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("20")  # never touched
    assert db.query(StockAdjustment).filter_by(stock_count_id=count.id).count() == 0


def test_failure_during_ship_transfer_accounting_rolls_back_movement_too(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("30")
    )
    make_product(db, store_b, sku=source.sku)
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside transfer shipment accounting posting")

    monkeypatch.setattr(accounting_service, "post_transfer_shipment_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        transfer_service.ship_transfer(
            db,
            transfer_id=transfer.id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("10"))],
            client_transaction_id=f"ship-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    reloaded = db.get(InterStoreTransfer, transfer.id)
    assert reloaded.status == "DRAFT"  # never advanced to SHIPPED
    db.refresh(source)
    assert source.current_qty_on_hand == Decimal("30")  # movement rolled back too
    from app.modules.inventory.models import InventoryMovement

    movements = (
        db.execute(
            select(InventoryMovement).where(
                InventoryMovement.reference_type == "inter_store_transfer",
                InventoryMovement.reference_id == transfer.id,
            )
        )
        .scalars()
        .all()
    )
    assert movements == []


def test_failure_during_receive_transfer_accounting_rolls_back_receipt_too(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("20")
    )
    destination = make_product(db, store_b, sku=source.sku, current_qty_on_hand=Decimal("5"))
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("10"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside transfer receipt accounting posting")

    monkeypatch.setattr(accounting_service, "post_transfer_receipt_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer.id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("10"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    db.refresh(destination)
    assert destination.current_qty_on_hand == Decimal("5")  # receipt movement rolled back
    receipts = db.query(InterStoreTransferReceipt).filter_by(transfer_id=transfer.id).all()
    assert receipts == []
    from app.modules.transfers.models import InterStoreTransferLine

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("0")  # line's own counter rolled back too


def test_failure_during_stock_count_posting_audit_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.audit import service as audit_service

    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("8"), current_cost=Decimal("1"))
    db.commit()
    count = inventory_service.create_stock_count(
        db, store_id=store.id, product_ids=[product.id], created_by=None, caller_store_id=None
    )
    inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("6"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    real_log_event = audit_service.log_event

    def _boom_on_adjustment_audit(db_, **kwargs):
        if kwargs.get("action") == "STOCK_ADJUSTMENT_CREATED":
            raise RuntimeError("forced failure inside audit logging")
        return real_log_event(db_, **kwargs)

    monkeypatch.setattr(inventory_service.audit_service, "log_event", _boom_on_adjustment_audit)

    with pytest.raises(RuntimeError, match="forced failure"):
        inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    db.rollback()

    reloaded = db.get(StockCount, count.id)
    assert reloaded.status == "REVIEWED"
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("8")
    assert db.query(StockAdjustment).filter_by(stock_count_id=count.id).count() == 0
