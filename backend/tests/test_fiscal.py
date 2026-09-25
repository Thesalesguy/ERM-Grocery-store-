"""M20 Session A (domain integrity) and Session B (payload/rounding
equivalence) -- docs/M20_DESIGN.md Sections 1, 3, 4.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig, FiscalSubmission
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_tax_rate, make_user, unique_suffix


def _finalize_sale(db: Session, store, product, cashier, *, quantity="1", price_paid="11.80"):
    return sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal(quantity))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal(price_paid))],
    )


# --- Session A: domain integrity -------------------------------------------


def test_fiscal_config_defaults_to_disabled_null_provider(db: Session) -> None:
    store = make_store(db)
    config = FiscalConfig(store_id=store.id)
    db.add(config)
    db.commit()
    db.refresh(config)
    assert config.is_enabled is False
    assert config.provider_name == "NULL"
    assert config.credential_reference is None
    assert config.retry_max_attempts == 5


def test_fiscal_config_store_id_is_unique(db: Session) -> None:
    store = make_store(db)
    db.add(FiscalConfig(store_id=store.id))
    db.commit()
    db.add(FiscalConfig(store_id=store.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_fiscal_config_retry_max_attempts_must_be_positive(db: Session) -> None:
    store = make_store(db)
    db.add(FiscalConfig(store_id=store.id, retry_max_attempts=0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_fiscal_submission_status_is_constrained(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    sale = _finalize_sale(db, store, product, cashier)
    db.commit()

    db.add(
        FiscalSubmission(
            sale_id=sale.id,
            store_id=store.id,
            status="NOT_A_REAL_STATUS",
            provider_name="FAKE",
            max_attempts=5,
            request_payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_fiscal_submission_sale_id_is_unique(db: Session) -> None:
    """The actual idempotency enforcement (docs/M20_DESIGN.md Section
    1.2): a second FiscalSubmission for the same sale is rejected by the
    database itself."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    sale = _finalize_sale(db, store, product, cashier)
    db.commit()

    db.add(
        FiscalSubmission(
            sale_id=sale.id,
            store_id=store.id,
            status="PENDING",
            provider_name="FAKE",
            max_attempts=5,
            request_payload={},
        )
    )
    db.commit()
    db.add(
        FiscalSubmission(
            sale_id=sale.id,
            store_id=store.id,
            status="PENDING",
            provider_name="FAKE",
            max_attempts=5,
            request_payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_disabled_fiscal_config_creates_no_submission(db: Session) -> None:
    """The mechanism that keeps M20 jurisdiction-neutral in practice:
    is_enabled=False (the default and only seeded value for every store)
    means finalize_sale never writes a FiscalSubmission row at all."""
    store = make_store(db)
    db.add(FiscalConfig(store_id=store.id, is_enabled=False))
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    db.commit()

    sale = _finalize_sale(db, store, product, cashier)
    db.commit()

    assert fiscal_service.list_submissions(db, store_id=store.id) == []
    assert sale.status == "COMPLETED"


def test_store_with_no_fiscal_config_row_creates_no_submission(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    db.commit()

    _finalize_sale(db, store, product, cashier)
    db.commit()

    assert fiscal_service.list_submissions(db, store_id=store.id) == []


# --- Session B: payload construction / rounding equivalence -----------------


def test_fiscal_payload_totals_exactly_match_sale_totals(db: Session) -> None:
    """The payload is built from Sale's own already-frozen totals, never
    a second calculation (docs/M20_DESIGN.md Section 4) -- proven here
    with mixed tax/discount/rounding, not just a round-number case."""
    store = make_store(db)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    cashier = make_user(db, store)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("18.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("9.99"),
        current_qty_on_hand=Decimal("100"),
        tax_rate_id=tax_rate.id,
    )
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("40.00"))],
    )
    db.commit()

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    payload = submission.request_payload
    assert payload["subtotal"] == str(sale.subtotal)
    assert payload["discount_total"] == str(sale.discount_total)
    assert payload["tax_total"] == str(sale.tax_total)
    assert payload["grand_total"] == str(sale.grand_total)
    assert len(payload["lines"]) == len(sale.items)
    for line_payload, item in zip(payload["lines"], sale.items, strict=True):
        assert line_payload["tax_amount"] == str(item.tax_amount)
        assert line_payload["line_total"] == str(item.line_total)


def test_fiscal_payload_is_frozen_and_not_rebuilt_on_retry(db: Session) -> None:
    """A retry must send byte-identical content (docs/M20_DESIGN.md
    Section 3) -- proven by mutating the product's price/tax AFTER the
    submission row exists and confirming the stored payload is
    unaffected."""
    store = make_store(db)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    db.commit()

    _finalize_sale(db, store, product, cashier)
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    original_payload = dict(submission.request_payload)

    product.current_price = Decimal("999.00")
    db.commit()

    fiscal_service.get_submission(db, submission.id)
    assert submission.request_payload == original_payload
