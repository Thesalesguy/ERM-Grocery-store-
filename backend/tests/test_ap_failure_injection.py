"""M6 Session I: forced failures at multiple stages of post_purchase_invoice,
void_purchase_invoice, and record_supplier_payment, each proving a
complete rollback with no half-posted invoice/payment — same discipline
as tests/test_sales_returns_failure_injection.py and
tests/test_accounting_hardening.py Section 16.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.ap import service as ap_service
from app.modules.ap.models import PurchaseInvoice, SupplierPayment
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.audit import service as audit_service
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


def _draft_invoice(db: Session, store, supplier, item, *, qty: Decimal, cost: Decimal, po_id: int):
    return ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po_id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, qty, cost)],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )


def test_failure_during_invoice_posting_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))
    invoice = _draft_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside invoice accounting posting")

    monkeypatch.setattr(accounting_service, "post_purchase_invoice_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.rollback()

    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")
    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.status == "DRAFT"
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_failure_during_invoice_posting_audit_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("5"), cost=Decimal("4.00"))
    invoice = _draft_invoice(
        db, store, supplier, item, qty=Decimal("5"), cost=Decimal("4.00"), po_id=po.id
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside audit logging")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.rollback()

    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")
    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.status == "DRAFT"


def test_failure_during_void_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))
    invoice = _draft_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside void accounting posting")

    monkeypatch.setattr(accounting_service, "post_purchase_invoice_void_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.rollback()

    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")  # NOT reversed
    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.status == "POSTED"  # NOT voided
    void_journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
                JournalEntry.source_id == invoice.id,
            )
        )
        .scalars()
        .all()
    )
    assert void_journals == []


def test_failure_during_payment_accounting_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))
    invoice = _draft_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside payment accounting posting")

    monkeypatch.setattr(accounting_service, "post_supplier_payment_journal", _boom)

    txn_id = f"ptxn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.record_supplier_payment(
            db,
            purchase_invoice_id=invoice.id,
            store_id=store.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("20.00"),
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.amount_paid == Decimal("0.00")
    assert reloaded.status == "POSTED"
    # No payment row survived to be referenced by a journal at all — the
    # failure was forced before the payment's own flush could complete.
    assert (
        db.execute(
            select(SupplierPayment).where(SupplierPayment.client_transaction_id == txn_id)
        ).scalar_one_or_none()
        is None
    )


def test_failure_during_payment_audit_rolls_back_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("5"), cost=Decimal("5.00"))
    invoice = _draft_invoice(
        db, store, supplier, item, qty=Decimal("5"), cost=Decimal("5.00"), po_id=po.id
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside audit logging")

    monkeypatch.setattr(audit_service, "log_event", _boom)

    txn_id = f"ptxn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.record_supplier_payment(
            db,
            purchase_invoice_id=invoice.id,
            store_id=store.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("10.00"),
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.amount_paid == Decimal("0.00")
    assert (
        db.execute(
            select(SupplierPayment).where(SupplierPayment.client_transaction_id == txn_id)
        ).scalar_one_or_none()
        is None
    )
