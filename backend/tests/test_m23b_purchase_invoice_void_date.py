"""M23B: `PURCHASE_INVOICE_VOID` posts on the current date, not the
original invoice's date (docs/M23A_POLICY.md Section 3.5, resolving the
gap docs/M22_DISCOVERY.md and docs/M23_DISCOVERY.md both identified).

Sessions A-I per the M23B brief. Every existing purchase-invoice-void test
(test_ap_invoices.py, test_ap_failure_injection.py, test_ap_payments.py,
test_ap_e2e_scenario.py, test_ap_mutation.py) continues to pass unmodified
-- none of them asserts a posting_date, so none is affected by this change.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import ACCOUNT_ACCOUNTS_PAYABLE, ACCOUNT_PURCHASE_CLEARING
from app.modules.accounting.models import JournalEntry
from app.modules.ap import service as ap_service
from app.modules.ap.models import PurchaseInvoice
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.audit.models import AuditLog
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
    unique_suffix,
)
from tests.test_ap_invoices import _assert_balanced, _line_amount, _order_and_receive


def _posted_invoice(
    db: Session, store, supplier, item, *, qty: Decimal, cost: Decimal, invoice_date: date
) -> PurchaseInvoice:
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=item.purchase_order_id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=invoice_date,
        lines=[PurchaseInvoiceLineInput(item.id, qty, cost)],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    return invoice


def _void_journal(db: Session, invoice_id: int) -> JournalEntry:
    return db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
            JournalEntry.source_id == invoice_id,
        )
    ).scalar_one()


# --- Session A: current-date posting -----------------------------------------


def test_session_a_void_posts_todays_date_not_the_original_invoice_date(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    old_date = date.today() - timedelta(days=60)
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), invoice_date=old_date
    )

    ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    entry = _void_journal(db, invoice.id)
    assert entry.posting_date == date.today()
    assert entry.posting_date != old_date
    _assert_balanced(db, entry)
    assert _line_amount(db, entry, ACCOUNT_PURCHASE_CLEARING) == Decimal("-50.000000")
    assert _line_amount(db, entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("50.000000")


# --- Session B: closed original period ---------------------------------------


def test_session_b_void_succeeds_after_the_original_period_is_closed(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    admin = make_user(db)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("4"), cost=Decimal("10.00")
    )
    old_date = date(2024, 1, 5)
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("4"), cost=Decimal("10.00"), invoice_date=old_date
    )

    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        reason="January 2024 close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    voided = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()

    assert voided.status == "VOIDED"
    entry = _void_journal(db, invoice.id)
    assert entry.posting_date == date.today()
    assert entry.posting_date != old_date


# --- Session C: closed current period -----------------------------------------


def test_session_c_void_refused_when_todays_own_period_is_closed(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    admin = make_user(db)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("3"), cost=Decimal("7.00")
    )
    invoice = _posted_invoice(
        db,
        store,
        supplier,
        item,
        qty=Decimal("3"),
        cost=Decimal("7.00"),
        invoice_date=date(2024, 1, 5),
    )
    db.refresh(item)
    quantity_invoiced_before = item.quantity_invoiced

    today = date.today()
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=today,
        period_end=today,
        reason="closing today itself",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    assert exc_info.value.error_code == "PERIOD_CLOSED"
    db.rollback()

    # No partial mutation: neither the document nor the PO item quantity moved.
    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.status == "POSTED"
    db.refresh(item)
    assert item.quantity_invoiced == quantity_invoiced_before
    assert (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
                JournalEntry.source_id == invoice.id,
            )
        ).scalar_one_or_none()
        is None
    )


# --- Session D: idempotency ----------------------------------------------------


def test_session_d_voiding_twice_creates_exactly_one_compensating_journal(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("5"), cost=Decimal("2.00")
    )
    invoice = _posted_invoice(
        db,
        store,
        supplier,
        item,
        qty=Decimal("5"),
        cost=Decimal("2.00"),
        invoice_date=date(2024, 1, 5),
    )

    first = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()
    second = ap_service.void_purchase_invoice(
        db, purchase_invoice_id=invoice.id, caller_store_id=None
    )
    db.commit()

    assert first.status == "VOIDED"
    assert second.status == "VOIDED"
    entries = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
                JournalEntry.source_id == invoice.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1


# --- Session E: concurrency ----------------------------------------------------


@dataclass
class _Outcome:
    status: str | None = None
    unexpected_error: str | None = None


def _setup_posted_invoice_for_concurrency(*, qty: Decimal, cost: Decimal) -> int:
    session = SessionLocal()
    try:
        store = make_store(session)
        supplier = make_supplier(session)
        product = make_product(session, store)
        session.commit()
        po = make_purchase_order(session, store, supplier)
        item = PurchaseOrderItem(
            purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
        )
        session.add(item)
        session.commit()
        purchasing_service.receive_goods(
            session,
            purchase_order_id=po.id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item.id, qty, cost)],
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        session.commit()
        invoice = ap_service.create_purchase_invoice(
            session,
            store_id=store.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-{uuid.uuid4().hex}",
            invoice_date=date(2024, 1, 5),
            lines=[PurchaseInvoiceLineInput(item.id, qty, cost)],
            client_transaction_id=f"itxn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        ap_service.post_purchase_invoice(
            session, purchase_invoice_id=invoice.id, caller_store_id=None
        )
        session.commit()
        return invoice.id
    finally:
        session.close()


def _attempt_void(*, invoice_id: int, barrier: threading.Barrier, result: _Outcome) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        voided = ap_service.void_purchase_invoice(
            session, purchase_invoice_id=invoice_id, caller_store_id=None
        )
        session.commit()
        result.status = voided.status
    except Exception as exc:  # noqa: BLE001 - want a real error to surface, not vanish
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_session_e_concurrent_voids_create_exactly_one_compensating_journal() -> None:
    for _ in range(5):
        invoice_id = _setup_posted_invoice_for_concurrency(qty=Decimal("6"), cost=Decimal("4.00"))

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_void,
            kwargs=dict(invoice_id=invoice_id, barrier=barrier, result=result_a),
        )
        thread_b = threading.Thread(
            target=_attempt_void,
            kwargs=dict(invoice_id=invoice_id, barrier=barrier, result=result_b),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        assert result_a.status == "VOIDED"
        assert result_b.status == "VOIDED"

        verify_session = SessionLocal()
        try:
            entries = (
                verify_session.execute(
                    select(JournalEntry).where(
                        JournalEntry.source_type == "PURCHASE_INVOICE_VOID",
                        JournalEntry.source_id == invoice_id,
                    )
                )
                .scalars()
                .all()
            )
            assert len(entries) == 1, entries
        finally:
            verify_session.close()


# --- Session F: store isolation ------------------------------------------------


def test_session_f_cannot_void_another_stores_invoice(db: Session) -> None:
    store = make_store(db)
    other_store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("2"), cost=Decimal("9.00")
    )
    invoice = _posted_invoice(
        db,
        store,
        supplier,
        item,
        qty=Decimal("2"),
        cost=Decimal("9.00"),
        invoice_date=date(2024, 1, 5),
    )

    with pytest.raises(ForbiddenError) as exc_info:
        ap_service.void_purchase_invoice(
            db, purchase_invoice_id=invoice.id, caller_store_id=other_store.id
        )
    assert exc_info.value.error_code == "STORE_ACCESS_DENIED"

    reloaded = db.get(PurchaseInvoice, invoice.id)
    assert reloaded.status == "POSTED"


# --- Session G: accounting invariant -------------------------------------------


def test_session_g_void_journal_balances_and_ap_summary_reconciles(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("8"), cost=Decimal("3.00")
    )
    invoice = _posted_invoice(
        db,
        store,
        supplier,
        item,
        qty=Decimal("8"),
        cost=Decimal("3.00"),
        invoice_date=date(2024, 1, 5),
    )
    summary_before = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=store.id)
    assert summary_before.total_owed == Decimal("24.00")

    ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()

    entry = _void_journal(db, invoice.id)
    _assert_balanced(db, entry)

    summary_after = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=store.id)
    assert summary_after.total_owed == Decimal("0")


# --- Session H: auditability ---------------------------------------------------


def test_session_h_void_is_audited_and_dated_at_the_actual_posting_date(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    admin = make_user(db)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("1"), cost=Decimal("6.00")
    )
    invoice = _posted_invoice(
        db,
        store,
        supplier,
        item,
        qty=Decimal("1"),
        cost=Decimal("6.00"),
        invoice_date=date(2024, 1, 5),
    )

    ap_service.void_purchase_invoice(
        db,
        purchase_invoice_id=invoice.id,
        caller_store_id=None,
        reason="wrong invoice",
        voided_by=admin.id,
    )
    db.commit()

    log = db.execute(
        select(AuditLog).where(
            AuditLog.action == "PURCHASE_INVOICE_VOIDED", AuditLog.entity_id == invoice.id
        )
    ).scalar_one()
    assert log.user_id == admin.id
    assert log.after_state["reason"] == "wrong invoice"

    entry = _void_journal(db, invoice.id)
    assert entry.posting_date == date.today()
    assert entry.source_id == invoice.id


# --- Session I: mutation testing ------------------------------------------------
# A second mutation (bypassing `_enforce_period_open` entirely) was also
# verified live against Session C -- edit the source, confirm Session C
# fails RED for the correct reason, revert, diff-verify byte-identical --
# per this codebase's established mutation-testing methodology (see the
# M23B final report for the transcript). It is not encoded as a permanent
# test here because a test that asserts the *bypassed* (wrong) outcome as
# its expected result would misdescribe correct behavior to a future
# reader; Session C itself, run against real source, is the permanent
# guard against that mutation.


def test_session_i_mutation_reverting_to_the_original_invoice_date_is_caught(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the prohibited mutation
    (`posting_date = purchase_invoice.date`) by monkeypatching
    `_post_journal` to force the OLD, wrong posting_date, and proves
    Session B's own assertion would fail against it."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    admin = make_user(db)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("4"), cost=Decimal("10.00")
    )
    old_date = date(2024, 1, 5)
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("4"), cost=Decimal("10.00"), invoice_date=old_date
    )
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        reason="January 2024 close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    real_post_journal = accounting_service._post_journal

    def _mutated_post_journal(db_, **kwargs):
        if kwargs.get("source_type") == "PURCHASE_INVOICE_VOID":
            kwargs["posting_date"] = old_date  # the prohibited, reverted behavior
        return real_post_journal(db_, **kwargs)

    monkeypatch.setattr(accounting_service, "_post_journal", _mutated_post_journal)

    with pytest.raises(ConflictError) as exc_info:
        ap_service.void_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    assert exc_info.value.error_code == "PERIOD_CLOSED"
