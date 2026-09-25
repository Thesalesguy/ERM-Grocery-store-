"""M16 Phase 0 item 1 / Session C: supplier payment and supplier credit
note reversal — the AP payment/credit-note correction path.

See docs/M16_DESIGN.md "AP payment/credit-note correction path" for the
full design (mirrors app.modules.payroll.service.reverse_payroll_period's
structure exactly: idempotent-by-existence, no client_transaction_id,
never through reverse_journal_entry's generic mechanism).
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationAppError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import ACCOUNT_ACCOUNTS_PAYABLE, ACCOUNT_INVENTORY
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.ap import service as ap_service
from app.modules.ap.models import (
    PurchaseInvoice,
    SupplierCreditNote,
    SupplierCreditNoteReversal,
    SupplierPaymentReversal,
)
from app.modules.ap.service import (
    PaymentAllocationInput,
    PurchaseInvoiceLineInput,
    SupplierCreditNoteLineInput,
)
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import MANAGER
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrder, PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseReturnLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _order_and_receive(
    db: Session, store, supplier, product, *, qty: Decimal, cost: Decimal
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
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, qty, cost)],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    return po, item


def _posted_invoice(
    db: Session, store, supplier, item, *, qty: Decimal, cost: Decimal, po_id: int
) -> PurchaseInvoice:
    invoice = ap_service.create_purchase_invoice(
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
    db.commit()
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice)
    return invoice


def _lines_for(db: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )


def _assert_balanced(db: Session, entry: JournalEntry) -> None:
    lines = _lines_for(db, entry)
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)


def _account_id(db: Session, code: str) -> int:
    return db.execute(select(Account.id).where(Account.code == code)).scalar_one()


def _line_amount(db: Session, entry: JournalEntry, code: str) -> Decimal:
    account_id = _account_id(db, code)
    lines = [line for line in _lines_for(db, entry) if line.account_id == account_id]
    return sum((line.debit - line.credit for line in lines), Decimal("0"))


def _actor_id(db: Session, store) -> int:
    """A real, committed user for `reversed_by` -- journal_entries.created_by
    has a real FK to users, so a hardcoded literal id (coincidentally
    present in a developer's local database, absent on a clean CI
    database) is never safe here."""
    user = make_user_with_role(db, store, MANAGER, username=f"reverser_{unique_suffix()}")
    db.flush()
    return user.id


# --- Supplier payment reversal ----------------------------------------------


def test_valid_payment_reversal_restores_invoice_balance_and_status(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )

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
    db.refresh(invoice)
    assert invoice.status == "PAID"
    assert invoice.amount_paid == Decimal("50.00")

    reversed_payment = ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Recorded against the wrong invoice",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    db.refresh(invoice)

    assert reversed_payment.id == payment.id
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.status == "POSTED"


def test_duplicate_reversal_is_idempotent(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Wrong amount",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    reversal_count_after_first = (
        db.execute(
            select(SupplierPaymentReversal).where(
                SupplierPaymentReversal.supplier_payment_id == payment.id
            )
        )
        .scalars()
        .all()
    )
    assert len(reversal_count_after_first) == 1

    # A second, genuinely new reversal attempt against an already-reversed
    # payment is itself the idempotent case (mirrors
    # reverse_payroll_period exactly) -- no error, no second reversal row.
    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="A different reason string on the retry",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    reversal_count_after_second = (
        db.execute(
            select(SupplierPaymentReversal).where(
                SupplierPaymentReversal.supplier_payment_id == payment.id
            )
        )
        .scalars()
        .all()
    )
    assert len(reversal_count_after_second) == 1


def test_reason_is_required(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    with pytest.raises(ValidationAppError) as exc_info:
        ap_service.reverse_supplier_payment(
            db,
            supplier_payment_id=payment.id,
            reason="   ",
            caller_store_id=None,
            reversed_by=_actor_id(db, store),
        )
    assert exc_info.value.error_code == "REVERSAL_REASON_REQUIRED"


def test_cross_store_reversal_denied(db: Session) -> None:
    store = make_store(db)
    other_store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    with pytest.raises(ForbiddenError) as exc_info:
        ap_service.reverse_supplier_payment(
            db,
            supplier_payment_id=payment.id,
            reason="Attempted cross-store reversal",
            caller_store_id=other_store.id,
            reversed_by=_actor_id(db, store),
        )
    assert exc_info.value.error_code == "STORE_ACCESS_DENIED"


def test_reversal_without_permission_is_forbidden_at_http_layer(client, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    username = f"mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    inv_clerk_username = f"iclerk_{unique_suffix()}"
    make_user_with_role(db, store, "Inventory Clerk", username=inv_clerk_username)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    # Inventory Clerk holds ap.read/ap.write but not ap.reverse.
    headers = auth_headers(client, inv_clerk_username, DEFAULT_TEST_PASSWORD)
    resp = client.post(
        f"/api/v1/ap/payments/{payment.id}/reverse",
        headers=headers,
        json={"reason": "Should be forbidden"},
    )
    assert resp.status_code == 403

    # Manager holds ap.reverse and succeeds.
    mgr_headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    resp = client.post(
        f"/api/v1/ap/payments/{payment.id}/reverse",
        headers=mgr_headers,
        json={"reason": "Manager-authorized correction"},
    )
    assert resp.status_code == 200


def test_journal_balance_and_audit_trail(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Test reason",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()

    reversal_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_PAYMENT_REVERSAL",
            JournalEntry.source_id == payment.id,
        )
    ).scalar_one()
    _assert_balanced(db, reversal_entry)

    reversal_row = db.execute(
        select(SupplierPaymentReversal).where(
            SupplierPaymentReversal.supplier_payment_id == payment.id
        )
    ).scalar_one()
    assert reversal_row.reversal_journal_entry_id == reversal_entry.id
    assert reversal_row.reason == "Test reason"

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "SUPPLIER_PAYMENT_REVERSED", AuditLog.entity_id == payment.id
        )
    ).scalar_one()
    assert audit_row.after_state["reversal_journal_entry_id"] == reversal_entry.id


def test_reversal_journal_entry_cannot_itself_be_reversed_via_generic_mechanism(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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
    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Test reason",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()

    reversal_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_PAYMENT_REVERSAL",
            JournalEntry.source_id == payment.id,
        )
    ).scalar_one()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=reversal_entry.id,
            reason="Attempting to reverse the reversal",
            reversed_by=_actor_id(db, store),
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


def test_failure_during_gl_posting_leaves_invoice_balance_unchanged(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside reversal GL posting")

    monkeypatch.setattr(accounting_service, "post_supplier_payment_reversal_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        ap_service.reverse_supplier_payment(
            db,
            supplier_payment_id=payment.id,
            reason="Should roll back entirely",
            caller_store_id=None,
            reversed_by=_actor_id(db, store),
        )
    db.rollback()

    reloaded_invoice = db.get(PurchaseInvoice, invoice.id)
    assert reloaded_invoice.amount_paid == Decimal("50.00")
    assert reloaded_invoice.status == "PAID"
    assert (
        db.execute(
            select(SupplierPaymentReversal).where(
                SupplierPaymentReversal.supplier_payment_id == payment.id
            )
        ).scalar_one_or_none()
        is None
    )


@dataclass
class _Outcome:
    succeeded: bool = False
    error: str | None = None


def _attempt_reversal(
    *,
    supplier_payment_id: int,
    reason: str,
    barrier: threading.Barrier,
    result: _Outcome,
    reversed_by: int,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        ap_service.reverse_supplier_payment(
            session,
            supplier_payment_id=supplier_payment_id,
            reason=reason,
            caller_store_id=None,
            reversed_by=reversed_by,
        )
        session.commit()
        result.succeeded = True
    except Exception as exc:  # noqa: BLE001 - captured for the assertion below
        session.rollback()
        result.error = repr(exc)
    finally:
        session.close()


def test_two_concurrent_reversal_attempts_exactly_one_creates_a_reversal_row() -> None:
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        supplier = make_supplier(setup_session)
        product = make_product(setup_session, store)
        setup_session.commit()
        po, item = _order_and_receive(
            setup_session, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
        )
        invoice = _posted_invoice(
            setup_session,
            store,
            supplier,
            item,
            qty=Decimal("10"),
            cost=Decimal("5.00"),
            po_id=po.id,
        )
        payment = ap_service.record_supplier_payment(
            setup_session,
            store_id=store.id,
            supplier_id=supplier.id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=Decimal("50.00"),
            allocations=[PaymentAllocationInput(invoice.id, Decimal("50.00"))],
            client_transaction_id=f"ptxn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        actor_id = _actor_id(setup_session, store)
        setup_session.commit()
        payment_id = payment.id
        invoice_id = invoice.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_reversal,
        kwargs=dict(
            supplier_payment_id=payment_id,
            reason="Attempt A",
            barrier=barrier,
            result=result_a,
            reversed_by=actor_id,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_reversal,
        kwargs=dict(
            supplier_payment_id=payment_id,
            reason="Attempt B",
            barrier=barrier,
            result=result_b,
            reversed_by=actor_id,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    # Both attempts should complete without an unhandled error -- the
    # loser observes the winner's already-committed reversal row via the
    # pre-lock existence check (or, in the tightest race, the DB's own
    # UNIQUE constraint converts it into a handled ALREADY_REVERSED
    # ConflictError inside the service function itself, which
    # _attempt_reversal still records as a non-"succeeded" outcome).
    assert result_a.error is None or "ALREADY_REVERSED" in (result_a.error or "")
    assert result_b.error is None or "ALREADY_REVERSED" in (result_b.error or "")

    verify_session = SessionLocal()
    try:
        rows = (
            verify_session.execute(
                select(SupplierPaymentReversal).where(
                    SupplierPaymentReversal.supplier_payment_id == payment_id
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        invoice_row = verify_session.get(PurchaseInvoice, invoice_id)
        # Reversed exactly once, not twice -- amount_paid back to 0, not
        # negative.
        assert invoice_row.amount_paid == Decimal("0.00")
    finally:
        verify_session.close()


def test_supplier_ap_summary_and_aging_reflect_reversal(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
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

    summary_after_payment = ap_service.get_supplier_ap_summary(
        db, supplier.id, as_of=date(2024, 2, 1)
    )
    assert summary_after_payment.total_owed == Decimal("0.00")
    assert summary_after_payment.total_paid == Decimal("50.00")

    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Test reason",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()

    summary_after_reversal = ap_service.get_supplier_ap_summary(
        db, supplier.id, as_of=date(2024, 2, 1)
    )
    assert summary_after_reversal.total_owed == Decimal("50.00")

    aging = ap_service.ap_aging(db, store_id=store.id, as_of=date(2024, 2, 1))
    matching_rows = [row for row in aging if row.supplier_id == supplier.id]
    assert sum(row.total for row in matching_rows) == Decimal("50.00")

    statement = ap_service.get_supplier_statement(db, supplier.id)
    # The original PAYMENT event still appears on the statement (it did
    # happen, historically) -- the reversal itself doesn't post a new
    # invoice/payment/credit-note-typed statement line (it's a pure GL +
    # amount_paid correction), so the statement's own closing balance is
    # what actually reflects the reversal.
    assert any(line.transaction_type == "PAYMENT" for line in statement.lines)
    assert statement.closing_balance == Decimal("50.00")


# --- Supplier credit note reversal ------------------------------------------


def _posted_credit_note_commercial_discount(
    db: Session, store, supplier, invoice: PurchaseInvoice, *, amount: Decimal
) -> SupplierCreditNote:
    credit_note = ap_service.create_supplier_credit_note(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        credit_number=f"CN-{unique_suffix()}",
        credit_date=date(2024, 1, 12),
        reason="COMMERCIAL_DISCOUNT",
        lines=[SupplierCreditNoteLineInput(description="Volume discount", amount=amount)],
        allocations=[PaymentAllocationInput(invoice.id, amount)],
        client_transaction_id=f"cntxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    return credit_note


def test_valid_credit_note_reversal_restores_invoice_balance(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )

    credit_note = _posted_credit_note_commercial_discount(
        db, store, supplier, invoice, amount=Decimal("20.00")
    )
    db.refresh(invoice)
    assert invoice.amount_credited == Decimal("20.00")
    assert invoice.status == "PARTIALLY_PAID"

    ap_service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=credit_note.id,
        reason="Discount should not have applied",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    db.refresh(invoice)
    db.refresh(credit_note)

    assert invoice.amount_credited == Decimal("0.00")
    assert invoice.status == "POSTED"
    assert credit_note.amount_allocated == Decimal("0.00")


def test_credit_note_reversal_uses_correct_account_for_goods_return_reason(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )

    purchase_return = purchasing_service.create_purchase_return(
        db,
        purchase_order_id=po.id,
        store_id=store.id,
        return_date=date(2024, 1, 11),
        lines=[PurchaseReturnLineInput(product_id=product.id, quantity=Decimal("2"))],
        client_transaction_id=f"prtxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    credit_note = ap_service.create_supplier_credit_note(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        credit_number=f"CN-{unique_suffix()}",
        credit_date=date(2024, 1, 12),
        reason="GOODS_RETURN",
        purchase_return_id=purchase_return.id,
        lines=[SupplierCreditNoteLineInput(description="Returned goods", amount=Decimal("10.00"))],
        allocations=[PaymentAllocationInput(invoice.id, Decimal("10.00"))],
        client_transaction_id=f"cntxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    ap_service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=credit_note.id,
        reason="Wrong return referenced",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()

    reversal_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SUPPLIER_CREDIT_NOTE_REVERSAL",
            JournalEntry.source_id == credit_note.id,
        )
    ).scalar_one()
    _assert_balanced(db, reversal_entry)
    assert _line_amount(db, reversal_entry, ACCOUNT_INVENTORY) == Decimal("10.00")
    assert _line_amount(db, reversal_entry, ACCOUNT_ACCOUNTS_PAYABLE) == Decimal("-10.00")


def test_credit_note_reversal_is_idempotent(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )
    credit_note = _posted_credit_note_commercial_discount(
        db, store, supplier, invoice, amount=Decimal("15.00")
    )

    ap_service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=credit_note.id,
        reason="First",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    ap_service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=credit_note.id,
        reason="Second",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()

    rows = (
        db.execute(
            select(SupplierCreditNoteReversal).where(
                SupplierCreditNoteReversal.supplier_credit_note_id == credit_note.id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_payment_and_credit_note_reversal_on_the_same_invoice_are_independent(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5.00")
    )
    invoice = _posted_invoice(
        db, store, supplier, item, qty=Decimal("10"), cost=Decimal("5.00"), po_id=po.id
    )

    payment = ap_service.record_supplier_payment(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        payment_date=date(2024, 1, 10),
        payment_method="CASH",
        amount=Decimal("30.00"),
        allocations=[PaymentAllocationInput(invoice.id, Decimal("30.00"))],
        client_transaction_id=f"ptxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    credit_note = _posted_credit_note_commercial_discount(
        db, store, supplier, invoice, amount=Decimal("20.00")
    )
    db.refresh(invoice)
    assert invoice.status == "PAID"  # 30 + 20 == grand_total (10 * 5.00 = 50)

    ap_service.reverse_supplier_payment(
        db,
        supplier_payment_id=payment.id,
        reason="Payment correction",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    db.refresh(invoice)

    # Reversing the payment leaves the credit note's own application
    # completely untouched.
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.amount_credited == Decimal("20.00")
    assert invoice.status == "PARTIALLY_PAID"

    ap_service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=credit_note.id,
        reason="Credit note correction",
        caller_store_id=None,
        reversed_by=_actor_id(db, store),
    )
    db.commit()
    db.refresh(invoice)
    assert invoice.amount_credited == Decimal("0.00")
    assert invoice.status == "POSTED"
