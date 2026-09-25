"""M20 Session E: sequential and concurrent duplicate fiscal-submission
attempts must never create/act on more than one row per sale
(docs/M20_DESIGN.md Section 3).

The two genuine-concurrency tests deliberately do NOT use the `db`
fixture (tests/conftest.py) -- exactly per test_concurrency.py's own
module docstring: that fixture wraps a test in one outer transaction
with a SAVEPOINT-based inner commit, so a second, independent connection
would never see its setup data at all. Proving real concurrency requires
two genuinely independent sessions, each committing for real -- so this
file talks to `app.db.session.SessionLocal` directly for those two tests
and leaves its small, uniquely-named committed rows in place.
"""

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig, FiscalSubmission
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user
from tests.fiscal_fakes import FakeFiscalProvider


def test_sequential_duplicate_submit_calls_are_a_no_op_after_first_success(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    fiscal_service.upsert_config(
        db,
        store_id=store.id,
        is_enabled=True,
        provider_name="FAKE",
        credential_reference=None,
        submission_endpoint=None,
        retry_max_attempts=5,
        updated_by=1,
    )
    db.commit()

    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{uuid.uuid4().hex}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
    )
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)

    for _ in range(3):
        fiscal_service.submit_fiscal_transaction(db, submission.id)
        db.commit()

    assert len(fake.calls) == 1
    assert submission.status == "ACKNOWLEDGED"
    assert submission.attempt_count == 1


@dataclass
class _CreateAttemptResult:
    submission_id: int | None = None
    conflict: bool = False


def _attempt_create_submission(
    *, sale_id: int, store_id: int, barrier: threading.Barrier, result: _CreateAttemptResult
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        submission = FiscalSubmission(
            sale_id=sale_id,
            store_id=store_id,
            status="PENDING",
            provider_name="FAKE",
            max_attempts=5,
            request_payload={"idempotency_key": f"sale-{sale_id}"},
        )
        session.add(submission)
        session.commit()
        result.submission_id = submission.id
    except IntegrityError:
        session.rollback()
        result.conflict = True
    finally:
        session.close()


def test_concurrent_duplicate_submission_creation_is_rejected_by_the_database() -> None:
    """Two threads racing to create a FiscalSubmission for the SAME sale
    -- only one can ever win; the UNIQUE constraint on sale_id is the
    real enforcement (docs/M20_DESIGN.md Section 1.2), not just an
    application-level check."""
    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
        )
        setup_session.commit()
        sale = sales_service.finalize_sale(
            setup_session,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
        )
        setup_session.commit()
        sale_id, store_id = sale.id, store.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    results = [_CreateAttemptResult(), _CreateAttemptResult()]
    threads = [
        threading.Thread(
            target=_attempt_create_submission,
            kwargs=dict(sale_id=sale_id, store_id=store_id, barrier=barrier, result=results[i]),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    winners = [r for r in results if r.submission_id is not None]
    losers = [r for r in results if r.conflict]
    assert len(winners) == 1
    assert len(losers) == 1

    verify_session = SessionLocal()
    try:
        rows = list(
            verify_session.execute(
                select(FiscalSubmission).where(FiscalSubmission.sale_id == sale_id)
            ).scalars()
        )
        assert len(rows) == 1
    finally:
        verify_session.close()


def _attempt_submit(
    *, submission_id: int, barrier: threading.Barrier, results: list, index: int
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        result = fiscal_service.submit_fiscal_transaction(session, submission_id)
        session.commit()
        results[index] = (result.status, result.fiscal_reference)
    finally:
        session.close()


def test_concurrent_submit_attempts_on_the_same_submission_resolve_consistently() -> None:
    """Two threads both calling submit_fiscal_transaction for the SAME
    already-PENDING submission (e.g. the background task and a manual
    retry racing) must never leave it ambiguous -- the row lock in
    submit_fiscal_transaction serializes them, and the fake provider's
    own idempotency-key deduplication means even if both somehow reached
    the provider, they'd get the same reference back.

    Uses a deliberately delayed provider (not an instant one) to widen
    the window between the row's SELECT and its UPDATE wide enough for
    two real threads to actually overlap -- an instant fake provider
    completes so fast that two threads rarely land inside the same
    race window regardless of whether the lock exists, which would make
    this test pass even with the lock removed and give a false sense of
    protection."""
    fake = FakeFiscalProvider(behavior="delayed", delay_seconds=0.2)
    fiscal_service.register_provider("FAKE", fake)

    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
        )
        setup_session.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
        setup_session.commit()

        sales_service.finalize_sale(
            setup_session,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
        )
        setup_session.commit()
        [submission] = fiscal_service.list_submissions(setup_session, store_id=store.id)
        submission_id = submission.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    results: list = [None, None]
    threads = [
        threading.Thread(
            target=_attempt_submit,
            kwargs=dict(submission_id=submission_id, barrier=barrier, results=results, index=i),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results[0] is not None and results[1] is not None
    assert results[0] == results[1], "both threads must observe the same final outcome"
    assert results[0][0] == "ACKNOWLEDGED"
    assert results[0][1] is not None
    assert len(fake.calls) == 1, "the row lock must prevent the provider being called twice"
