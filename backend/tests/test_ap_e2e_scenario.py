"""M7 Session Q: the required comprehensive end-to-end AP financial
scenario (M7 task Section 32), extending M4/M5/M6's own comprehensive-
scenario discipline — every expected value hand-derived BEFORE running the
test, never adjusted after the fact to make an assertion pass.

Scenario:
 1. Create supplier.
 2. Create PO (20 units ordered).
 3. Receive 10 units @ 10.00 (cost A).
 4. Receive 5 more units @ 20.00 (cost B) -> WAC = 13.333333.
 5. First invoice: 10 units @ 10.00 (exact match to receipt A — zero
    variance).
 6. Second invoice: 5 units @ 22.00 (receipt B was 20.00 — a deliberate
    10.00 unfavorable price variance).
 7. Post both invoices; verify Purchase Clearing fully cleared (0) and AP
    established (100.00 + 110.00 = 210.00).
 8. Supplier credit note (COMMERCIAL_DISCOUNT, 10.00) allocated against
    the second invoice — the variance is waived commercially. AP -> 200.00.
 9. Partial payment (40.00) against the first invoice.
10. ONE payment (160.00) allocating across BOTH invoices, settling each
    to PAID — "one payment settles multiple invoices".
11. Supplier statement reconciles to 0 (fully settled).
12. AP aging reconciles to 0 after settlement (a mid-scenario snapshot
    before the final payment shows the real outstanding total instead).
13. Supplier balance verified (0 owed, 210.00 lifetime paid+credited).
14. AP control account == AP subledger (0).
15. Purchase Clearing reconciliation (0).
16. Inventory-to-GL reconciliation (unchanged M4 invariant).
17. COGS remains correct (untouched — nothing sold).
18. Cross-store access attempted.
19. Duplicate invoice attempted.
20. Duplicate payment attempted (idempotent retry).
21. Over-allocation (credit note exceeding an invoice's remaining balance)
    attempted.
22. Overpayment (paying an already-PAID invoice) attempted.
23. Over-invoicing (nothing left to invoice — 15/15 already) attempted.

Real concurrent-request races and forced mid-transaction failures are
covered exhaustively in tests/test_ap_concurrency.py and
tests/test_ap_failure_injection.py respectively — not duplicated here.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError
from app.modules.accounting import service as accounting_service
from app.modules.ap import service as ap_service
from app.modules.ap.service import (
    PaymentAllocationInput,
    PurchaseInvoiceLineInput,
    SupplierCreditNoteLineInput,
)
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
        quantity_ordered=Decimal("20"),
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
    assert product.current_qty_on_hand == Decimal("10.000")
    assert product.current_cost == Decimal("10.000000")
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.discrepancy == Decimal("0.000000")

    # --- 4: receive 5 more @ 20.00 (cost B), same PO item -------------------
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
    # (10*10 + 5*20) / 15 = 200/15 = 13.333333 (ROUND_HALF_UP at 6dp).
    assert product.current_cost == Decimal("13.333333")

    # --- 5: first invoice, exact match to receipt A (zero variance) --------
    invoice1 = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV1-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        due_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"itxn1-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice1.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice1)
    assert invoice1.grand_total == Decimal("100.00")

    # --- 6: second invoice, deliberate unfavorable price variance ----------
    invoice2 = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV2-{unique_suffix()}",
        invoice_date=date(2024, 1, 6),
        due_date=date(2024, 1, 6),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("22.00"))],
        client_transaction_id=f"itxn2-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice2.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice2)
    db.refresh(item)
    assert invoice2.grand_total == Decimal("110.00")  # 5 * 22.00
    assert item.quantity_invoiced == Decimal("15.000")  # fully invoiced

    # --- 7: verify Purchase Clearing cleared + AP established ---------------
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.gl_purchase_clearing_balance == Decimal("0.000000")
    assert clearing_recon.discrepancy == Decimal("0.000000")
    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("210.00")
    assert ap_recon.ap_subledger_total == Decimal("210.00")
    assert ap_recon.discrepancy == Decimal("0.00")

    # --- mid-scenario aging snapshot (before any settlement) ----------------
    aging_before = ap_service.ap_aging(db, store_id=store.id, as_of=date(2024, 2, 1))
    assert len(aging_before) == 1
    assert aging_before[0].total == Decimal("210.00")

    # --- 8: supplier credit note commercially waiving the variance ---------
    credit_note = ap_service.create_supplier_credit_note(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        credit_number=f"CN-{unique_suffix()}",
        credit_date=date(2024, 1, 8),
        reason="COMMERCIAL_DISCOUNT",
        lines=[
            SupplierCreditNoteLineInput(
                description="Waived price variance", amount=Decimal("10.00")
            )
        ],
        allocations=[PaymentAllocationInput(invoice2.id, Decimal("10.00"))],
        client_transaction_id=f"ctxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice2)
    assert credit_note.grand_total == Decimal("10.00")
    assert invoice2.amount_credited == Decimal("10.00")
    assert invoice2.status == "PARTIALLY_PAID"  # settled 10 of 110

    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("200.00")
    assert ap_recon.ap_subledger_total == Decimal("200.00")

    # --- 9: partial payment against invoice1 --------------------------------
    ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 10),
        payment_method="CASH",
        amount=Decimal("40.00"),
        allocations=[PaymentAllocationInput(invoice1.id, Decimal("40.00"))],
        client_transaction_id=f"ptxn-a-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice1)
    assert invoice1.status == "PARTIALLY_PAID"
    assert invoice1.amount_paid == Decimal("40.00")

    # --- 21 (raised here, before final settlement, while a real remaining
    # balance exists to over-allocate against): over-allocation attempted -
    with pytest.raises(ConflictError) as exc_info:
        ap_service.create_supplier_credit_note(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            credit_number=f"CN-OVER-{unique_suffix()}",
            credit_date=date(2024, 1, 11),
            reason="COMMERCIAL_DISCOUNT",
            lines=[SupplierCreditNoteLineInput(description="Too much", amount=Decimal("9999.00"))],
            allocations=[PaymentAllocationInput(invoice1.id, Decimal("9999.00"))],
            client_transaction_id=f"ctxn-over-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_ALLOCATION"

    # --- 10: ONE payment settling BOTH remaining invoice balances ----------
    # invoice1 remaining: 100 - 40 = 60. invoice2 remaining: 110 - 10 = 100.
    final_payment_key = f"ptxn-b-{unique_suffix()}"
    ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 15),
        payment_method="BANK_TRANSFER",
        amount=Decimal("160.00"),
        allocations=[
            PaymentAllocationInput(invoice1.id, Decimal("60.00")),
            PaymentAllocationInput(invoice2.id, Decimal("100.00")),
        ],
        client_transaction_id=final_payment_key,
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice1)
    db.refresh(invoice2)
    assert invoice1.status == "PAID"
    assert invoice2.status == "PAID"
    assert invoice1.amount_paid == Decimal("100.00")
    assert invoice2.amount_paid + invoice2.amount_credited == Decimal("110.00")

    # --- 11: supplier statement reconciles to 0 -----------------------------
    statement = ap_service.get_supplier_statement(db, supplier.id)
    assert statement.opening_balance == Decimal("0.00")
    assert statement.closing_balance == Decimal("0.00")
    # invoice1 (+100) + invoice2 (+110) + credit (-10) + paymentA (-40) +
    # paymentB (-160) = 0, in chronological order.
    assert len(statement.lines) == 5
    assert [line.transaction_type for line in statement.lines] == [
        "INVOICE",
        "INVOICE",
        "CREDIT_NOTE",
        "PAYMENT",
        "PAYMENT",
    ]
    assert statement.lines[-1].running_balance == Decimal("0.00")

    # --- 12: AP aging after settlement is empty -----------------------------
    aging_after = ap_service.ap_aging(db, store_id=store.id, as_of=date(2024, 2, 1))
    assert aging_after == []

    # --- 13: supplier balance verified ---------------------------------------
    summary = ap_service.get_supplier_ap_summary(db, supplier.id)
    assert summary.total_owed == Decimal("0.00")
    assert summary.total_paid == Decimal("200.00")  # 40 + 60 + 100
    assert summary.total_credited == Decimal("10.00")
    assert summary.outstanding_purchase_clearing == Decimal("0.000000")

    # --- 14: AP control account == AP subledger -------------------------------
    ap_recon = ap_service.ap_reconciliation(db, store_id=store.id)
    assert ap_recon.gl_accounts_payable_balance == Decimal("0.00")
    assert ap_recon.ap_subledger_total == Decimal("0.00")
    assert ap_recon.discrepancy == Decimal("0.00")

    # --- 15: Purchase Clearing reconciliation ---------------------------------
    clearing_recon = ap_service.purchase_clearing_reconciliation(db, store_id=store.id)
    assert clearing_recon.discrepancy == Decimal("0.000000")

    # --- 16: Inventory-to-GL reconciliation (unchanged M4 invariant) ---------
    inv_recon = accounting_service.inventory_reconciliation(db, store_id=store.id)
    row = inv_recon[0]
    assert row.gl_inventory_balance == Decimal("200.000000")
    operational_valuation = product.current_qty_on_hand * product.current_cost
    assert row.operational_valuation == operational_valuation.quantize(Decimal("0.000001"))
    assert abs(row.discrepancy) <= Decimal("0.00001")

    # --- 17: COGS remains correct (untouched by AP postings) --------------------
    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.cogs == Decimal("0")  # nothing was ever sold in this scenario

    # --- 18: cross-store access attempted -------------------------------------
    other_store = make_store(db)
    db.commit()
    with pytest.raises(ForbiddenError) as exc_info:
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 20),
            payment_method="CASH",
            amount=Decimal("1.00"),
            allocations=[PaymentAllocationInput(invoice1.id, Decimal("1.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=other_store.id,  # wrong store
        )
    assert exc_info.value.error_code == "STORE_ACCESS_DENIED"
    with pytest.raises(ForbiddenError):
        ap_service.void_purchase_invoice(
            db, purchase_invoice_id=invoice1.id, caller_store_id=other_store.id
        )

    # --- 19: duplicate invoice attempted -----------------------------------------
    with pytest.raises(ConflictError) as exc_info:
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=invoice1.invoice_number,  # SAME supplier + number
            invoice_date=date(2024, 1, 6),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("1"), Decimal("10.00"))],
            client_transaction_id=f"itxn-{unique_suffix()}",  # different key
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "DUPLICATE_SUPPLIER_INVOICE_NUMBER"

    # --- 20: duplicate payment attempted (idempotent retry) -----------------------
    retry_payment = ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 15),
        payment_method="BANK_TRANSFER",
        amount=Decimal("160.00"),
        allocations=[
            PaymentAllocationInput(invoice1.id, Decimal("60.00")),
            PaymentAllocationInput(invoice2.id, Decimal("100.00")),
        ],
        client_transaction_id=final_payment_key,  # SAME key as step 10
        caller_store_id=None,
    )
    db.commit()
    db.refresh(invoice1)
    assert invoice1.amount_paid == Decimal("100.00")  # NOT 160 — not double-applied
    assert retry_payment.amount == Decimal("160.00")

    # --- 22: overpayment (invoice1 is already fully PAID) -------------------------
    with pytest.raises(ConflictError) as exc_info:
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 20),
            payment_method="CASH",
            amount=Decimal("1.00"),
            allocations=[PaymentAllocationInput(invoice1.id, Decimal("1.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code in ("OVERPAYMENT", "INVALID_INVOICE_STATE")
    db.refresh(invoice1)
    assert invoice1.amount_paid == Decimal("100.00")  # unaffected

    # --- 23: over-invoicing (nothing left to invoice — 15/15 already) -------------
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
