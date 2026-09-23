"""M19 Design §4: failure injection for receive_goods, create_purchase_invoice,
and create_purchase_return — the three financially-material procurement
writes that had no forced-failure coverage before this milestone (the
underlying transaction-boundary code was already correct; this closes a
proof gap, not a behavior gap). Same discipline as
tests/test_ap_failure_injection.py/tests/test_sale_finalization_failure_injection.py:
monkeypatch a downstream step to raise, assert zero rows survive across
every affected table.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.ap import service as ap_service
from app.modules.ap.models import PurchaseInvoice
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.audit import service as audit_service
from app.modules.inventory.models import InventoryMovement
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import (
    GoodsReceipt,
    PurchaseOrderItem,
    PurchaseReturn,
)
from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseReturnLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    unique_suffix,
)


def _receive(db: Session, store, supplier, product, *, qty: Decimal, cost: Decimal):
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, qty, cost)],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    return po, item


def test_failure_during_receiving_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("5.00"),
    )
    db.add(item)
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside goods-receipt accounting posting")

    monkeypatch.setattr(accounting_service, "post_goods_receipt_journal", _boom)

    key = f"txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        purchasing_service.receive_goods(
            db,
            purchase_order_id=po.id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("5.00"))],
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    # No receipt row survives.
    assert (
        db.execute(
            select(GoodsReceipt).where(GoodsReceipt.client_transaction_id == key)
        ).scalar_one_or_none()
        is None
    )
    # No inventory movement or WAC change survives.
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("0")
    assert (
        db.execute(
            select(InventoryMovement).where(
                InventoryMovement.reference_type == "purchase_order",
                InventoryMovement.reference_id == po.id,
            )
        )
        .scalars()
        .first()
        is None
    )
    # The PO's running-total (quantity_received) never advanced.
    db.refresh(item)
    assert item.quantity_received == Decimal("0")
    db.refresh(po)
    assert po.status == "ORDERED"


def test_failure_during_invoice_creation_audit_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside invoice-creation audit logging")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    key = f"itxn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-{unique_suffix()}",
            invoice_date=date(2024, 1, 5),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    assert (
        db.execute(
            select(PurchaseInvoice).where(PurchaseInvoice.client_transaction_id == key)
        ).scalar_one_or_none()
        is None
    )


def test_failure_during_purchase_return_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))
    db.refresh(product)
    on_hand_before = product.current_qty_on_hand

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside purchase-return accounting posting")

    monkeypatch.setattr(accounting_service, "post_purchase_return_journal", _boom)

    key = f"ret-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        purchasing_service.create_purchase_return(
            db,
            purchase_order_id=po.id,
            store_id=store.id,
            return_date=date(2024, 1, 6),
            lines=[PurchaseReturnLineInput(product_id=product.id, quantity=Decimal("3"))],
            client_transaction_id=key,
            caller_store_id=None,
        )
    db.rollback()

    assert (
        db.execute(
            select(PurchaseReturn).where(PurchaseReturn.client_transaction_id == key)
        ).scalar_one_or_none()
        is None
    )
    db.refresh(product)
    # The return's inventory-removal never applied.
    assert product.current_qty_on_hand == on_hand_before
    assert (
        db.execute(select(JournalEntry).where(JournalEntry.source_type == "PURCHASE_RETURN"))
        .scalars()
        .first()
        is None
    )
