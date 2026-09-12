"""M6 Session G: AP subledger vs GL reconciliation, Purchase Clearing
reconciliation, supplier AP summary, aging, and transaction history.

Focus: every figure is independently hand-derived before running the
test (matching the M4/M5 reconciliation-test discipline) — never
adjusted after the fact to make a test pass.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.ap import service as ap_service
from app.modules.ap.service import PaymentAllocationInput, PurchaseInvoiceLineInput
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


def test_ap_and_purchase_clearing_reconciliation_after_partial_invoice_and_payment(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()

    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("10.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    # GL Purchase Clearing: +100 (Dr Inventory / Cr Purchase Clearing)

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        due_date=date(2024, 1, 5),  # due immediately, so it is overdue "as_of" anything after
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("10.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    # Invoice1: clearing=60 (Dr Purchase Clearing 60 / Cr AP 60)
    # GL Purchase Clearing: 100 - 60 = 40. GL AP: 60.

    ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 10),
        payment_method="CASH",
        amount=Decimal("20.00"),
        allocations=[PaymentAllocationInput(invoice.id, Decimal("20.00"))],
        client_transaction_id=f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    # GL AP: 60 - 20 = 40.

    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("40.00")
    assert ap_recon.ap_subledger_total == Decimal("40.00")  # 60 grand_total - 20 paid
    assert ap_recon.discrepancy == Decimal("0.00")

    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.gl_purchase_clearing_balance == Decimal("40.000000")
    # 4 units still uninvoiced, at their receipt cost of 10.00 each = 40.
    assert clearing_recon.outstanding_clearing_total == Decimal("40.000000")
    assert clearing_recon.discrepancy == Decimal("0.000000")

    summary = ap_service.get_supplier_ap_summary(db, supplier.id, as_of=date(2024, 2, 1))
    assert summary.total_owed == Decimal("40.00")
    assert summary.total_overdue == Decimal("40.00")  # due 2024-01-05, as_of 2024-02-01
    assert summary.total_current == Decimal("0.00")
    assert summary.total_paid == Decimal("20.00")
    assert summary.outstanding_purchase_clearing == Decimal("40.000000")

    aging = ap_service.ap_aging(db, store_id=store.id, as_of=date(2024, 2, 1))
    assert len(aging) == 1
    row = aging[0]
    assert row.supplier_id == supplier.id
    # 2024-01-05 to 2024-02-01 = 27 days overdue -> the 1-30 bucket.
    assert row.days_1_30 == Decimal("40.00")
    assert row.current == Decimal("0.00")
    assert row.total == Decimal("40.00")

    history = ap_service.get_supplier_transaction_history(db, supplier.id)
    assert len(history) == 2
    invoice_tx = next(t for t in history if t.transaction_type == "INVOICE")
    payment_tx = next(t for t in history if t.transaction_type == "PAYMENT")
    assert invoice_tx.amount == Decimal("60.00")
    assert payment_tx.amount == Decimal("20.00")


def test_ap_summary_current_bucket_for_not_yet_due_invoice(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("5"),
        unit_cost=Decimal("4.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("4.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        due_date=date(2024, 1, 5) + timedelta(days=30),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("4.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    summary = ap_service.get_supplier_ap_summary(db, supplier.id, as_of=date(2024, 1, 6))
    assert summary.total_owed == Decimal("20.00")
    assert summary.total_overdue == Decimal("0.00")
    assert summary.total_current == Decimal("20.00")

    aging = ap_service.ap_aging(db, store_id=store.id, as_of=date(2024, 1, 6))
    assert aging[0].current == Decimal("20.00")


def test_supplier_statement_orders_same_day_events_by_created_at_not_row_id(
    db: Session,
) -> None:
    """Regression test for a real bug caught by a live smoke test: an
    invoice, a payment, and a credit note are three INDEPENDENT
    auto-increment sequences, so comparing their raw `id` columns across
    tables to order same-day events is meaningless — whichever table's
    sequence happened to be lower would sort first regardless of actual
    creation order. get_supplier_statement must instead order by
    `created_at` (real insertion order).

    Which of the two tables' sequences is numerically ahead depends on
    the whole shared test database's history, not on anything this test
    controls — so instead of assuming a direction, it creates both rows,
    checks which one actually got the higher id, and backdates THAT one's
    created_at to be earlier. This guarantees the correct (created_at)
    order always disagrees with the (wrong) id order, deterministically,
    regardless of which table happened to be ahead this run."""
    from sqlalchemy import text

    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("10.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    payment = ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 10),  # same calendar day as the credit note below
        payment_method="CASH",
        amount=Decimal("20.00"),
        allocations=[PaymentAllocationInput(invoice.id, Decimal("20.00"))],
        client_transaction_id=f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    credit_note = ap_service.create_supplier_credit_note(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        credit_number=f"CN-{unique_suffix()}",
        credit_date=date(2024, 1, 10),
        reason="COMMERCIAL_DISCOUNT",
        lines=[
            ap_service.SupplierCreditNoteLineInput(description="Discount", amount=Decimal("5.00"))
        ],
        allocations=[PaymentAllocationInput(invoice.id, Decimal("5.00"))],
        client_transaction_id=f"ctxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    # payment.id and credit_note.id come from two INDEPENDENT sequences —
    # which one is numerically lower depends on how many rows each table
    # has ever had across the whole shared test database, not on which
    # was created first here. To make this test deterministic regardless
    # of that history, force whichever row has the HIGHER id to have the
    # EARLIER created_at — guaranteeing the correct (created_at) order
    # always disagrees with the (wrong) id order, in whichever direction
    # that happens to be this run.
    if credit_note.id > payment.id:
        backdated, other = "supplier_credit_notes", credit_note
        expected_order = ["CREDIT_NOTE", "PAYMENT"]
    else:
        backdated, other = "supplier_payments", payment
        expected_order = ["PAYMENT", "CREDIT_NOTE"]
    db.execute(
        text(f"UPDATE {backdated} SET created_at = created_at - INTERVAL '1 hour' WHERE id = :id"),
        {"id": other.id},
    )
    db.commit()
    db.expire_all()

    statement = ap_service.get_supplier_statement(db, supplier.id)
    same_day_lines = [line for line in statement.lines if line.date_ == date(2024, 1, 10)]
    assert [line.transaction_type for line in same_day_lines] == expected_order
    assert statement.closing_balance == Decimal("75.00")  # 100 - 5 - 20
