"""M20 Session G: fiscal totals vs. accounting-ledger totals vs. the
local Sale's own totals must never diverge (docs/M20_DESIGN.md Section 4)
-- across multi-line, mixed-rate, discount, and zero-tax cases.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_store, make_tax_rate, make_user, unique_suffix
from tests.fiscal_fakes import FakeFiscalProvider


def _tax_payable_credits_for_sale(db: Session, sale_id: int) -> Decimal:
    from app.modules.accounting.constants import ACCOUNT_TAX_PAYABLE

    total = db.execute(
        select(JournalLine.credit)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            Account.code == ACCOUNT_TAX_PAYABLE,
            JournalEntry.source_type == "SALE",
            JournalEntry.source_id == sale_id,
        )
    ).scalars()
    return sum(total, Decimal("0"))


def test_single_line_no_tax_reconciles(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
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

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    assert Decimal(submission.request_payload["tax_total"]) == sale.tax_total == Decimal("0.00")
    assert Decimal(submission.request_payload["grand_total"]) == sale.grand_total
    assert _tax_payable_credits_for_sale(db, sale.id) == Decimal("0")


def test_multi_line_mixed_tax_rates_reconciles(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    rate_a = make_tax_rate(db, rate_percent=Decimal("10.000"))
    rate_b = make_tax_rate(db, rate_percent=Decimal("20.000"))
    product_a = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_qty_on_hand=Decimal("100"),
        tax_rate_id=rate_a.id,
    )
    product_b = make_product(
        db,
        store,
        current_price=Decimal("25.00"),
        current_qty_on_hand=Decimal("100"),
        tax_rate_id=rate_b.id,
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(product_id=product_a.id, quantity=Decimal("2")),
            SaleLineInput(product_id=product_b.id, quantity=Decimal("1")),
        ],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("52.00"))],
    )
    db.commit()

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    payload = submission.request_payload
    assert Decimal(payload["tax_total"]) == sale.tax_total
    assert Decimal(payload["grand_total"]) == sale.grand_total
    payload_line_sum = sum(Decimal(line_["tax_amount"]) for line_ in payload["lines"])
    assert payload_line_sum == sale.tax_total
    assert _tax_payable_credits_for_sale(db, sale.id) == sale.tax_total


def test_discounted_line_reconciles(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("15.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("20.00"),
        current_qty_on_hand=Decimal("100"),
        tax_rate_id=tax_rate.id,
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(
                product_id=product.id, quantity=Decimal("1"), discount_amount=Decimal("5.00")
            )
        ],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
    )
    db.commit()

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    payload = submission.request_payload
    assert Decimal(payload["discount_total"]) == sale.discount_total == Decimal("5.00")
    assert Decimal(payload["tax_total"]) == sale.tax_total
    # Tax computed on the post-discount taxable amount: (20 - 5) * 15% = 2.25
    assert sale.tax_total == Decimal("2.25")
    assert _tax_payable_credits_for_sale(db, sale.id) == sale.tax_total


def test_fractional_quantity_rounding_reconciles(db: Session) -> None:
    """A weighed-product-style fractional quantity at a rate that does
    not divide evenly -- proves rounding is applied once, consistently,
    not double-rounded between the sale and the fiscal payload."""
    store = make_store(db)
    cashier = make_user(db, store)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("18.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("9.99"),
        current_qty_on_hand=Decimal("100"),
        tax_rate_id=tax_rate.id,
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2.375"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("30.00"))],
    )
    db.commit()

    [submission] = fiscal_service.list_submissions(db, store_id=store.id)
    assert Decimal(submission.request_payload["tax_total"]) == sale.tax_total
    assert Decimal(submission.request_payload["grand_total"]) == sale.grand_total
    assert _tax_payable_credits_for_sale(db, sale.id) == sale.tax_total
