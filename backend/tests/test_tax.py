"""Tax rate model: historical capture and DB-level constraints.

Nothing here (or anywhere in the codebase) hardcodes 18% or any other
figure as a permanent rule — 18% below is only a realistic example value
used the same way a real deployment's operator would configure one.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.sales.models import Sale, SaleItem
from app.modules.tax.models import TaxRate
from tests.factories import make_product, make_store, make_tax_rate, make_user, unique_suffix


def test_sale_item_keeps_its_historical_tax_rate_after_the_rate_changes(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(db, store)
    original_rate = make_tax_rate(db, rate_percent=Decimal("18.000"))
    db.commit()

    sale = Sale(
        store_id=store.id,
        sale_number=f"SALE-{unique_suffix()}",
        cashier_id=cashier.id,
        status="COMPLETED",
        subtotal=Decimal("10.00"),
        discount_total=0,
        tax_total=Decimal("1.80"),
        grand_total=Decimal("11.80"),
    )
    db.add(sale)
    db.flush()
    sale_item = SaleItem(
        sale_id=sale.id,
        product_id=product.id,
        quantity=Decimal("1"),
        unit_price_at_sale=Decimal("10.00"),
        unit_cost_at_sale=Decimal("5.00"),
        tax_rate_id=original_rate.id,
        tax_amount=Decimal("1.80"),
        line_total=Decimal("11.80"),
    )
    db.add(sale_item)
    db.commit()

    # The rate changes: the old one is retired, a new one takes over.
    original_rate.is_active = False
    original_rate.effective_to = date(2024, 6, 30)
    new_rate = TaxRate(
        name="Standard VAT (new)",
        rate_percent=Decimal("20.000"),
        is_active=True,
        effective_from=date(2024, 7, 1),
    )
    db.add(new_rate)
    db.commit()

    db.refresh(sale_item)
    db.refresh(original_rate)
    # The historical sale line still points at the original rate/value —
    # completely unaffected by the new rate existing.
    assert sale_item.tax_rate_id == original_rate.id
    assert original_rate.rate_percent == Decimal("18.000")
    assert sale_item.tax_amount == Decimal("1.80")


def test_tax_rate_percent_must_be_within_bounds(db: Session) -> None:
    db.add(
        TaxRate(
            name="Bad rate",
            rate_percent=Decimal("150.000"),
            is_active=True,
            effective_from=date(2024, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_tax_rate_effective_to_cannot_precede_effective_from(db: Session) -> None:
    db.add(
        TaxRate(
            name="Bad range",
            rate_percent=Decimal("18.000"),
            is_active=True,
            effective_from=date(2024, 6, 1),
            effective_to=date(2024, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
