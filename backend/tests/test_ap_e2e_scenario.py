"""M6 Session M: the required comprehensive end-to-end AP financial
scenario (M6 task Section 29), extending M4/M5's own comprehensive-
scenario discipline — every expected value hand-derived BEFORE running
the test, never adjusted after the fact to make an assertion pass.

Scenario:
 1. Create supplier.
 2. Create PO.
 3. Receive 10 units @ 10.00 (cost A).
 4. Verify Inventory and Purchase Clearing.
 5. Receive 5 more units @ 20.00 (cost B), same PO item.
 6. Verify WAC.
 7. Create a supplier invoice matching the receipts EXACTLY (two lines,
    one per receipt cost: 10 @ 10.00 and 5 @ 20.00 — zero price variance
    by construction, so downstream reconciliation numbers stay simple).
 8. Post the invoice.
 9. Verify Purchase Clearing is correctly cleared (back to 0 — fully
    matched).
10. Verify the AP liability (200.00).
11. Record a partial payment (120.00).
12. Verify the AP remaining balance (80.00).
13. Record a second payment (80.00).
14. Verify the invoice becomes PAID.
15. Verify the supplier balance.
16. Verify AP control account == AP subledger.
17. Verify Purchase Clearing reconciliation.
18. Verify Inventory-to-GL reconciliation (unchanged M4 invariant, bounded
    WAC-rounding drift only).
19. Verify COGS remains correct (untouched by AP — nothing sold, so 0).
20. Attempt a duplicate invoice (same supplier+invoice_number, new key).
21. Attempt a duplicate payment (same idempotency key).
22. Attempt cross-store access.
23. Attempt overpayment.
24. Attempt over-invoicing.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError
from app.modules.accounting import service as accounting_service
from app.modules.ap import service as ap_service
from app.modules.ap.service import PurchaseInvoiceLineInput
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


def test_full_ap_lifecycle_end_to_end(db: Session) -> None:
    # --- 1-2: supplier + PO ------------------------------------------------
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("15"),
        unit_cost=Decimal("10.00"),
    )
    db.add(item)
    db.commit()

    # --- 3: receive 10 @ 10.00 (cost A) -------------------------------------
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    db.refresh(item)

    # --- 4: verify Inventory and Purchase Clearing --------------------------
    assert product.current_qty_on_hand == Decimal("10.000")
    assert product.current_cost == Decimal("10.000000")
    inv_recon = accounting_service.inventory_reconciliation(db, store_id=store.id)
    assert inv_recon[0].gl_inventory_balance == Decimal("100.000000")
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.gl_purchase_clearing_balance == Decimal("100.000000")
    assert clearing_recon.outstanding_clearing_total == Decimal("100.000000")
    assert clearing_recon.discrepancy == Decimal("0.000000")

    # --- 5: receive 5 more @ 20.00 (cost B), same PO item -------------------
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("20.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    db.refresh(item)

    # --- 6: verify WAC -------------------------------------------------------
    # (10*10 + 5*20) / 15 = 200/15 = 13.333333 (ROUND_HALF_UP at 6dp).
    assert product.current_qty_on_hand == Decimal("15.000")
    assert product.current_cost == Decimal("13.333333")
    assert item.quantity_received == Decimal("15.000")

    # --- 7: create a supplier invoice matching the receipts EXACTLY --------
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[
            PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("10.00")),
            PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("20.00")),
        ],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    assert invoice.status == "DRAFT"
    assert invoice.grand_total == Decimal("200.00")

    # --- 8: post the invoice --------------------------------------------------
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice)
    db.refresh(item)
    assert invoice.status == "POSTED"
    assert item.quantity_invoiced == Decimal("15.000")

    # --- 9: verify Purchase Clearing is correctly cleared ---------------------
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.gl_purchase_clearing_balance == Decimal("0.000000")
    assert clearing_recon.outstanding_clearing_total == Decimal("0.000000")
    assert clearing_recon.discrepancy == Decimal("0.000000")

    # --- 10: verify the AP liability -------------------------------------------
    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("200.00")
    assert ap_recon.ap_subledger_total == Decimal("200.00")
    assert ap_recon.discrepancy == Decimal("0.00")

    # --- 11: record a partial payment -------------------------------------------
    ap_service.record_supplier_payment(
        db,
        purchase_invoice_id=invoice.id,
        store_id=store.id,
        payment_date=date(2024, 1, 10),
        payment_method="CASH",
        amount=Decimal("120.00"),
        client_transaction_id=f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice)

    # --- 12: verify the AP remaining balance -------------------------------------
    assert invoice.status == "PARTIALLY_PAID"
    assert invoice.amount_paid == Decimal("120.00")
    assert invoice.grand_total - invoice.amount_paid == Decimal("80.00")

    # --- 13: record a second payment --------------------------------------------
    second_payment_key = f"ptxn-{unique_suffix()}"
    ap_service.record_supplier_payment(
        db,
        purchase_invoice_id=invoice.id,
        store_id=store.id,
        payment_date=date(2024, 1, 15),
        payment_method="BANK_TRANSFER",
        amount=Decimal("80.00"),
        client_transaction_id=second_payment_key,
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice)

    # --- 14: verify the invoice becomes PAID -------------------------------------
    assert invoice.status == "PAID"
    assert invoice.amount_paid == invoice.grand_total == Decimal("200.00")

    # --- 15: verify the supplier balance -------------------------------------------
    summary = ap_service.get_supplier_ap_summary(db, supplier.id)
    assert summary.total_owed == Decimal("0.00")
    assert summary.total_paid == Decimal("200.00")
    assert summary.outstanding_purchase_clearing == Decimal("0.000000")

    # --- 16: verify AP control account == AP subledger -----------------------
    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("0.00")
    assert ap_recon.ap_subledger_total == Decimal("0.00")
    assert ap_recon.discrepancy == Decimal("0.00")

    # --- 17: verify Purchase Clearing reconciliation -----------------------------
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.discrepancy == Decimal("0.000000")

    # --- 18: verify Inventory-to-GL reconciliation (unchanged M4 invariant) ------
    inv_recon = accounting_service.inventory_reconciliation(db, store_id=store.id)
    row = inv_recon[0]
    # GL Inventory = 100 + 100 = 200.000000 exactly (each receipt's own
    # quantized Dr Inventory line); operational valuation = 15 * 13.333333
    # = 199.999995 — the same, already-documented, bounded WAC-rounding
    # drift M4/M5 established, NOT a new or widened tolerance.
    assert row.gl_inventory_balance == Decimal("200.000000")
    operational_valuation = product.current_qty_on_hand * product.current_cost
    assert row.operational_valuation == operational_valuation.quantize(Decimal("0.000001"))
    assert abs(row.discrepancy) <= Decimal("0.00001")

    # --- 19: verify COGS remains correct (untouched by AP postings) --------------
    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.cogs == Decimal("0")  # nothing was ever sold in this scenario

    # --- 20: attempt a duplicate invoice -------------------------------------------
    with pytest.raises(ConflictError) as exc_info:
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=invoice.invoice_number,  # SAME supplier + number
            invoice_date=date(2024, 1, 6),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("1"), Decimal("10.00"))],
            client_transaction_id=f"itxn-{unique_suffix()}",  # different key
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "DUPLICATE_SUPPLIER_INVOICE_NUMBER"

    # --- 21: attempt a duplicate payment ---------------------------------------------
    retry_payment = ap_service.record_supplier_payment(
        db,
        purchase_invoice_id=invoice.id,
        store_id=store.id,
        payment_date=date(2024, 1, 15),
        payment_method="BANK_TRANSFER",
        amount=Decimal("80.00"),
        client_transaction_id=second_payment_key,  # SAME key as step 13
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice)
    assert invoice.amount_paid == Decimal("200.00")  # NOT 280 — not double-applied
    assert retry_payment.amount == Decimal("80.00")

    # --- 22: attempt cross-store access -------------------------------------------
    other_store = make_store(db)
    db.commit()
    with pytest.raises(ForbiddenError) as exc_info:
        ap_service.record_supplier_payment(
            db,
            purchase_invoice_id=invoice.id,
            store_id=store.id,
            payment_date=date(2024, 1, 20),
            payment_method="CASH",
            amount=Decimal("1.00"),
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=other_store.id,  # wrong store
        )
    assert exc_info.value.error_code == "STORE_ACCESS_DENIED"
    with pytest.raises(ForbiddenError):
        ap_service.void_purchase_invoice(
            db, purchase_invoice_id=invoice.id, caller_store_id=other_store.id
        )

    # --- 23: attempt overpayment (invoice is already fully PAID) -----------------
    with pytest.raises(ConflictError) as exc_info:
        ap_service.record_supplier_payment(
            db,
            purchase_invoice_id=invoice.id,
            store_id=store.id,
            payment_date=date(2024, 1, 20),
            payment_method="CASH",
            amount=Decimal("1.00"),
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code in ("OVERPAYMENT", "INVALID_INVOICE_STATE")
    db.refresh(invoice)
    assert invoice.amount_paid == Decimal("200.00")  # unaffected

    # --- 24: attempt over-invoicing (nothing left to invoice — 15/15 already) ----
    extra_invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-EXTRA-{unique_suffix()}",
        invoice_date=date(2024, 1, 25),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("1"), Decimal("10.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    with pytest.raises(ConflictError) as exc_info:
        ap_service.post_purchase_invoice(
            db, purchase_invoice_id=extra_invoice.id, caller_store_id=None
        )
    assert exc_info.value.error_code == "OVER_INVOICING"
