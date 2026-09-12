"""M6/M7 Session F: supplier payment domain logic and accounting.

Focus: a payment reduces AP by exactly the amount recorded, never more
than the invoice's own remaining balance, and posts Dr Accounts Payable /
Cr <the real asset account the payment method maps to> — a mapping
deliberately separate from the sales-side payment-method accounts (see
accounting/constants.py SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE).

M7: a payment is now a header + PaymentAllocationInput rows
(docs/M7_ADVANCED_AP_SETTLEMENT.md Section 8/9) — every call below
allocates to exactly one invoice (the M6 shape), proving the new API is a
strict superset; test_ap_e2e_scenario.py and dedicated M7 tests exercise
genuine multi-invoice/multi-payment allocation.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.modules.accounting.constants import (
    ACCOUNT_ACCOUNTS_PAYABLE,
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_CASH_ON_HAND,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
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


def _make_posted_invoice(db: Session, store, supplier, product, *, qty: Decimal, cost: Decimal):
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
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, qty, cost)],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice)
    return invoice


def _pay(
    db: Session,
    *,
    invoice,
    store,
    supplier,
    amount: Decimal,
    payment_method: str = "CASH",
    payment_date_=date(2024, 1, 10),
    client_transaction_id: str | None = None,
):
    return ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=payment_date_,
        payment_method=payment_method,
        amount=amount,
        allocations=[PaymentAllocationInput(invoice.id, amount)],
        client_transaction_id=client_transaction_id or f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )


def _lines_for(db: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id)).scalars()
    )


def _assert_balanced(db: Session, entry: JournalEntry) -> None:
    lines = _lines_for(db, entry)
    assert lines
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) > 0


def _line_amount(db: Session, entry: JournalEntry, code: str) -> Decimal:
    account_id = db.execute(select(Account.id).where(Account.code == code)).scalar_one()
    return sum(
        (
            line.debit - line.credit
            for line in _lines_for(db, entry)
            if line.account_id == account_id
        ),
        Decimal("0"),
    )


def test_partial_payment_moves_invoice_to_partially_paid(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )
    assert invoice.grand_total == Decimal("100.00")

    payment = _pay(db, invoice=invoice, store=store, supplier=supplier, amount=Decimal("40.00"))
    db.commit()
    db.refresh(invoice)
    assert invoice.status == "PARTIALLY_PAID"
    assert invoice.amount_paid == Decimal("40.00")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_PAYMENT", JournalEntry.source_id == payment.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    # Dr Accounts Payable / Cr Cash on Hand.
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("40.000000")
    assert _line_amount(db, entry, ACCOUNT_CASH_ON_HAND) == Decimal("-40.000000")


def test_full_payment_across_two_installments_marks_invoice_paid(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    _pay(
        db,
        invoice=invoice,
        store=store,
        supplier=supplier,
        amount=Decimal("60.00"),
        payment_method="BANK_TRANSFER",
    )
    db.commit()
    db.refresh(invoice)
    assert invoice.status == "PARTIALLY_PAID"

    second = _pay(
        db,
        invoice=invoice,
        store=store,
        supplier=supplier,
        amount=Decimal("40.00"),
        payment_method="BANK_TRANSFER",
        payment_date_=date(2024, 1, 15),
    )
    db.commit()
    db.refresh(invoice)
    assert invoice.status == "PAID"
    assert invoice.amount_paid == invoice.grand_total

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_PAYMENT", JournalEntry.source_id == second.id
        )
    ).scalar_one()
    # Dr Accounts Payable / Cr Bank Account.
    assert _line_amount(db, entry, ACCOUNT_BANK_ACCOUNT) == Decimal("-40.000000")


def test_overpayment_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    with pytest.raises(ConflictError) as exc_info:
        _pay(db, invoice=invoice, store=store, supplier=supplier, amount=Decimal("150.00"))
    assert exc_info.value.error_code == "OVERPAYMENT"
    db.rollback()
    db.refresh(invoice)
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.status == "POSTED"


def test_second_payment_cannot_exceed_remaining_balance(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    _pay(db, invoice=invoice, store=store, supplier=supplier, amount=Decimal("80.00"))
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        _pay(
            db,
            invoice=invoice,
            store=store,
            supplier=supplier,
            amount=Decimal("30.00"),  # only 20 remains
            payment_date_=date(2024, 1, 12),
        )
    assert exc_info.value.error_code == "OVERPAYMENT"


def test_payment_against_draft_invoice_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("5"),
        unit_cost=Decimal("2.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("2.00"))],
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
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("2.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    # still DRAFT — never posted
    with pytest.raises(ConflictError) as exc_info:
        _pay(db, invoice=invoice, store=store, supplier=supplier, amount=Decimal("5.00"))
    assert exc_info.value.error_code == "INVALID_INVOICE_STATE"


def test_idempotent_payment_returns_same_row_for_same_payload(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )
    key = f"ptxn-{unique_suffix()}"

    first = _pay(
        db,
        invoice=invoice,
        store=store,
        supplier=supplier,
        amount=Decimal("50.00"),
        client_transaction_id=key,
    )
    db.commit()
    second = _pay(
        db,
        invoice=invoice,
        store=store,
        supplier=supplier,
        amount=Decimal("50.00"),
        client_transaction_id=key,
    )
    assert first.id == second.id
    db.refresh(invoice)
    assert invoice.amount_paid == Decimal("50.00")  # NOT double-applied


def test_conflicting_payload_with_same_payment_idempotency_key_is_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )
    key = f"ptxn-{unique_suffix()}"

    _pay(
        db,
        invoice=invoice,
        store=store,
        supplier=supplier,
        amount=Decimal("50.00"),
        client_transaction_id=key,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        _pay(
            db,
            invoice=invoice,
            store=store,
            supplier=supplier,
            amount=Decimal("60.00"),  # different amount
            client_transaction_id=key,
        )
    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_CONFLICT"


def test_cannot_void_invoice_with_a_payment_recorded(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )
    _pay(db, invoice=invoice, store=store, supplier=supplier, amount=Decimal("10.00"))
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    assert exc_info.value.error_code == "INVOICE_HAS_PAYMENTS"


def test_allocations_must_sum_to_payment_amount(db: Session) -> None:
    """M7 (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 13): an unapplied
    remainder is rejected explicitly, never silently accepted as an
    advance."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    invoice = _make_posted_invoice(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    with pytest.raises(Exception) as exc_info:
        ap_service.record_supplier_payment(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("100.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("40.00"))],
            client_transaction_id=f"ptxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert getattr(exc_info.value, "error_code", None) == "ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT"
