"""M20 Session M: restart during a pending fiscalization must be safely
resumable -- the durable outbox row is the recovery mechanism
(docs/M20_DESIGN.md Section 2.1), not any in-memory state.

A real process restart is simulated the same way the rest of this
codebase proves durability across "process boundaries": use one
SessionLocal to create/partially-progress a submission and close it (as
a crashed process's connection would simply disappear), then use a
BRAND NEW, independent session/service call to resume -- if recovery
depends on nothing that lived only in the first session's memory, this
is a faithful proxy for an actual restart.
"""

from decimal import Decimal

from app.db.session import SessionLocal
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix
from tests.fiscal_fakes import FakeFiscalProvider


def test_pending_submission_survives_a_session_close_and_resumes_correctly() -> None:
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)

    creating_session = SessionLocal()
    try:
        store = make_store(creating_session)
        cashier = make_user(creating_session, store)
        product = make_product(
            creating_session,
            store,
            current_price=Decimal("11.80"),
            current_qty_on_hand=Decimal("100"),
        )
        creating_session.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
        creating_session.commit()

        sales_service.finalize_sale(
            creating_session,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{unique_suffix()}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
        )
        creating_session.commit()
        [submission] = fiscal_service.list_submissions(creating_session, store_id=store.id)
        submission_id = submission.id
        assert submission.status == "PENDING"
    finally:
        # Simulates the process dying right here: PENDING row committed,
        # nothing ever attempted, no in-memory state survives this.
        creating_session.close()

    # "Restart": a brand-new session/process resumes purely from the
    # durable row.
    recovery_session = SessionLocal()
    try:
        result = fiscal_service.submit_fiscal_transaction(recovery_session, submission_id)
        recovery_session.commit()
        assert result.status == "ACKNOWLEDGED"
        assert result.fiscal_reference is not None
    finally:
        recovery_session.close()

    assert len(fake.calls) == 1


def test_failed_submission_survives_restart_and_retry_succeeds() -> None:
    """A submission that FAILED right before an assumed crash is still
    retryable after "restart" -- attempt_count/last_error are durable,
    not lost."""
    fake = FakeFiscalProvider(behavior="success", fail_first_n_attempts=1)
    fiscal_service.register_provider("FAKE", fake)

    setup_session = SessionLocal()
    try:
        store = make_store(setup_session)
        cashier = make_user(setup_session, store)
        product = make_product(
            setup_session,
            store,
            current_price=Decimal("11.80"),
            current_qty_on_hand=Decimal("100"),
        )
        setup_session.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
        setup_session.commit()
        sales_service.finalize_sale(
            setup_session,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{unique_suffix()}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
        )
        setup_session.commit()
        [submission] = fiscal_service.list_submissions(setup_session, store_id=store.id)
        submission_id = submission.id

        first_attempt = fiscal_service.submit_fiscal_transaction(setup_session, submission_id)
        setup_session.commit()
        assert first_attempt.status == "FAILED"
        assert first_attempt.attempt_count == 1
    finally:
        setup_session.close()

    recovery_session = SessionLocal()
    try:
        reloaded = fiscal_service.get_submission(recovery_session, submission_id)
        assert reloaded.status == "FAILED"
        assert reloaded.attempt_count == 1

        result = fiscal_service.submit_fiscal_transaction(recovery_session, submission_id)
        recovery_session.commit()
        assert result.status == "ACKNOWLEDGED"
        assert result.attempt_count == 2
    finally:
        recovery_session.close()
