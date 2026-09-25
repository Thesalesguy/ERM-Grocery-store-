"""M20 Session C (sale lifecycle) and Session D (returns/void) --
docs/M20_DESIGN.md Section 2.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.fiscal import service as fiscal_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix
from tests.fiscal_fakes import FakeFiscalProvider


def _enable_fiscal(db: Session, store, fake: FakeFiscalProvider, *, updated_by: int) -> None:
    fiscal_service.register_provider("FAKE", fake)
    fiscal_service.upsert_config(
        db,
        store_id=store.id,
        is_enabled=True,
        provider_name="FAKE",
        credential_reference=None,
        submission_endpoint=None,
        retry_max_attempts=5,
        updated_by=updated_by,
    )


def _finalize_sale(db: Session, store, product, cashier):
    return sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("11.80"))],
    )


def test_full_lifecycle_sale_to_acknowledged(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    _enable_fiscal(db, store, fake, updated_by=cashier.id)
    db.commit()

    sale = _finalize_sale(db, store, product, cashier)
    db.commit()

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    assert submission.status == "PENDING"
    assert submission.sale_id == sale.id

    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()

    assert result.status == "ACKNOWLEDGED"
    assert result.fiscal_reference is not None
    assert result.fiscal_reference.startswith("FAKE-REF-")
    assert result.attempt_count == 1
    assert result.submitted_at is not None
    assert len(fake.calls) == 1


def test_rejection_is_terminal_and_never_auto_retried(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="rejection")
    _enable_fiscal(db, store, fake, updated_by=cashier.id)
    db.commit()

    _finalize_sale(db, store, product, cashier)
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)

    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "REJECTED"
    assert result.fiscal_reference is None

    # A second call (e.g. an operator's manual retry) is a pure no-op --
    # REJECTED is terminal, so the provider is never called again.
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert len(fake.calls) == 1


def test_disabling_fiscal_after_pending_row_created_leaves_it_untouched(db: Session) -> None:
    """Fiscalization was disabled for this store after the row was
    created -- an operator decision, not a failure (docs/M20_DESIGN.md
    Section 2's submit_fiscal_transaction docstring)."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    _enable_fiscal(db, store, fake, updated_by=cashier.id)
    db.commit()
    sale = _finalize_sale(db, store, product, cashier)
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)

    config = fiscal_service.get_config(db, store.id)
    assert config is not None
    config.is_enabled = False
    db.commit()

    result = fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    assert result.status == "PENDING"
    assert len(fake.calls) == 0
    assert sale.id == submission.sale_id


# --- Session D: returns / void ----------------------------------------------


def test_return_against_acknowledged_sale_does_not_mutate_original_submission(
    db: Session,
) -> None:
    """A return/void never retroactively alters the original sale's
    FiscalSubmission -- it is a new, separate record ABOUT the sale, and
    this milestone does not build a return-side fiscal submission (no
    confirmed requirement calls for one; the return itself is fully
    reflected in the sale's own already-immutable SaleItem.tax_refunded/
    accounting reversal -- docs/M20_DISCOVERY.md Section 2). This test
    proves the boundary holds: the original submission is untouched."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    _enable_fiscal(db, store, fake, updated_by=cashier.id)
    db.commit()

    sale = _finalize_sale(db, store, product, cashier)
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()
    original_reference = submission.fiscal_reference
    original_status = submission.status
    original_updated_at = submission.updated_at

    sales_service.void_sale(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        refund_method="CASH",
        client_transaction_id=f"void-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    db.refresh(submission)
    assert submission.status == original_status == "ACKNOWLEDGED"
    assert submission.fiscal_reference == original_reference
    assert submission.updated_at == original_updated_at
    # And voiding creates no second submission for this sale either.
    assert len(fiscal_service.list_submissions(db, store_id=store.id)) == 1


def test_return_itself_creates_no_fiscal_submission(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("11.80"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    _enable_fiscal(db, store, fake, updated_by=cashier.id)
    db.commit()

    sale = _finalize_sale(db, store, product, cashier)
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    fiscal_service.submit_fiscal_transaction(db, submission.id)
    db.commit()

    sale_item = sale.items[0]
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        lines=[SaleReturnLineInput(sale_item_id=sale_item.id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"return-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    # Still exactly one FiscalSubmission -- the sale's own -- not one for
    # the return.
    assert len(fiscal_service.list_submissions(db, store_id=store.id)) == 1
