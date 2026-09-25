"""M20 Session N: adversarial inputs -- malformed config, stale
configuration mid-flight, invalid direct status transitions, unauthorized
store IDs, and provider-lookup failures.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import ADMIN
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig, FiscalSubmission
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_user, make_user_with_role, unique_suffix
from tests.fiscal_fakes import FakeFiscalProvider


def test_upsert_config_with_nonexistent_store_id_violates_fk(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db, store)
    with pytest.raises(IntegrityError):
        fiscal_service.upsert_config(
            db,
            store_id=99_999_999,
            is_enabled=True,
            provider_name="FAKE",
            credential_reference=None,
            submission_endpoint=None,
            retry_max_attempts=5,
            updated_by=admin.id,
        )
        db.flush()
    db.rollback()


def test_retry_max_attempts_zero_is_rejected(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db, store)
    with pytest.raises(IntegrityError):
        fiscal_service.upsert_config(
            db,
            store_id=store.id,
            is_enabled=True,
            provider_name="FAKE",
            credential_reference=None,
            submission_endpoint=None,
            retry_max_attempts=0,
            updated_by=admin.id,
        )
        db.flush()
    db.rollback()


def test_submit_unknown_submission_id_raises_not_found(db: Session) -> None:
    with pytest.raises(NotFoundError):
        fiscal_service.submit_fiscal_transaction(db, 99_999_999)


def test_get_submission_unknown_id_raises_not_found(db: Session) -> None:
    with pytest.raises(NotFoundError):
        fiscal_service.get_submission(db, 99_999_999)


def test_unregistered_provider_name_raises_not_found(db: Session) -> None:
    """A FiscalConfig pointing at a provider_name that was never
    registered (a real deployment misconfiguration) fails loudly rather
    than silently no-op'ing or crashing with an unrelated KeyError."""
    store = make_store(db)
    cashier = make_user_with_role(
        db, store, ADMIN, username=f"fiscal_adv_cashier_{unique_suffix()}"
    )
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="NEVER_REGISTERED"))
    db.commit()

    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)

    with pytest.raises(NotFoundError):
        fiscal_service.submit_fiscal_transaction(db, submission.id)


def test_direct_forward_status_jump_past_terminal_is_rejected_by_no_check_constraint_bypass(
    db: Session,
) -> None:
    """A submission cannot be created with an invalid status string at
    all -- the CHECK constraint is the backstop against ANY code path
    (buggy or malicious) writing a status outside the defined set,
    including a value that looks like a plausible "in progress" state
    nobody defined."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, ADMIN, username=f"fiscal_adv_2_{unique_suffix()}")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()

    db.add(
        FiscalSubmission(
            sale_id=sale.id,
            store_id=store.id,
            status="SUBMITTING",  # not a real status
            provider_name="FAKE",
            max_attempts=5,
            request_payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_stale_config_disabled_mid_flight_does_not_resurrect_a_terminal_submission(
    db: Session,
) -> None:
    """Disabling fiscalization after a submission already reached a
    terminal state changes nothing about that submission -- disabling is
    a forward-looking switch, never a retroactive one."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, ADMIN, username=f"fiscal_adv_3_{unique_suffix()}")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert submission.status == "ACKNOWLEDGED"

    config = fiscal_service.get_config(db, store.id)
    config.is_enabled = False
    db.commit()

    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert submission.status == "ACKNOWLEDGED"
    assert len(fake.calls) == 1
