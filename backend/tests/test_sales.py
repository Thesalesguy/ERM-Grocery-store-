"""Sales, sale items, payments, and sale returns.

Focus: COGS/historical reproducibility (a sale item's frozen cost must
never move, even after a later purchase changes the product's WAC), and
the stored-total consistency CHECK constraints (M1 task Section 19).
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales.models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
    unique_suffix,
)


def _make_sale(db: Session, store, cashier, *, subtotal, discount_total, tax_total) -> Sale:
    sale = Sale(
        store_id=store.id,
        sale_number=f"SALE-{unique_suffix()}",
        client_transaction_id=f"txn-{unique_suffix()}",
        cashier_id=cashier.id,
        status="COMPLETED",
        subtotal=subtotal,
        discount_total=discount_total,
        tax_total=tax_total,
        grand_total=subtotal - discount_total + tax_total,
    )
    db.add(sale)
    db.flush()
    return sale


def test_sale_item_freezes_cost_independent_of_later_wac_changes(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("10.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("10.00"))],
    )
    db.commit()
    db.refresh(product)
    assert product.current_cost == Decimal("10.000000")

    sale = _make_sale(db, store, cashier, subtotal=Decimal("10.00"), discount_total=0, tax_total=0)
    sale_item = SaleItem(
        sale_id=sale.id,
        product_id=product.id,
        quantity=Decimal("1"),
        unit_price_at_sale=Decimal("10.00"),
        unit_cost_at_sale=product.current_cost,  # frozen at 10.000000
        line_total=Decimal("10.00"),
    )
    db.add(sale_item)
    db.commit()

    # Now a second purchase at a very different cost moves the product's
    # live WAC — the already-recorded sale line must NOT move with it.
    po2 = make_purchase_order(db, store, supplier)
    item2 = PurchaseOrderItem(
        purchase_order_id=po2.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("50.00"),
    )
    db.add(item2)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po2.id,
        received_date=date(2024, 2, 1),
        lines=[GoodsReceiptLineInput(item2.id, Decimal("10"), Decimal("50.00"))],
    )
    db.commit()
    db.refresh(product)

    assert product.current_cost != Decimal("10.000000")  # WAC did move
    db.refresh(sale_item)
    assert sale_item.unit_cost_at_sale == Decimal("10.000000")  # history did not


def test_grand_total_must_equal_subtotal_minus_discount_plus_tax(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    db.commit()

    # Valid: succeeds.
    _make_sale(
        db,
        store,
        cashier,
        subtotal=Decimal("100.00"),
        discount_total=Decimal("10.00"),
        tax_total=Decimal("18.00"),
    )
    db.commit()

    # Invalid: grand_total inconsistent with its own row's other columns.
    bad_sale = Sale(
        store_id=store.id,
        sale_number=f"SALE-{unique_suffix()}",
        client_transaction_id=f"txn-{unique_suffix()}",
        cashier_id=cashier.id,
        status="COMPLETED",
        subtotal=Decimal("100.00"),
        discount_total=Decimal("10.00"),
        tax_total=Decimal("18.00"),
        grand_total=Decimal("999.00"),  # wrong on purpose
    )
    db.add(bad_sale)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_sale_item_line_total_must_match_its_own_arithmetic(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    sale = _make_sale(db, store, cashier, subtotal=Decimal("20.00"), discount_total=0, tax_total=0)
    db.commit()

    db.add(
        SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=Decimal("2"),
            unit_price_at_sale=Decimal("10.00"),
            unit_cost_at_sale=Decimal("5.00"),
            discount_amount=Decimal("0"),
            tax_amount=Decimal("0"),
            line_total=Decimal("999.00"),  # should be 20.00
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_invalid_sale_status_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    db.commit()

    db.add(
        Sale(
            store_id=store.id,
            sale_number=f"SALE-{unique_suffix()}",
            client_transaction_id=f"txn-{unique_suffix()}",
            cashier_id=cashier.id,
            status="NOT_A_REAL_STATUS",
            subtotal=0,
            discount_total=0,
            tax_total=0,
            grand_total=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_split_tender_payments_sum_to_grand_total(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    db.commit()
    sale = _make_sale(db, store, cashier, subtotal=Decimal("100.00"), discount_total=0, tax_total=0)
    db.commit()

    db.add(Payment(sale_id=sale.id, payment_method="CASH", amount=Decimal("60.00")))
    db.add(Payment(sale_id=sale.id, payment_method="CARD", amount=Decimal("40.00")))
    db.commit()
    db.refresh(sale)

    total_paid = sum((p.amount for p in sale.payments), Decimal("0"))
    assert total_paid == sale.grand_total


def test_payment_amount_must_be_positive(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    db.commit()
    sale = _make_sale(db, store, cashier, subtotal=Decimal("10.00"), discount_total=0, tax_total=0)
    db.commit()

    db.add(Payment(sale_id=sale.id, payment_method="CASH", amount=Decimal("0")))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_sale_return_data_model(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    sale = _make_sale(db, store, cashier, subtotal=Decimal("10.00"), discount_total=0, tax_total=0)
    db.commit()
    sale_item = SaleItem(
        sale_id=sale.id,
        product_id=product.id,
        quantity=Decimal("1"),
        unit_price_at_sale=Decimal("10.00"),
        unit_cost_at_sale=Decimal("5.00"),
        line_total=Decimal("10.00"),
    )
    db.add(sale_item)
    db.commit()

    sale_return = SaleReturn(
        sale_id=sale.id,
        store_id=store.id,
        return_number=f"RET-{unique_suffix()}",
        reason="Customer changed mind",
        refund_method="CASH",
        refund_amount=Decimal("10.00"),
        processed_by=cashier.id,
    )
    db.add(sale_return)
    db.flush()
    db.add(
        SaleReturnItem(
            sale_return_id=sale_return.id,
            sale_item_id=sale_item.id,
            quantity=Decimal("1"),
            unit_price_refunded=Decimal("10.00"),
            restock=True,
        )
    )
    db.commit()

    assert sale_return.items[0].restock is True
    # BR-6: the original sale itself is never edited by a return.
    db.refresh(sale)
    assert sale.status == "COMPLETED"
