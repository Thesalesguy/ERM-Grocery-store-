"""M6 Session H: AP concurrency, proven against real PostgreSQL with
genuinely independent connections (same discipline as
tests/test_sales_returns_concurrency.py — see that file's module
docstring for why the `db` fixture's savepoint isolation can't be used
here).

Three scenarios, each repeated 5x since a real race is timing-sensitive:

A: two concurrent payments together exceeding an invoice's balance —
   exactly one succeeds, the other is rejected with OVERPAYMENT, and the
   invoice's amount_paid never exceeds grand_total.
B: two concurrent invoice postings together exceeding a PO item's
   received quantity — exactly one succeeds, the other is rejected with
   OVER_INVOICING, and quantity_invoiced never exceeds quantity_received.
C: two concurrent payment requests with the SAME client_transaction_id —
   exactly one SupplierPayment row is created; both callers observe the
   identical id.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.ap import service as ap_service
from app.modules.ap.models import PurchaseInvoice, SupplierPayment
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import make_product, make_purchase_order, make_store, make_supplier


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    result_id: int | None = None


def _setup_posted_invoice(*, qty: Decimal, cost: Decimal) -> tuple[int, int, int]:
    """Returns (invoice_id, store_id, po_item_id)."""
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
        return invoice.id, store.id, item.id
    finally:
        session.close()


def _attempt_payment(
    *,
    invoice_id: int,
    store_id: int,
    amount: Decimal,
    barrier: threading.Barrier,
    result: _Outcome,
    client_transaction_id: str | None = None,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        payment = ap_service.record_supplier_payment(
            session,
            purchase_invoice_id=invoice_id,
            store_id=store_id,
            payment_date=date(2024, 1, 10),
            payment_method="CASH",
            amount=amount,
            client_transaction_id=client_transaction_id or f"ptxn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.result_id = payment.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock/error to surface, not vanish
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_a_two_concurrent_payments_exceeding_balance_exactly_one_wins() -> None:
    for _ in range(5):
        invoice_id, store_id, _ = _setup_posted_invoice(qty=Decimal("10"), cost=Decimal("10.00"))
        # grand_total = 100.00; two payments of 60 each together exceed it.

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_payment,
            kwargs=dict(
                invoice_id=invoice_id,
                store_id=store_id,
                amount=Decimal("60.00"),
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_payment,
            kwargs=dict(
                invoice_id=invoice_id,
                store_id=store_id,
                amount=Decimal("60.00"),
                barrier=barrier,
                result=result_b,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        outcomes = [result_a, result_b]
        winners = [r for r in outcomes if r.succeeded]
        losers = [r for r in outcomes if not r.succeeded]
        assert len(winners) == 1, outcomes
        assert len(losers) == 1, outcomes
        assert losers[0].error_code == "OVERPAYMENT", losers[0]

        verify_session = SessionLocal()
        try:
            invoice = verify_session.get(PurchaseInvoice, invoice_id)
            assert invoice.amount_paid == Decimal("60.00")  # never 120, never 0
            assert invoice.amount_paid <= invoice.grand_total
        finally:
            verify_session.close()


def _attempt_post_invoice(*, invoice_id: int, barrier: threading.Barrier, result: _Outcome) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        invoice = ap_service.post_purchase_invoice(
            session, purchase_invoice_id=invoice_id, caller_store_id=None
        )
        session.commit()
        result.succeeded = True
        result.result_id = invoice.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.succeeded = False
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_b_two_concurrent_invoice_postings_exceeding_received_qty_exactly_one_wins() -> None:
    for _ in range(5):
        session = SessionLocal()
        try:
            store = make_store(session)
            supplier = make_supplier(session)
            product = make_product(session, store)
            session.commit()
            po = make_purchase_order(session, store, supplier)
            item = PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=product.id,
                quantity_ordered=Decimal("10"),
                unit_cost=Decimal("10.00"),
            )
            session.add(item)
            session.commit()
            purchasing_service.receive_goods(
                session,
                purchase_order_id=po.id,
                received_date=date(2024, 1, 1),
                lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
                client_transaction_id=f"txn-{uuid.uuid4().hex}",
                caller_store_id=None,
            )
            session.commit()
            # Two separate DRAFT invoices, each invoicing 6 of the 10
            # received units — together they exceed what was received.
            invoice_a = ap_service.create_purchase_invoice(
                session,
                store_id=store.id,
                supplier_id=supplier.id,
                purchase_order_id=po.id,
                invoice_number=f"INV-A-{uuid.uuid4().hex}",
                invoice_date=date(2024, 1, 5),
                lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("10.00"))],
                client_transaction_id=f"itxn-{uuid.uuid4().hex}",
                caller_store_id=None,
            )
            invoice_b = ap_service.create_purchase_invoice(
                session,
                store_id=store.id,
                supplier_id=supplier.id,
                purchase_order_id=po.id,
                invoice_number=f"INV-B-{uuid.uuid4().hex}",
                invoice_date=date(2024, 1, 5),
                lines=[PurchaseInvoiceLineInput(item.id, Decimal("6"), Decimal("10.00"))],
                client_transaction_id=f"itxn-{uuid.uuid4().hex}",
                caller_store_id=None,
            )
            invoice_a_id, invoice_b_id, item_id = invoice_a.id, invoice_b.id, item.id
        finally:
            session.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_post_invoice,
            kwargs=dict(invoice_id=invoice_a_id, barrier=barrier, result=result_a),
        )
        thread_b = threading.Thread(
            target=_attempt_post_invoice,
            kwargs=dict(invoice_id=invoice_b_id, barrier=barrier, result=result_b),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        outcomes = [result_a, result_b]
        winners = [r for r in outcomes if r.succeeded]
        losers = [r for r in outcomes if not r.succeeded]
        assert len(winners) == 1, outcomes
        assert len(losers) == 1, outcomes
        assert losers[0].error_code == "OVER_INVOICING", losers[0]

        verify_session = SessionLocal()
        try:
            item = verify_session.get(PurchaseOrderItem, item_id)
            assert item.quantity_invoiced == Decimal("6.000")  # never 12, never 0
            assert item.quantity_invoiced <= item.quantity_received
        finally:
            verify_session.close()


def test_c_two_concurrent_payments_with_same_idempotency_key_create_exactly_one() -> None:
    for _ in range(5):
        invoice_id, store_id, _ = _setup_posted_invoice(qty=Decimal("5"), cost=Decimal("10.00"))
        shared_key = f"ptxn-{uuid.uuid4().hex}"

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_payment,
            kwargs=dict(
                invoice_id=invoice_id,
                store_id=store_id,
                amount=Decimal("50.00"),
                barrier=barrier,
                result=result_a,
                client_transaction_id=shared_key,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_payment,
            kwargs=dict(
                invoice_id=invoice_id,
                store_id=store_id,
                amount=Decimal("50.00"),
                barrier=barrier,
                result=result_b,
                client_transaction_id=shared_key,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        assert result_a.succeeded and result_b.succeeded, (result_a, result_b)
        assert result_a.result_id == result_b.result_id

        verify_session = SessionLocal()
        try:
            count = (
                verify_session.query(SupplierPayment)
                .filter_by(client_transaction_id=shared_key)
                .count()
            )
            assert count == 1
            invoice = verify_session.get(PurchaseInvoice, invoice_id)
            assert invoice.amount_paid == Decimal("50.00")  # not double-applied to 100
        finally:
            verify_session.close()
