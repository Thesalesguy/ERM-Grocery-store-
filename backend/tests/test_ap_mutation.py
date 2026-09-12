"""M6 Session L: mutation-style tests for the AP protections that are
cleanly interceptable at a function/tuple boundary (kept as permanent,
automated regression tests). Store isolation is already a permanent test
in tests/test_ap_api.py
(test_store_isolation_mutation_test_removing_enforce_store_access).

Three protections were instead mutation-tested via a manual, reverted
source-code edit during this audit session rather than as permanent
tests here (the protection in each case is which computation an inline
expression uses, not a call to a separately-patchable function) — see
docs/M6_HARDENING_AUDIT.md "Mutation testing" for the exact edit made,
the test(s) that failed under it, and confirmation of the revert:
overpayment protection, over-invoicing protection, and historical
receipt-cost usage in FIFO matching.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
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
    return po, item


def test_mutation_removing_invoice_idempotency_check_breaks_retry_transparency(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: retrying create_purchase_invoice with the same
    client_transaction_id and the same payload transparently returns the
    original invoice. Neutering
    _match_or_reject_idempotent_invoice makes the retry instead reach the
    (supplier_id, invoice_number) unique constraint and raise
    DUPLICATE_SUPPLIER_INVOICE_NUMBER — proving the check is load-bearing
    for retry safety, not just an optimization (the same class of finding
    M5 made for sale returns)."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00"))
    key = f"itxn-{unique_suffix()}"
    invoice_number = f"INV-{unique_suffix()}"

    def _create():
        return ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=invoice_number,
            invoice_date=date(2024, 1, 5),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
            client_transaction_id=key,
            caller_store_id=None,
        )

    first = _create()
    retry = _create()
    assert retry.id == first.id  # baseline: transparent

    monkeypatch.setattr(ap_service, "_match_or_reject_idempotent_invoice", lambda *a, **kw: None)
    with pytest.raises(ConflictError) as exc_info:
        _create()
    assert exc_info.value.error_code == "DUPLICATE_SUPPLIER_INVOICE_NUMBER"
    db.rollback()

    monkeypatch.undo()
    restored_retry = _create()
    assert restored_retry.id == first.id


def test_mutation_removing_payment_idempotency_check_breaks_retry_transparency(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape as the invoice test above, for record_supplier_payment.
    An invoice paid in full, then retried with the same idempotency key
    and amount, is transparently idempotent at baseline. With
    _match_or_reject_idempotent_payment neutered, the retry instead
    re-runs full validation against the now-fully-paid invoice and is
    rejected (INVALID_INVOICE_STATE, since the invoice is now PAID — the
    exact status-guard-fires-first pattern M5 found for its own
    idempotency race) instead of transparently returning the original
    payment."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00"))
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
    key = f"ptxn-{unique_suffix()}"

    def _pay():
        return ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("100.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("100.00"))],
            client_transaction_id=key,
            caller_store_id=None,
        )

    first = _pay()
    db.commit()
    retry = _pay()
    assert retry.id == first.id  # baseline: transparent

    monkeypatch.setattr(ap_service, "_match_or_reject_idempotent_payment", lambda *a, **kw: None)
    with pytest.raises(ConflictError) as exc_info:
        _pay()
    assert exc_info.value.error_code in ("OVERPAYMENT", "INVALID_INVOICE_STATE")
    db.rollback()

    monkeypatch.undo()
    restored_retry = _pay()
    assert restored_retry.id == first.id


def test_mutation_ap_reconciliation_detects_a_corrupted_invoice_total(db: Session) -> None:
    """Proves ap_reconciliation is a real, working check, not decorative:
    after a clean post (GL AP balance == subledger total, discrepancy 0),
    directly corrupt the invoice's own stored grand_total (simulating a
    bug that let posted financial data become mutable and drift from its
    own journal — something this codebase's normal code paths never do,
    since PurchaseInvoice.grand_total is never touched again after
    create_purchase_invoice) and confirm the reconciliation's discrepancy
    becomes nonzero and points at the corruption, rather than always
    reporting zero regardless of the underlying data."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00"))
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

    baseline = ap_service.ap_reconciliation(db, store_id=store.id)
    assert baseline.discrepancy == Decimal("0.00")

    # Directly corrupt the subledger's own total via raw SQL — bypassing
    # every service-layer guard, simulating data corruption rather than a
    # code path this application would ever normally take. subtotal is
    # bumped by the same amount so the row still satisfies
    # ck_purchase_invoices_grand_total_consistent (an internal
    # self-consistency check, not a check against the GL) — the
    # corruption this test targets is the divergence from the JOURNAL
    # that was already posted for the ORIGINAL total, which no DB
    # constraint can catch (that's exactly what ap_reconciliation exists
    # to catch instead).
    from sqlalchemy import text

    db.execute(
        text(
            "UPDATE purchase_invoices SET grand_total = grand_total + 500, "
            "subtotal = subtotal + 500 WHERE id = :id"
        ),
        {"id": invoice.id},
    )
    db.flush()
    db.expire_all()  # the raw UPDATE above bypassed the ORM's identity map

    corrupted = ap_service.ap_reconciliation(db, store_id=store.id)
    assert corrupted.discrepancy != Decimal("0.00")
    assert abs(corrupted.discrepancy) == Decimal("500.00")
    db.rollback()


def test_mutation_removing_supplier_payment_from_automated_sources_allows_bare_journal_reversal(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: reverse_journal_entry refuses to reverse a
    SUPPLIER_PAYMENT-sourced journal entry directly
    (OPERATIONAL_REVERSAL_REQUIRED). Removing SUPPLIER_PAYMENT from
    AUTOMATED_SOURCE_TYPES (as seen by accounting.service) makes the
    exact same call succeed instead, proving the block is what stops it —
    the same M4/M5-established pattern, now proven for this new M6
    source type specifically."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("5"), cost=Decimal("10.00"))
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("10.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    payment = ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 10),
        payment_method="CASH",
        amount=Decimal("50.00"),
        allocations=[PaymentAllocationInput(invoice.id, Decimal("50.00"))],
        client_transaction_id=f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    from sqlalchemy import select

    from app.modules.accounting.models import JournalEntry

    payment_journal = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_PAYMENT", JournalEntry.source_id == payment.id
        )
    ).scalar_one()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=payment_journal.id,
            reason="attempted direct reversal",
            reversed_by=None,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"
    db.rollback()

    monkeypatch.setattr(
        accounting_service,
        "AUTOMATED_SOURCE_TYPES",
        (
            "SALE",
            "PURCHASE_RECEIPT",
            "PURCHASE_RETURN",
            "SALE_RETURN",
            "STOCK_ADJUSTMENT",
            "PURCHASE_INVOICE",
            "PURCHASE_INVOICE_VOID",
        ),
    )
    reversal = accounting_service.reverse_journal_entry(
        db,
        journal_entry_id=payment_journal.id,
        reason="attempted direct reversal under mutation",
        reversed_by=None,
        caller_store_id=None,
    )
    assert reversal.entry_type == "REVERSAL"
    db.rollback()

    monkeypatch.undo()
    with pytest.raises(ConflictError) as exc_info_restored:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=payment_journal.id,
            reason="attempted direct reversal after restore",
            reversed_by=None,
            caller_store_id=None,
        )
    assert exc_info_restored.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


def test_mutation_removing_credit_note_idempotency_check_breaks_retry_transparency(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: same shape as the invoice/payment idempotency mutation tests
    above, for create_supplier_credit_note. Neutering
    _match_or_reject_idempotent_credit_note makes a transparent retry
    instead hit the (supplier_id, credit_number) unique constraint."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00"))
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
    key = f"ctxn-{unique_suffix()}"
    credit_number = f"CN-{unique_suffix()}"

    def _create():
        return ap_service.create_supplier_credit_note(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            credit_number=credit_number,
            credit_date=date(2024, 1, 12),
            reason="COMMERCIAL_DISCOUNT",
            lines=[SupplierCreditNoteLineInput(description="Discount", amount=Decimal("20.00"))],
            allocations=[PaymentAllocationInput(invoice.id, Decimal("20.00"))],
            client_transaction_id=key,
            caller_store_id=None,
        )

    first = _create()
    db.commit()
    retry = _create()
    assert retry.id == first.id  # baseline: transparent

    monkeypatch.setattr(
        ap_service, "_match_or_reject_idempotent_credit_note", lambda *a, **kw: None
    )
    with pytest.raises(ConflictError) as exc_info:
        _create()
    assert exc_info.value.error_code == "DUPLICATE_SUPPLIER_CREDIT_NUMBER"
    db.rollback()

    monkeypatch.undo()
    restored_retry = _create()
    assert restored_retry.id == first.id


def test_mutation_removing_payment_allocation_bounds_allows_overpayment(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: proves the per-allocation `amount > remaining` OVERPAYMENT
    check in record_supplier_payment is load-bearing. There is no single
    patchable function boundary for this inline check, so — following the
    M6 precedent for overpayment/over-invoicing (see this module's
    docstring) — this monkeypatches `_outstanding_balance` (the one
    function every bound check calls to compute "remaining") to always
    report a huge balance, which is equivalent to removing the check for
    this purpose."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00"))
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

    with pytest.raises(ConflictError) as exc_info:
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("500.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("500.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVERPAYMENT"
    db.rollback()

    # With the application-level guard neutered, the DB's own
    # ck_purchase_invoices_settlement_bounds CHECK constraint is the
    # backstop — it still prevents the corrupted row from ever being
    # written, but as a raw IntegrityError instead of a clean domain
    # error. This is the desired belt-and-suspenders outcome (M6/M7
    # precedent: application checks give a clean rejection; DB
    # constraints are the last-resort backstop against a coding mistake
    # ever reaching the same conclusion this app-level check exists to
    # reach cleanly) — proving BOTH layers matter: the app check for a
    # good error message, the DB check for actual data safety.
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(ap_service, "_outstanding_balance", lambda invoice: Decimal("999999"))
    with pytest.raises(IntegrityError):
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("500.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("500.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    monkeypatch.undo()
    with pytest.raises(ConflictError) as exc_info_restored:
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("500.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("500.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info_restored.value.error_code == "OVERPAYMENT"


def test_mutation_historical_receipt_cost_vs_current_wac_in_fifo_matching(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7 Section 31 target #10: proves post_purchase_invoice prices the
    matched clearing amount from the ACTUAL historical GoodsReceiptItem
    lot cost, never the product's CURRENT (possibly since-changed) WAC.
    Receives at 10.00, then bumps the product's current_cost to 999.00 (a
    later, unrelated receipt/adjustment would do this in real life) before
    posting an invoice against the ORIGINAL receipt — the clearing amount
    posted must still reflect 10.00/unit, not 999.00/unit."""
    from sqlalchemy import select

    from app.modules.accounting.constants import ACCOUNT_PURCHASE_CLEARING
    from app.modules.accounting.models import Account as AccountModel
    from app.modules.accounting.models import JournalEntry, JournalLine

    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _receive(db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00"))

    # Simulate the product's current WAC having since drifted away from
    # this receipt's own historical cost.
    db.refresh(product)
    product.current_cost = Decimal("999.000000")
    db.flush()

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

    account_id = db.execute(
        select(AccountModel.id).where(AccountModel.code == ACCOUNT_PURCHASE_CLEARING)
    ).scalar_one()
    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
        )
    ).scalar_one()
    clearing_line = db.execute(
        select(JournalLine).where(
            JournalLine.journal_entry_id == entry.id, JournalLine.account_id == account_id
        )
    ).scalar_one()
    # 10 units at the RECEIPT's 10.00 cost = 100.00, never at the
    # "current" 999.00 WAC (which would be 9990.00).
    assert clearing_line.debit == Decimal("100.000000")
