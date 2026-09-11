"""M6 Sessions B/C/D/E: purchase invoice domain/lifecycle logic, three-way
matching against receipts, and Purchase Clearing/AP accounting.

Focus: an invoice never invents its own valuation — clearing_amount is
always priced at the ACTUAL recorded cost of the specific GoodsReceiptItem
lot(s) an invoiced quantity falls into (FIFO across possibly several
receipts at different costs, the same WAC-driving scenario M3 already
supports), price variance is the difference between that and the
invoice's own stated unit price, and every posted journal balances by
construction from those two numbers plus the invoice's own stored
tax/discount/grand_total.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationAppError
from app.modules.accounting.constants import (
    ACCOUNT_ACCOUNTS_PAYABLE,
    ACCOUNT_PURCHASE_CLEARING,
    ACCOUNT_PURCHASE_DISCOUNTS,
    ACCOUNT_PURCHASE_PRICE_VARIANCE,
    ACCOUNT_PURCHASE_TAX_EXPENSE,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.ap import service as ap_service
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrder, PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    unique_suffix,
)


def _order_and_receive(
    db: Session, store, supplier, product, *, qty: Decimal, cost: Decimal, received_date=None
) -> tuple[PurchaseOrder, PurchaseOrderItem]:
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=received_date or date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, qty, cost)],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    return po, item


def _lines_for(db: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id)).scalars()
    )


def _assert_balanced(db: Session, entry: JournalEntry) -> None:
    lines = _lines_for(db, entry)
    assert lines
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) > 0


def _account_id(db: Session, code: str) -> int:
    return db.execute(select(Account.id).where(Account.code == code)).scalar_one()


def _line_amount(db: Session, entry: JournalEntry, code: str) -> Decimal:
    account_id = _account_id(db, code)
    lines = [line for line in _lines_for(db, entry) if line.account_id == account_id]
    return sum((line.debit - line.credit for line in lines), Decimal("0"))


# --- Creation (DRAFT) --------------------------------------------------------


def test_create_purchase_invoice_is_a_draft_with_no_side_effects(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )

    assert invoice.status == "DRAFT"
    assert invoice.grand_total == Decimal("50.00")
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")  # DRAFT has no matching effect
    # Scoped to THIS invoice's own id — the `db` fixture shares a real
    # database with the concurrency tests (which commit for real, not via
    # savepoint), so an unscoped query would also see unrelated rows from
    # other test files.
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE",
                JournalEntry.source_id == invoice.id,
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_due_date_defaults_from_supplier_payment_terms(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db, default_payment_terms_days=30)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("5"), cost=Decimal("2.00")
    )

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
    assert invoice.due_date == date(2024, 1, 5) + timedelta(days=30)


def test_duplicate_supplier_invoice_number_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice_number = f"DUP-{unique_suffix()}"

    ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=invoice_number,
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )

    with pytest.raises(ConflictError) as exc_info:
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=invoice_number,  # same supplier + same number
            invoice_date=date(2024, 1, 6),
            lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("5.00"))],
            client_transaction_id=f"itxn-{unique_suffix()}",  # DIFFERENT key
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "DUPLICATE_SUPPLIER_INVOICE_NUMBER"


def test_same_invoice_number_allowed_across_different_suppliers(db: Session) -> None:
    store = make_store(db)
    supplier_a = make_supplier(db)
    supplier_b = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po_a, item_a = _order_and_receive(
        db, store, supplier_a, product, qty=Decimal("5"), cost=Decimal("5.00")
    )
    po_b, item_b = _order_and_receive(
        db, store, supplier_b, product, qty=Decimal("5"), cost=Decimal("5.00")
    )
    shared_number = "INV-0001"

    inv_a = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier_a.id,
        purchase_order_id=po_a.id,
        invoice_number=shared_number,
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item_a.id, Decimal("5"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    inv_b = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier_b.id,
        purchase_order_id=po_b.id,
        invoice_number=shared_number,
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item_b.id, Decimal("5"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    assert inv_a.invoice_number == inv_b.invoice_number == shared_number
    assert inv_a.id != inv_b.id


def test_idempotent_create_returns_same_invoice_for_same_payload(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    key = f"itxn-{unique_suffix()}"

    def _create():
        return ap_service.create_purchase_invoice(
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

    first = _create()
    second = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=first.invoice_number,
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=key,
        caller_store_id=None,
    )
    assert first.id == second.id


def test_conflicting_payload_with_same_idempotency_key_is_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    key = f"itxn-{unique_suffix()}"

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
    with pytest.raises(ConflictError) as exc_info:
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-{unique_suffix()}",
            invoice_date=date(2024, 1, 5),
            lines=[
                PurchaseInvoiceLineInput(item.id, Decimal("9"), Decimal("5.00"))
            ],  # different qty
            client_transaction_id=key,  # SAME key
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_CONFLICT"


def test_empty_invoice_rejected(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    with pytest.raises(ValidationAppError) as exc_info:
        ap_service.create_purchase_invoice(
            db,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-{unique_suffix()}",
            invoice_date=date(2024, 1, 5),
            lines=[],
            client_transaction_id=f"itxn-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "EMPTY_INVOICE"


# --- Posting: clean match ----------------------------------------------------


def test_post_purchase_invoice_clean_match_establishes_ap_and_clears_purchase_clearing(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    posted = ap_service.post_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()

    assert posted.status == "POSTED"
    assert posted.posted_at is not None
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_CLEARING) == Decimal("50.000000")
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("-50.000000")
    # No variance/tax/discount on a clean match at the same cost.
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_PRICE_VARIANCE) == Decimal("0")


def test_posting_is_idempotent_by_state(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("5"), cost=Decimal("3.00")
    )
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("5"), Decimal("3.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    first = ap_service.post_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()
    second = ap_service.post_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    assert first.id == second.id == invoice.id
    assert second.status == "POSTED"

    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
            )
        )
        .scalars()
        .all()
    )
    assert len(journals) == 1  # posting twice never double-posts


def test_over_invoicing_rejected_at_posting_not_creation(db: Session) -> None:
    """M6 task Section 4's exact example: PO=100, Received=80,
    Invoice=100 must not silently become a valid matched transaction."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("100"),
        unit_cost=Decimal("10.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("80"), Decimal("10.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)

    # Creation (a DRAFT recording what the paperwork says) succeeds even
    # though it will later fail to post — this is deliberate (see
    # create_purchase_invoice's docstring).
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("100"), Decimal("10.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    assert invoice.status == "DRAFT"

    with pytest.raises(ConflictError) as exc_info:
        ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    assert exc_info.value.error_code == "OVER_INVOICING"
    db.rollback()

    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")  # nothing committed


def test_partial_invoicing_leaves_po_open_for_more(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("6.000")

    invoice2 = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 6),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("4"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice2.id, caller_store_id=None)
    db.commit()
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")  # exactly fully invoiced now


# --- FIFO multi-receipt matching and price variance --------------------------


def test_fifo_matching_across_two_receipts_at_different_costs(db: Session) -> None:
    """Receive 10 @ 10.00, then 5 @ 20.00 against the SAME PO item (the
    exact WAC-driving multi-receipt scenario M3 already supports).
    Invoice 12 units at a price of 15.00 each. Clearing must price the
    first 10 units at 10.00 (the first receipt lot) and the next 2 units
    at 20.00 (the second lot): clearing = 10*10 + 2*20 = 140.00.
    Invoiced value = 12*15 = 180.00. Price variance = 180-140 = +40.00
    (unfavorable — invoiced higher than the FIFO-matched receipt cost)."""
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
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("20.00"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    assert item.quantity_received == Decimal("15.000")

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("12"), Decimal("15.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_CLEARING) == Decimal("140.000000")
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_PRICE_VARIANCE) == Decimal("40.000000")
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("-180.000000")


def test_favorable_price_variance_credits_the_variance_account(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    # Invoiced CHEAPER than received (a favorable variance).
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("9.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    # Favorable: invoiced value (90) < clearing (100) -> variance = -10,
    # posted as a CREDIT to the variance account (net -10 in debit-credit terms).
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_PRICE_VARIANCE) == Decimal("-10.000000")
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("-90.000000")


def test_invoice_tax_and_discount_post_to_their_own_accounts(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("10.00")
    )

    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[
            PurchaseInvoiceLineInput(
                item.id,
                Decimal("10"),
                Decimal("10.00"),
                discount_amount=Decimal("5.00"),
                tax_amount=Decimal("8.00"),
            )
        ],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    # subtotal=100, discount=5, tax=8, grand_total=103
    assert invoice.grand_total == Decimal("103.00")

    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE", JournalEntry.source_id == invoice.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_CLEARING) == Decimal("100.000000")
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_TAX_EXPENSE) == Decimal("8.000000")
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_DISCOUNTS) == Decimal("-5.000000")
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("-103.000000")


# --- Matching status (read-only) ---------------------------------------------


def test_get_invoice_matching_status_reflects_ordered_received_invoiced(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    status = ap_service.get_invoice_matching_status(db, po.id)
    assert len(status) == 1
    row = status[0]
    assert row.quantity_ordered == Decimal("10")
    assert row.quantity_received == Decimal("10.000")
    assert row.quantity_invoiced == Decimal("6.000")
    assert row.quantity_invoiceable == Decimal("4.000")


# --- Void ---------------------------------------------------------------------


def test_void_draft_invoice_is_a_pure_status_flip(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    voided = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    assert voided.status == "VOIDED"
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")


def test_void_posted_invoice_reverses_quantity_and_posts_mirror_journal(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")

    voided = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()
    assert voided.status == "VOIDED"
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("0.000")  # fully reversed

    void_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
            JournalEntry.source_id == invoice.id,
        )
    ).scalar_one()
    _assert_balanced(db, void_entry)
    # Exact mirror of the original: Cr Purchase Clearing / Dr AP.
    assert _line_amount(db, void_entry, ACCOUNT_PURCHASE_CLEARING) == Decimal("-50.000000")
    assert _line_amount(db, void_entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("50.000000")

    # And the PO item can now be invoiced again from scratch.
    invoice2 = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 6),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice2.id, caller_store_id=None)
    db.commit()
    db.refresh(item)
    assert item.quantity_invoiced == Decimal("10.000")


def test_voiding_an_already_voided_invoice_is_idempotent(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("5"), cost=Decimal("2.00")
    )
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
    ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    second = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    assert second.status == "VOIDED"
