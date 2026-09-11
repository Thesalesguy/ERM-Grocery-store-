"""Sale returns and voids: domain/service logic (M5 Session B) and
accounting integration (M5 Session C).

Focus: a return must use ONLY the original sale's frozen values (never
current price/tax/WAC/product config), quantity limits are enforced
server-side, discount/tax telescope correctly across partial returns,
restocked returns correctly blend into WAC via the EXISTING
compute_new_wac (no new algorithm), and every posted return journal
balances and derives from the same values used for the inventory
movement.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import (
    ACCOUNT_CASH_ON_HAND,
    ACCOUNT_COGS,
    ACCOUNT_INVENTORY,
    ACCOUNT_SALES_DISCOUNTS,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_TAX_PAYABLE,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale, SaleItem
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_tax_rate,
    make_user,
    unique_suffix,
)


def _sell(
    db: Session, store, cashier, product, *, quantity, discount_amount=Decimal("0"), amount=None
) -> Sale:
    price = product.current_price
    grand = (price * quantity - discount_amount).quantize(Decimal("0.01"))
    return sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(product_id=product.id, quantity=quantity, discount_amount=discount_amount)
        ],
        payments=[PaymentInput(payment_method="CASH", amount=amount or grand)],
    )


def _lines_for(db: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id)).scalars()
    )


def _assert_balanced(db: Session, entry: JournalEntry) -> None:
    lines = _lines_for(db, entry)
    assert lines
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) > 0


def _account_id(db: Session, code: str) -> int:
    return db.execute(select(Account.id).where(Account.code == code)).scalar_one()


# --- Basic return lifecycle --------------------------------------------


def test_full_return_of_a_single_line_sale(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("20"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("3"))
    db.commit()

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("3"), restock=True)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    assert sale_return.refund_amount == Decimal("30.00")
    db.refresh(sale)
    assert sale.status == "REFUNDED"
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("20.000")  # 20 - 3 (sold) + 3 (returned)
    assert product.current_cost == Decimal("4.000000")  # WAC restored exactly


def test_partial_quantity_return(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("20"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("5"))
    db.commit()

    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(sale)
    assert sale.status == "PARTIALLY_REFUNDED"
    item = db.get(SaleItem, sale.items[0].id)
    assert item.quantity_returned == Decimal("2.000")


def test_multiple_return_transactions_against_the_same_sale(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("20"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("5"))
    db.commit()

    for qty in (Decimal("2"), Decimal("1"), Decimal("2")):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=qty)],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
        db.commit()

    db.refresh(sale)
    assert sale.status == "REFUNDED"
    item = db.get(SaleItem, sale.items[0].id)
    assert item.quantity_returned == Decimal("5.000")


def test_cannot_return_more_than_sold(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("3"))
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("4"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code == "EXCESSIVE_RETURN_QUANTITY"


def test_cannot_return_already_returned_quantity(db: Session) -> None:
    """Returns the full 3-unit line first (so sale.status becomes
    REFUNDED and blocks any further return via SALE_NOT_RETURNABLE —
    itself a correct guard), then proves the SAME guard fires for a
    partially-returned line still in PARTIALLY_REFUNDED state, where
    EXCESSIVE_RETURN_QUANTITY is the actual quantity-limit check being
    exercised."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("3"))
    db.commit()
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("3"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(sale)
    assert sale.status == "REFUNDED"

    with pytest.raises(ConflictError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 3),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code == "SALE_NOT_RETURNABLE"

    # Now the quantity-limit guard specifically, on a line with some
    # (but not all) quantity already returned.
    store2 = make_store(db)
    cashier2 = make_user(db, store2)
    product2 = make_product(
        db, store2, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale2 = _sell(db, store2, cashier2, product2, quantity=Decimal("3"))
    db.commit()
    sales_service.create_sale_return(
        db,
        sale_id=sale2.id,
        store_id=store2.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale2.items[0].id, quantity=Decimal("2"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier2.id,
    )
    db.commit()
    db.refresh(sale2)
    assert sale2.status == "PARTIALLY_REFUNDED"

    with pytest.raises(ConflictError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale2.id,
            store_id=store2.id,
            return_date=date(2024, 1, 3),
            # only 1 remains (3 - 2 already returned); asking for 2 more
            lines=[SaleReturnLineInput(sale_item_id=sale2.items[0].id, quantity=Decimal("2"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier2.id,
        )
    assert exc_info.value.error_code == "EXCESSIVE_RETURN_QUANTITY"


def test_cannot_return_against_a_sale_item_from_a_different_sale(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale_a = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    sale_b = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()

    with pytest.raises(NotFoundError):
        sales_service.create_sale_return(
            db,
            sale_id=sale_a.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            # tampered: this sale_item_id belongs to sale_b, not sale_a
            lines=[SaleReturnLineInput(sale_item_id=sale_b.items[0].id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )


def test_non_restock_return_does_not_increase_inventory(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("20"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("3"))
    db.commit()
    db.refresh(product)
    qty_after_sale = product.current_qty_on_hand

    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("3"), restock=False)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == qty_after_sale  # unchanged — damaged goods scrapped


# --- WAC / cost basis ---------------------------------------------------


def test_return_blends_into_current_wac_using_original_sale_cost(db: Session) -> None:
    """Sell at WAC 4, receive more stock at a different cost (driving WAC
    up), THEN return the original sale — the return must blend the
    RETURNED units back in at their own original cost (4), not the
    product's current (higher) WAC."""
    store = make_store(db)
    supplier = make_supplier(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("8.000")

    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("40.000000"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("40.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    # WAC after this receipt: (8*4 + 10*40)/18 = (32+400)/18 = 24.0
    assert product.current_cost == Decimal("24.000000")

    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 3),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(product)
    # New WAC: (18*24 + 2*4)/20 = (432+8)/20 = 22.0 — blended at the
    # RETURNED units' own original cost (4), never current WAC (24).
    assert product.current_qty_on_hand == Decimal("20.000")
    assert product.current_cost == Decimal("22.000000")


def test_return_cogs_reversal_uses_frozen_cost_not_current_wac(db: Session) -> None:
    """The financial-control test explicitly required by the M5 task:
    sell at cost A, change WAC to cost B, return — the return's journal
    COGS reversal must use A, never B."""
    store = make_store(db)
    supplier = make_supplier(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("20.00"),
        current_cost=Decimal("5.000000"),
        current_qty_on_hand=Decimal("50"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()

    sale_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    original_cogs = next(
        line.debit
        for line in _lines_for(db, sale_entry)
        if line.account_id == _account_id(db, ACCOUNT_COGS)
    )
    assert original_cogs == Decimal("10.000000")  # 2 * 5.00

    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("50"),
        unit_cost=Decimal("500.000000"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("500.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    assert product.current_cost != Decimal("5.000000")  # WAC really moved

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 3),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    return_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()
    _assert_balanced(db, return_entry)
    return_cogs_credit = next(
        line.credit
        for line in _lines_for(db, return_entry)
        if line.account_id == _account_id(db, ACCOUNT_COGS)
    )
    assert return_cogs_credit == Decimal("10.000000")  # still A (5.00), not B (500.00)

    # The ORIGINAL sale's journal must remain untouched too.
    original_cogs_after = next(
        line.debit
        for line in _lines_for(db, sale_entry)
        if line.account_id == _account_id(db, ACCOUNT_COGS)
    )
    assert original_cogs_after == Decimal("10.000000")


# --- Discount / tax telescoping -----------------------------------------


def test_partial_returns_of_a_discounted_line_never_exceed_original_discount(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    # 3 units @ 10.00 = 30.00, discount 3.00 (a discount that doesn't
    # divide evenly by 3, forcing rounding on every partial return).
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(
                product_id=product.id, quantity=Decimal("3"), discount_amount=Decimal("3.00")
            )
        ],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("27.00"))],
    )
    db.commit()
    sale_item_id = sale.items[0].id

    total_discount_refunded = Decimal("0")
    for qty in (Decimal("1"), Decimal("1"), Decimal("1")):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=qty)],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
        db.commit()
        item = (
            db.execute(
                select(sales_service.SaleReturnItem).order_by(
                    sales_service.SaleReturnItem.id.desc()
                )
            )
            .scalars()
            .first()
        )
        total_discount_refunded += item.discount_refunded

    # Telescoping guarantees the sum exactly equals the original discount
    # once fully returned, regardless of how 3.00/3 rounds per step.
    assert total_discount_refunded == Decimal("3.00")


def test_tax_reversal_uses_historical_tax_not_current_rate(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    old_rate = make_tax_rate(db, rate_percent=Decimal("10.000"), effective_from=date(2020, 1, 1))
    product = make_product(
        db,
        store,
        current_price=Decimal("100.00"),
        current_qty_on_hand=Decimal("20"),
        tax_rate_id=old_rate.id,
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("110.00"))],
    )
    db.commit()
    assert sale.tax_total == Decimal("10.00")

    # Tax rate configuration changes AFTER the sale.
    old_rate.is_active = False
    new_rate = make_tax_rate(db, rate_percent=Decimal("25.000"), effective_from=date(2024, 1, 1))
    product.tax_rate_id = new_rate.id
    db.commit()

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 3),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    assert sale_return.refund_amount == Decimal("110.00")  # 100 + 10% (OLD rate), not 25%

    return_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()
    tax_line = next(
        line
        for line in _lines_for(db, return_entry)
        if line.account_id == _account_id(db, ACCOUNT_TAX_PAYABLE)
    )
    assert tax_line.debit == Decimal("10.000000")


# --- Accounting balance/accounts ----------------------------------------


def test_return_journal_debits_revenue_and_tax_credits_discount_and_cash(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("10.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("100.00"),
        current_cost=Decimal("40.000000"),
        current_qty_on_hand=Decimal("20"),
        tax_rate_id=tax_rate.id,
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(
                product_id=product.id, quantity=Decimal("1"), discount_amount=Decimal("10.00")
            )
        ],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("99.00"))],
    )
    db.commit()
    # subtotal=100, discount=10, taxable=90, tax=9, grand_total=99

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    assert sale_return.refund_amount == Decimal("99.00")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_SALES_REVENUE)].debit == Decimal("100.000000")
    assert lines[_account_id(db, ACCOUNT_TAX_PAYABLE)].debit == Decimal("9.000000")
    assert lines[_account_id(db, ACCOUNT_SALES_DISCOUNTS)].credit == Decimal("10.000000")
    assert lines[_account_id(db, ACCOUNT_CASH_ON_HAND)].credit == Decimal("99.000000")
    assert lines[_account_id(db, ACCOUNT_COGS)].credit == Decimal("40.000000")
    assert lines[_account_id(db, ACCOUNT_INVENTORY)].debit == Decimal("40.000000")


def test_non_restock_return_posts_no_inventory_cogs_pair(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("20"),
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[
            SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("2"), restock=False)
        ],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    account_ids = {line.account_id for line in _lines_for(db, entry)}
    assert _account_id(db, ACCOUNT_INVENTORY) not in account_ids
    assert _account_id(db, ACCOUNT_COGS) not in account_ids


def test_automated_sale_return_journal_cannot_be_reversed(db: Session) -> None:
    """SALE_RETURN is already in M4's AUTOMATED_SOURCE_TYPES block list —
    confirms it actually applies here, not just in theory."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db, journal_entry_id=entry.id, reason="x", reversed_by=None, caller_store_id=None
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


# --- Void ----------------------------------------------------------------


def test_void_sale_returns_every_remaining_line_in_full(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product_a = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    product_b = make_product(
        db, store, current_price=Decimal("5.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[
            SaleLineInput(product_id=product_a.id, quantity=Decimal("2")),
            SaleLineInput(product_id=product_b.id, quantity=Decimal("3")),
        ],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("35.00"))],
    )
    db.commit()

    sales_service.void_sale(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        refund_method="CASH",
        client_transaction_id=f"void-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(sale)
    assert sale.status == "REFUNDED"
    for item in sale.items:
        assert item.quantity_returned == item.quantity


def test_void_sale_with_nothing_left_is_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    sales_service.void_sale(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        refund_method="CASH",
        client_transaction_id=f"void-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        sales_service.void_sale(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 3),
            refund_method="CASH",
            client_transaction_id=f"void-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code == "NOTHING_TO_VOID"


# --- Validation edge cases -------------------------------------------------


def test_empty_return_is_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    with pytest.raises(ValidationAppError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code == "EMPTY_RETURN"


def test_negative_and_zero_return_quantity_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("3"))
    db.commit()
    for bad_qty in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationAppError) as exc_info:
            sales_service.create_sale_return(
                db,
                sale_id=sale.id,
                store_id=store.id,
                return_date=date(2024, 1, 2),
                lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=bad_qty)],
                refund_method="CASH",
                client_transaction_id=f"ret-{unique_suffix()}",
                caller_store_id=None,
                created_by=cashier.id,
            )
        assert exc_info.value.error_code == "INVALID_QUANTITY"


def test_invalid_refund_method_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    with pytest.raises(ValidationAppError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
            refund_method="BITCOIN",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code == "INVALID_REFUND_METHOD"


def test_return_against_nonexistent_sale_not_found(db: Session) -> None:
    with pytest.raises(NotFoundError):
        sales_service.create_sale_return(
            db,
            sale_id=999999999,
            store_id=1,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=1, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=None,
        )


def test_return_against_already_fully_refunded_sale_rejected(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 3),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc_info.value.error_code in ("SALE_NOT_RETURNABLE", "EXCESSIVE_RETURN_QUANTITY")
