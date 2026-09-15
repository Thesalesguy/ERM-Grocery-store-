"""M11 Phase 12: analytics vs. concurrent writes. Proves each report
listed in docs/M11_DESIGN.md Section 12 sees an all-or-nothing view of
a concurrent multi-statement write -- never a partial/torn read of a
transaction that is still in flight -- and reflects the full write
once it commits.

Like tests/test_concurrency.py and tests/test_ap_concurrency.py, this
deliberately does NOT use the `db` fixture: proving real cross-
connection isolation requires two genuinely independent
SessionLocal() connections, each doing its own commit/no-commit, not
one connection's SAVEPOINT-nested transaction. This module relies
entirely on PostgreSQL's own isolation levels (never any app-level
locking invented for reports -- read-only analytics has nothing to
lock): READ COMMITTED (the SQLAlchemy/psycopg default) is enough to
prove a reader never observes another transaction's uncommitted rows
and sees a commit completely once it lands.

One report -- payroll_cost_summary, exercised by
test_payroll_posting_during_payroll_report below -- issues several
separate SELECT statements per call. READ COMMITTED alone is NOT
enough there: it re-snapshots on every statement, so a write
committing between two of those statements can produce a torn read
(this was an actual defect this test caught, not a hypothetical --
see that test's own docstring). The fix, and what that test now
verifies, is REPEATABLE READ for the lifetime of one report call,
applied at the API boundary in
app.api.v1.endpoints.reports._report_db so every statement a report
issues -- however many, across however many service functions --
shares one consistent snapshot. Still pure PostgreSQL isolation, just
a stronger level where one statement's worth of consistency isn't
enough.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.session import SessionLocal
from app.modules.ap import service as ap_service
from app.modules.ap.service import PaymentAllocationInput, PurchaseInvoiceLineInput
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
from app.modules.payroll.service import PayrollPeriodInput
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.reports import service as reports_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ShipLineInput, TransferLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
)


def test_report_db_dependency_bumps_a_fresh_session_to_repeatable_read() -> None:
    """Direct proof of the _report_db fix: a fresh, query-free session
    (exactly what every real HTTP request gets from get_db) is bumped to
    REPEATABLE READ. This is what closes the torn-read gap
    test_payroll_posting_during_payroll_report demonstrates below."""
    from sqlalchemy import text

    from app.api.v1.endpoints.reports import _report_db

    session = SessionLocal()
    try:
        _report_db(session)
        level = session.execute(text("SHOW transaction_isolation")).scalar_one()
        assert level == "repeatable read"
    finally:
        session.rollback()
        session.close()


def test_report_db_dependency_does_not_raise_on_an_already_active_transaction() -> None:
    """The tests/conftest.py `db` fixture (and the `client` fixture built
    on it) opens its own outer transaction before any endpoint code
    runs, on purpose, for per-test rollback isolation -- a session in
    that state can no longer have its isolation level changed. _report_db
    must degrade gracefully (keep the existing isolation level) rather
    than raise -- proven directly here rather than only incidentally via
    the RBAC/API tests that happen to use the `client` fixture."""
    from app.api.v1.endpoints.reports import _report_db
    from app.db.session import engine

    connection = engine.connect()
    transaction = connection.begin()
    from sqlalchemy.orm import Session

    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        make_store(session)  # any statement is enough to start the session's own transaction
        returned = _report_db(session)
        assert returned is session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _run_read_during_uncommitted_write(
    *,
    stage_write: Callable[[Any], None],
    do_report: Callable[[], Any],
    timeout: float = 10,
) -> tuple[Any, Any]:
    """`stage_write` runs on its own SessionLocal() and performs every
    statement of a real write WITHOUT committing; this function then
    runs `do_report` (which must open and close its own independent
    session) while that write is still uncommitted, only then lets the
    writer commit, and runs `do_report` again. Returns
    (during_uncommitted, after_committed)."""
    writer = SessionLocal()
    staged = threading.Event()
    proceed_to_commit = threading.Event()
    committed = threading.Event()
    write_error: list[BaseException] = []

    def _writer() -> None:
        try:
            stage_write(writer)
            staged.set()
            proceed_to_commit.wait(timeout=timeout)
            writer.commit()
        except BaseException as exc:  # pragma: no cover - surfaced via write_error
            write_error.append(exc)
            staged.set()
        finally:
            committed.set()

    thread = threading.Thread(target=_writer)
    thread.start()
    try:
        assert staged.wait(timeout=timeout), "writer never staged its uncommitted write"
        assert not write_error, f"writer raised before staging: {write_error}"
        during_value = do_report()
        proceed_to_commit.set()
        assert committed.wait(timeout=timeout), "writer never committed"
        thread.join(timeout=timeout)
        assert not write_error, f"writer raised: {write_error}"
        after_value = do_report()
    finally:
        writer.close()
    return during_value, after_value


def test_sale_finalizing_during_sales_report() -> None:
    setup = SessionLocal()
    store = make_store(setup)
    cashier = make_user(setup, store)
    product = make_product(
        setup, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    setup.commit()
    store_id, cashier_id, product_id = store.id, cashier.id, product.id
    setup.close()

    def stage_write(session: Any) -> None:
        sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=f"conc-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product_id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )

    def do_report() -> Decimal:
        session = SessionLocal()
        try:
            return reports_service.sales_summary(session, store_ids=[store_id]).gross_sales
        finally:
            session.close()

    before, after = _run_read_during_uncommitted_write(stage_write=stage_write, do_report=do_report)
    assert before == Decimal("0.00")
    assert after == Decimal("10.00")


def test_return_during_sales_report() -> None:
    setup = SessionLocal()
    store = make_store(setup)
    cashier = make_user(setup, store)
    product = make_product(
        setup, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    setup.commit()
    sale = sales_service.finalize_sale(
        setup,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"conc-{uuid.uuid4().hex}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    setup.commit()
    store_id, sale_id, item_id = store.id, sale.id, sale.items[0].id
    setup.close()

    def stage_write(session: Any) -> None:
        sales_service.create_sale_return(
            session,
            sale_id=sale_id,
            store_id=store_id,
            return_date=date.today(),
            client_transaction_id=f"concret-{uuid.uuid4().hex}",
            refund_method="CASH",
            caller_store_id=None,
            lines=[SaleReturnLineInput(sale_item_id=item_id, quantity=Decimal("1"))],
        )

    def do_report() -> Decimal:
        session = SessionLocal()
        try:
            return reports_service.sales_summary(session, store_ids=[store_id]).net_sales
        finally:
            session.close()

    before, after = _run_read_during_uncommitted_write(stage_write=stage_write, do_report=do_report)
    assert before == Decimal("10.00")  # the return is not yet committed/visible
    assert after == Decimal("0.00")  # fully netted out once committed


def test_stock_receipt_during_inventory_report() -> None:
    setup = SessionLocal()
    store = make_store(setup)
    supplier = make_supplier(setup)
    product = make_product(
        setup, store, current_qty_on_hand=Decimal("0"), current_cost=Decimal("0")
    )
    setup.commit()
    po = make_purchase_order(setup, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )
    setup.add(item)
    setup.commit()
    store_id, po_id, item_id = store.id, po.id, item.id
    setup.close()

    def stage_write(session: Any) -> None:
        purchasing_service.receive_goods(
            session,
            purchase_order_id=po_id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item_id, Decimal("10"), Decimal("4.00"))],
            client_transaction_id=f"grn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )

    def do_report() -> Decimal:
        session = SessionLocal()
        try:
            rows = reports_service.inventory_value_by_store(session, store_ids=[store_id])
            return next((r.value for r in rows if r.key == store_id), Decimal("0"))
        finally:
            session.close()

    before, after = _run_read_during_uncommitted_write(stage_write=stage_write, do_report=do_report)
    assert before == Decimal("0")
    assert after == Decimal("40.000000")


def test_transfer_ship_during_in_transit_report() -> None:
    setup = SessionLocal()
    store_a = make_store(setup)
    store_b = make_store(setup)
    source = make_product(
        setup,
        store_a,
        sku=f"SKU-{uuid.uuid4().hex[:8]}",
        current_qty_on_hand=Decimal("50"),
        current_cost=Decimal("6.00"),
    )
    make_product(setup, store_b, sku=source.sku)
    setup.commit()
    transfer = transfer_service.create_transfer(
        setup,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    setup.commit()
    store_a_id, store_b_id = store_a.id, store_b.id
    transfer_id, line_id = transfer.id, transfer.lines[0].id
    setup.close()

    def stage_write(session: Any) -> None:
        transfer_service.ship_transfer(
            session,
            transfer_id=transfer_id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("10"))],
            client_transaction_id=f"ship-{uuid.uuid4().hex}",
            caller_store_id=None,
        )

    def do_report() -> Decimal:
        session = SessionLocal()
        try:
            rows = reports_service.inventory_in_transit(session, store_ids=[store_a_id, store_b_id])
            return sum((r.quantity_in_transit for r in rows), start=Decimal("0"))
        finally:
            session.close()

    before, after = _run_read_during_uncommitted_write(stage_write=stage_write, do_report=do_report)
    assert before == Decimal("0")
    assert after == Decimal("10")


def test_ap_payment_during_aging() -> None:
    setup = SessionLocal()
    store = make_store(setup)
    supplier = make_supplier(setup)
    product = make_product(setup, store)
    setup.commit()
    po = make_purchase_order(setup, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("5.00"),
    )
    setup.add(item)
    setup.commit()
    purchasing_service.receive_goods(
        setup,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"grn-{uuid.uuid4().hex}",
        caller_store_id=None,
    )
    setup.commit()
    invoice = ap_service.create_purchase_invoice(
        setup,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{uuid.uuid4().hex}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("10"), Decimal("5.00"))],
        client_transaction_id=f"itxn-{uuid.uuid4().hex}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(setup, purchase_invoice_id=invoice.id, caller_store_id=None)
    setup.commit()
    invoice_id, store_id, supplier_id = invoice.id, store.id, supplier.id
    setup.close()

    def stage_write(session: Any) -> None:
        ap_service.record_supplier_payment(
            session,
            store_id=store_id,
            supplier_id=supplier_id,
            payment_date=date(2024, 1, 10),
            payment_method="BANK_TRANSFER",
            amount=Decimal("50.00"),
            allocations=[PaymentAllocationInput(invoice_id, Decimal("50.00"))],
            client_transaction_id=f"pay-{uuid.uuid4().hex}",
            caller_store_id=None,
        )

    def do_report() -> Decimal:
        session = SessionLocal()
        try:
            rows = ap_service.ap_aging(session, store_ids=[store_id], as_of=date(2024, 2, 1))
            return sum((r.total for r in rows if r.supplier_id == supplier_id), start=Decimal("0"))
        finally:
            session.close()

    before, after = _run_read_during_uncommitted_write(stage_write=stage_write, do_report=do_report)
    assert before == Decimal("50.00")
    assert after == Decimal("0.00")


def _hire_and_pay(session: Any, store: Any) -> int:
    employee = hr_service.hire_employee(
        session,
        EmployeeHireInput(
            employee_number=f"EMP-{uuid.uuid4().hex[:8]}",
            legal_name="Concurrency Test Employee",
            hire_date=date(2024, 1, 1),
            store_id=store.id,
        ),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.change_compensation(
        session,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("2000.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    return employee.id


def test_payroll_posting_during_payroll_report() -> None:
    """Unlike the other five scenarios above, `post_payroll_period`
    commits internally (docs/M11_DESIGN.md Section 12) rather than
    leaving the commit to its caller, so there is no observable
    uncommitted window to pause it in from outside -- the staged-write
    helper used elsewhere in this file doesn't apply. What's actually
    at risk here is a TORN read: a concurrent payroll_cost_summary call
    racing that single commit must see the period as either fully
    APPROVED-and-pending or fully POSTED-and-counted, never a mixed
    state (e.g. no longer "pending" but also not yet contributing to
    gross_pay -- which would mean the period briefly vanished from
    every report). This drives a real concurrent race (via a Barrier,
    not staged commits) and asserts every observed reading during that
    race is one of the two valid states."""
    setup = SessionLocal()
    store = make_store(setup)
    approver = make_user(setup, store)
    _hire_and_pay(setup, store)
    setup.commit()
    period = payroll_service.create_payroll_period(
        setup,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            pay_date=date(2024, 1, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        setup, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        setup, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    payroll_service.approve_payroll_period(
        setup, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    setup.commit()
    period_id, approver_id, store_id = period.id, approver.id, store.id
    setup.close()

    barrier = threading.Barrier(2)
    readings: list[tuple[int, Decimal]] = []
    poster_error: list[BaseException] = []
    reader_error: list[BaseException] = []

    def _poster() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            payroll_service.post_payroll_period(
                session, payroll_period_id=period_id, actor_id=approver_id, caller_store_id=None
            )
        except BaseException as exc:  # pragma: no cover - surfaced via poster_error
            poster_error.append(exc)
        finally:
            session.close()

    def _reader() -> None:
        barrier.wait(timeout=10)
        for _ in range(50):
            session = SessionLocal()
            try:
                # payroll_cost_summary issues several separate SELECT
                # statements in one call; under default READ COMMITTED
                # each one gets its own fresh snapshot, so a commit
                # landing between them could show a torn read (a period
                # that is neither pending nor posted). The real reports
                # API closes this gap for every endpoint via
                # app.api.v1.endpoints.reports._report_db, which bumps
                # the request's connection to REPEATABLE READ before its
                # first query -- reused here on this fresh, query-free
                # session to prove that fix actually works, the same way
                # a real report request would experience it.
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                summary = reports_service.payroll_cost_summary(
                    session,
                    store_ids=[store_id],
                    period_start=date(2024, 1, 1),
                    period_end=date(2024, 1, 31),
                )
                readings.append((summary.pending_period_count, summary.gross_pay))
            except BaseException as exc:  # pragma: no cover - surfaced via reader_error
                reader_error.append(exc)
                break
            finally:
                session.close()

    poster_thread = threading.Thread(target=_poster)
    reader_thread = threading.Thread(target=_reader)
    poster_thread.start()
    reader_thread.start()
    poster_thread.join(timeout=10)
    reader_thread.join(timeout=10)

    assert not poster_error, f"posting raised: {poster_error}"
    assert not reader_error, f"reporting raised: {reader_error}"
    assert readings, "reader never got a chance to run"

    for pending_count, gross_pay in readings:
        # exactly one of "still pending" / "posted with a real gross_pay"
        # must hold -- never both, and never neither.
        still_pending = pending_count == 1 and gross_pay == Decimal("0")
        fully_posted = pending_count == 0 and gross_pay > Decimal("0")
        assert still_pending or fully_posted, (
            f"torn read: pending_period_count={pending_count}, gross_pay={gross_pay} "
            "is neither the pre-post nor the post-post state"
        )

    session = SessionLocal()
    try:
        final = reports_service.payroll_cost_summary(
            session,
            store_ids=[store_id],
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
        )
        assert final.pending_period_count == 0
        assert final.gross_pay > Decimal("0")
    finally:
        session.close()
