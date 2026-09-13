"""M11 Phase 6: purchasing & supplier analytics — proves spend uses the
actual received cost (not the PO's estimate), CANCELLED orders are
excluded from fulfillment, and the PPV report is sourced from the
already-posted GL lines (never a second computation).
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.reports import service as reports_service
from tests.factories import make_product, make_purchase_order, make_store, make_supplier


def _receive(
    db: Session,
    *,
    quantity_ordered=Decimal("10"),
    po_unit_cost=Decimal("5.00"),
    received_unit_cost=Decimal("5.00"),
):
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=quantity_ordered,
        unit_cost=po_unit_cost,
    )
    db.add(item)
    db.commit()

    from app.modules.purchasing.service import GoodsReceiptLineInput
    from tests.factories import unique_suffix

    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 5),
        lines=[GoodsReceiptLineInput(item.id, quantity_ordered, received_unit_cost)],
        client_transaction_id=f"grn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    return store, supplier, product, po


def test_purchase_spend_uses_actual_received_cost_not_po_estimate(db: Session) -> None:
    store, supplier, product, po = _receive(
        db,
        quantity_ordered=Decimal("10"),
        po_unit_cost=Decimal("5.00"),
        received_unit_cost=Decimal("6.00"),
    )

    rows = reports_service.purchase_spend_by_supplier(db, store_ids=[store.id])
    row = next(r for r in rows if r.key == supplier.id)
    assert row.spend == Decimal("60.000000")  # 10 * 6.00 (received), not 10 * 5.00 (PO estimate)
    assert row.quantity_received == Decimal("10")


def test_purchase_spend_by_store_and_product(db: Session) -> None:
    store, supplier, product, po = _receive(
        db, quantity_ordered=Decimal("4"), received_unit_cost=Decimal("2.50")
    )

    by_store = reports_service.purchase_spend_by_store(db, store_ids=[store.id])
    assert by_store[0].spend == Decimal("10.000000")

    by_product = reports_service.purchase_spend_by_product(db, store_ids=[store.id])
    row = next(r for r in by_product if r.key == product.id)
    assert row.spend == Decimal("10.000000")


def test_po_fulfillment_excludes_cancelled_orders(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("20"),
        unit_cost=Decimal("3.00"),
    )
    db.add(item)
    po.status = "CANCELLED"
    db.commit()

    rows = reports_service.po_fulfillment(db, store_ids=[store.id])
    assert rows == []


def test_po_fulfillment_reports_outstanding_quantity(db: Session) -> None:
    store, supplier, product, po = _receive(
        db, quantity_ordered=Decimal("10"), received_unit_cost=Decimal("5.00")
    )
    # Fully received in this fixture; add a second, un-received item.
    product2 = make_product(db, store)
    po2 = make_purchase_order(db, store, supplier)
    item2 = PurchaseOrderItem(
        purchase_order_id=po2.id,
        product_id=product2.id,
        quantity_ordered=Decimal("15"),
        unit_cost=Decimal("2.00"),
    )
    db.add(item2)
    db.commit()

    rows = reports_service.po_fulfillment(db, store_ids=[store.id])
    row2 = next(r for r in rows if r.purchase_order_id == po2.id)
    assert row2.quantity_ordered == Decimal("15")
    assert row2.quantity_received == Decimal("0")
    assert row2.quantity_outstanding == Decimal("15")


def test_supplier_delivery_performance_computes_average_lead_time(db: Session) -> None:
    store, supplier, product, po = _receive(db)
    po.order_date = date(2024, 1, 1)
    db.commit()

    rows = reports_service.supplier_delivery_performance(db, store_ids=[store.id])
    row = next(r for r in rows if r.supplier_id == supplier.id)
    assert row.receipt_count == 1
    assert row.average_lead_time_days == Decimal("4")  # Jan 1 -> Jan 5


def test_purchase_price_variance_report_reads_posted_gl_lines(db: Session) -> None:
    store, supplier, product, po = _receive(
        db,
        quantity_ordered=Decimal("10"),
        po_unit_cost=Decimal("5.00"),
        received_unit_cost=Decimal("5.00"),
    )
    rows = reports_service.purchase_price_variance_report(db, store_ids=[store.id])
    # No invoice posted in this fixture -- PPV only exists once an invoice
    # is matched/posted (docs/M11_DESIGN.md Section 5.4), so this must be
    # empty, not a recomputed "expected" variance from PO vs receipt cost.
    assert rows == []


def test_purchase_price_variance_report_matches_the_posted_invoice_variance(db: Session) -> None:
    from app.modules.ap import service as ap_service
    from app.modules.ap.service import PurchaseInvoiceLineInput
    from tests.factories import unique_suffix

    store, supplier, product, po = _receive(
        db, quantity_ordered=Decimal("10"), received_unit_cost=Decimal("5.00")
    )
    item = db.execute(
        select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == po.id)
    ).scalar_one()

    # Invoiced at 6.00/unit vs. 5.00 received -- an unfavorable variance
    # of 10.00 (10 units * 1.00), posted for real through the same GL
    # path this report reads from.
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 10),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("6.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    rows = reports_service.purchase_price_variance_report(db, store_ids=[store.id])
    total = sum((r.total_variance for r in rows), start=Decimal("0"))
    assert total == Decimal("10.000000")
