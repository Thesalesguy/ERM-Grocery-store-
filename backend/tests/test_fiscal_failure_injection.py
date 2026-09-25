"""M20 Session F: the 12 failure scenarios named in docs/M20_DESIGN.md
Phase 3 / M20 mission Phase 2, exercised against FakeFiscalProvider.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix
from tests.fiscal_fakes import FakeFiscalProvider


def _setup(db: Session, fake: FakeFiscalProvider, *, max_attempts: int = 5):
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fiscal_service.register_provider("FAKE", fake)
    db.add(
        FiscalConfig(
            store_id=store.id,
            is_enabled=True,
            provider_name="FAKE",
            retry_max_attempts=max_attempts,
        )
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
    )
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    return sale, submission


# 1. First submission succeeds.
def test_first_submission_succeeds(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="success")
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "ACKNOWLEDGED"


# 2. Same submission retried (after success) -- pure no-op.
def test_retry_after_success_is_a_no_op(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="success")
    _sale, submission = _setup(db, fake)
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert len(fake.calls) == 1


# 3. Timeout after remote acceptance is indistinguishable from timeout
#    before -- both surface as FiscalProviderError -> FAILED, retried.
def test_timeout_marks_failed_and_is_retryable(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="timeout")
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "FAILED"
    assert result.last_error is not None
    assert result.attempt_count == 1


# 4. Remote rejection -- terminal, not retried.
def test_remote_rejection_is_terminal(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="rejection")
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "REJECTED"
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert len(fake.calls) == 1


# 5. Network failure before submission (connection refused outright).
def test_connection_failure_before_submission(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="connection_failure")
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "FAILED"


# 6. Network failure after submission (same as #3 -- no distinguishable
#    signal on our side; the retry/idempotency-key design is the answer).
def test_network_failure_after_submission_still_recovers_on_retry(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="success", fail_first_n_attempts=1)
    _sale, submission = _setup(db, fake)
    first = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert first.status == "FAILED"
    second = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert second.status == "ACKNOWLEDGED"
    assert second.attempt_count == 2


# 7. Concurrent duplicate submission -- covered exhaustively in
#    test_fiscal_idempotency.py; referenced here for session completeness.


# 8/9. Application restart during submission / retry after restart --
#    simulated by using a fresh DB session (a new "process") to resume
#    a PENDING row created by an earlier one; see test_fiscal_recovery.py
#    (Session M) for the dedicated scenario.


# 10. Duplicate external response (the provider's own dedup-by-key path).
def test_duplicate_external_acknowledgement_returns_same_reference(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="success")
    _sale, submission = _setup(db, fake)
    first = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    # Directly exercise the provider a second time with the SAME payload
    # (simulating an external retry the provider itself sees) -- proves
    # its own dedup-by-idempotency-key behavior, which our retry design
    # depends on for the honest guarantee described in
    # docs/M20_DESIGN.md Section 3.
    second_outcome = fake.submit(submission.request_payload)
    assert second_outcome.fiscal_reference == first.fiscal_reference


# 11. Invalid/unparseable response from the authority.
def test_malformed_response_marks_failed(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="malformed")
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "FAILED"
    assert result.last_error is not None


# 12. Delayed response -- succeeds, just slower; proves no timeout logic
#     in this layer falsely marks a slow-but-successful call as failed.
def test_delayed_response_still_succeeds(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="delayed", delay_seconds=0.05)
    _sale, submission = _setup(db, fake)
    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "ACKNOWLEDGED"


# Retry-cap / EXHAUSTED terminal state.
def test_exhausted_after_max_attempts_becomes_terminal(db: Session) -> None:
    fake = FakeFiscalProvider(behavior="timeout")
    _sale, submission = _setup(db, fake, max_attempts=3)
    for _ in range(3):
        result = fiscal_service.submit_fiscal_transaction(db, submission.id)
        db.commit()
    assert result.status == "EXHAUSTED"
    assert result.attempt_count == 3

    # EXHAUSTED is terminal -- a further call is a no-op, not a 4th attempt.
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert len(fake.calls) == 3
