"""The M4 double-entry accounting core: chart of accounts, journal
posting for each operational source, reversal, and reports.

Focus: every posted journal entry balances, the Inventory-account amount
always matches the operational movement it accounts for exactly (never a
second, independently-derived valuation), and COGS/journal amounts never
move after the fact even when the product's WAC changes later — the same
"historical facts are frozen" invariant M1/M2/M3 already established for
sales/purchases, now proven for the accounting layer built on top of them.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import (
    ACCOUNT_CASH_ON_HAND,
    ACCOUNT_COGS,
    ACCOUNT_INVENTORY,
    ACCOUNT_INVENTORY_ADJUSTMENT_GAIN,
    ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
    ACCOUNT_PURCHASE_CLEARING,
    ACCOUNT_SALES_DISCOUNTS,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_TAX_PAYABLE,
    SYSTEM_ACCOUNTS,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.inventory import service as inventory_service
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseReturnLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_tax_rate,
    make_user,
    unique_suffix,
)


def _lines_for(db: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id)).scalars()
    )


def _assert_balanced(db: Session, entry: JournalEntry) -> None:
    lines = _lines_for(db, entry)
    assert lines, "entry has no lines"
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) > 0


def _account_id(db: Session, code: str) -> int:
    return db.execute(select(Account.id).where(Account.code == code)).scalar_one()


# --- Chart of accounts -------------------------------------------------------


def test_chart_of_accounts_is_seeded_and_matches_constants(db: Session) -> None:
    accounts = accounting_service.list_accounts(db)
    codes = {a.code for a in accounts}
    assert codes == {code for code, *_ in SYSTEM_ACCOUNTS}
    for account in accounts:
        assert account.is_system is True
        assert account.is_active is True
        assert account.account_type in ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE")
        assert account.normal_balance in ("DEBIT", "CREDIT")


def test_sales_discounts_is_a_contra_revenue_account(db: Session) -> None:
    account = db.execute(
        select(Account).where(Account.code == ACCOUNT_SALES_DISCOUNTS)
    ).scalar_one()
    assert account.account_type == "REVENUE"
    assert account.normal_balance == "DEBIT"  # contra to Sales Revenue's CREDIT normal balance


# --- Sales accounting --------------------------------------------------------


def _setup_sale_fixture(
    db: Session, *, price: Decimal, cost: Decimal, qty: Decimal = Decimal("100")
):
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=price, current_cost=cost, current_qty_on_hand=qty
    )
    db.commit()
    return store, cashier, product


def test_cash_sale_posts_a_balanced_entry_with_expected_accounts(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("6.000000")
    )
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines_by_account = {line.account_id: line for line in _lines_for(db, entry)}

    cash_line = lines_by_account[_account_id(db, ACCOUNT_CASH_ON_HAND)]
    assert cash_line.debit == Decimal("20.000000")
    revenue_line = lines_by_account[_account_id(db, ACCOUNT_SALES_REVENUE)]
    assert revenue_line.credit == Decimal("20.000000")
    cogs_line = lines_by_account[_account_id(db, ACCOUNT_COGS)]
    assert cogs_line.debit == Decimal("12.000000")  # 2 * 6.00
    inventory_line = lines_by_account[_account_id(db, ACCOUNT_INVENTORY)]
    assert inventory_line.credit == Decimal("12.000000")


def test_cash_sale_with_change_credits_cash_for_the_change_given(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("4.000000")
    )
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("50.00"))],
    )
    db.commit()
    assert sale.change_due == Decimal("40.00")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = _lines_for(db, entry)
    cash_account_id = _account_id(db, ACCOUNT_CASH_ON_HAND)
    cash_lines = [line for line in lines if line.account_id == cash_account_id]
    # One debit line (amount tendered) and one credit line (change given).
    assert sorted((line.debit, line.credit) for line in cash_lines) == [
        (Decimal("0.000000"), Decimal("40.000000")),
        (Decimal("50.000000"), Decimal("0.000000")),
    ]


def test_split_tender_sale_posts_one_debit_line_per_payment_method(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("5.000000")
    )
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[
            PaymentInput(payment_method="CASH", amount=Decimal("10.00")),
            PaymentInput(payment_method="CARD", amount=Decimal("20.00")),
        ],
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_CASH_ON_HAND)].debit == Decimal("10.000000")
    from app.modules.accounting.constants import ACCOUNT_CARD_CLEARING

    assert lines[_account_id(db, ACCOUNT_CARD_CLEARING)].debit == Decimal("20.000000")


def test_sale_with_discount_and_tax_posts_gross_revenue_discount_and_tax_payable(
    db: Session,
) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    tax_rate = make_tax_rate(db, rate_percent=Decimal("10.000"))
    product = make_product(
        db,
        store,
        current_price=Decimal("100.00"),
        current_cost=Decimal("50.000000"),
        current_qty_on_hand=Decimal("10"),
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
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("100.00"))],
    )
    db.commit()
    # subtotal=100, discount=10, taxable=90, tax=9, grand_total=99
    assert sale.subtotal == Decimal("100.00")
    assert sale.tax_total == Decimal("9.00")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_SALES_REVENUE)].credit == Decimal("100.000000")
    assert lines[_account_id(db, ACCOUNT_SALES_DISCOUNTS)].debit == Decimal("10.000000")
    assert lines[_account_id(db, ACCOUNT_TAX_PAYABLE)].credit == Decimal("9.000000")


def test_zero_price_sale_posts_no_sales_revenue_line_but_still_balances(db: Session) -> None:
    """A free item (price 0) must not try to credit Sales Revenue for
    zero, which would violate the exactly-one-side-positive line
    constraint — the entry must still balance using only the payment/
    change lines."""
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("0.00"), cost=Decimal("0.000000")
    )
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("5.00"))],
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    account_ids = {line.account_id for line in _lines_for(db, entry)}
    assert _account_id(db, ACCOUNT_SALES_REVENUE) not in account_ids
    assert _account_id(db, ACCOUNT_COGS) not in account_ids


# --- Purchasing accounting ---------------------------------------------------


def _po_with_item(db: Session, store, supplier, *, quantity, unit_cost, product=None):
    product = product or make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=quantity,
        unit_cost=unit_cost,
    )
    db.add(item)
    db.commit()
    return po, item, product


def test_goods_receipt_posts_inventory_debit_and_purchase_clearing_credit(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    po, item, product = _po_with_item(
        db, store, supplier, quantity=Decimal("10"), unit_cost=Decimal("4.500000")
    )

    receipt = purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("4.500000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_RECEIPT", JournalEntry.source_id == receipt.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_INVENTORY)].debit == Decimal("45.000000")
    assert lines[_account_id(db, ACCOUNT_PURCHASE_CLEARING)].credit == Decimal("45.000000")


def test_free_goods_receipt_posts_no_journal_entry(db: Session) -> None:
    """A supplier giving stock away for free (unit_cost 0) is a real M3
    scenario — the receipt must succeed with no accounting consequence,
    not raise or block the operational transaction."""
    store = make_store(db)
    supplier = make_supplier(db)
    po, item, product = _po_with_item(
        db, store, supplier, quantity=Decimal("5"), unit_cost=Decimal("0")
    )
    receipt = purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("5"), Decimal("0"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    count = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_RECEIPT", JournalEntry.source_id == receipt.id
            )
        )
        .scalars()
        .all()
    )
    assert count == []


def test_purchase_return_posts_purchase_clearing_debit_and_inventory_credit_at_current_wac(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    po, item, product = _po_with_item(
        db, store, supplier, quantity=Decimal("10"), unit_cost=Decimal("2.000000")
    )
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("2.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    assert product.current_cost == Decimal("2.000000")

    purchase_return = purchasing_service.create_purchase_return(
        db,
        purchase_order_id=po.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[PurchaseReturnLineInput(product_id=product.id, quantity=Decimal("3"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "PURCHASE_RETURN",
            JournalEntry.source_id == purchase_return.id,
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_PURCHASE_CLEARING)].debit == Decimal("6.000000")  # 3*2.00
    assert lines[_account_id(db, ACCOUNT_INVENTORY)].credit == Decimal("6.000000")

    db.refresh(product)
    assert product.current_cost == Decimal("2.000000")  # WAC unchanged by a removal


# --- Stock adjustment accounting ---------------------------------------------


def test_positive_stock_adjustment_posts_inventory_debit_and_gain_credit(db: Session) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(
        db, store, current_cost=Decimal("3.000000"), current_qty_on_hand=Decimal("10")
    )
    db.commit()

    adjustment = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("5"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="found extra stock",
        created_by=user.id,
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "STOCK_ADJUSTMENT", JournalEntry.source_id == adjustment.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_INVENTORY)].debit == Decimal("15.000000")  # 5*3.00
    assert lines[_account_id(db, ACCOUNT_INVENTORY_ADJUSTMENT_GAIN)].credit == Decimal("15.000000")


def test_negative_stock_adjustment_posts_shrinkage_expense_debit_and_inventory_credit(
    db: Session,
) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(
        db, store, current_cost=Decimal("3.000000"), current_qty_on_hand=Decimal("10")
    )
    db.commit()

    adjustment = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-4"),
        reason_code="DAMAGE",
        notes="damaged goods",
        created_by=user.id,
    )
    db.commit()

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "STOCK_ADJUSTMENT", JournalEntry.source_id == adjustment.id
        )
    ).scalar_one()
    _assert_balanced(db, entry)
    lines = {line.account_id: line for line in _lines_for(db, entry)}
    assert lines[_account_id(db, ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE)].debit == Decimal("12.000000")
    assert lines[_account_id(db, ACCOUNT_INVENTORY)].credit == Decimal("12.000000")


def test_zero_cost_stock_adjustment_succeeds_operationally_with_no_journal_entry(
    db: Session,
) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(db, store, current_cost=Decimal("0"), current_qty_on_hand=Decimal("10"))
    db.commit()

    adjustment = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-2"),
        reason_code="DAMAGE",
        notes="zero cost product",
        created_by=user.id,
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("8.000")  # the operation still happened

    entries = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "STOCK_ADJUSTMENT",
                JournalEntry.source_id == adjustment.id,
            )
        )
        .scalars()
        .all()
    )
    assert entries == []


# --- COGS immutability under WAC change --------------------------------------


def test_sale_cogs_journal_amount_is_immutable_after_a_later_wac_change(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    supplier = make_supplier(db)
    product = make_product(
        db,
        store,
        current_price=Decimal("20.00"),
        current_cost=Decimal("5.000000"),
        current_qty_on_hand=Decimal("100"),
    )
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("40.00"))],
    )
    db.commit()

    sale_entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()
    cogs_line_before = next(
        line
        for line in _lines_for(db, sale_entry)
        if line.account_id == _account_id(db, ACCOUNT_COGS)
    )
    assert cogs_line_before.debit == Decimal("10.000000")  # 2 * 5.00

    # Now change the product's WAC via a receipt at a very different cost.
    po, item, _ = _po_with_item(
        db,
        store,
        supplier,
        quantity=Decimal("50"),
        unit_cost=Decimal("500.000000"),
        product=product,
    )
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 2, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("500.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(product)
    assert product.current_cost != Decimal("5.000000")  # WAC really did change

    # The original sale's journal line must be untouched.
    cogs_line_after = next(
        line
        for line in _lines_for(db, sale_entry)
        if line.account_id == _account_id(db, ACCOUNT_COGS)
    )
    assert cogs_line_after.debit == Decimal("10.000000")


# --- Reversal ------------------------------------------------------------
#
# docs/M4_HARDENING_AUDIT.md Section 1 (CRITICAL, fixed): reversing an
# automatically-posted journal entry (SALE/PURCHASE_RECEIPT/
# PURCHASE_RETURN/STOCK_ADJUSTMENT/SALE_RETURN) is refused — it would
# correct the accounting without undoing the operational transaction
# that produced it (inventory, payment, stock), silently diverging the
# two. Proven live against a running instance before the fix existed.
# The reversal *mechanism* itself is still real and tested — against a
# MANUAL entry (the one source_type no endpoint currently posts, added
# specifically so this mechanism has a legal, testable target — see
# accounting/models.py).


def _post_manual_entry(db: Session, *, store_id: int, debit_amount: Decimal) -> JournalEntry:
    from app.modules.accounting.service import _credit, _debit, _post_journal

    return _post_journal(
        db,
        store_id=store_id,
        posting_date=date(2024, 1, 1),
        source_type="MANUAL",
        source_id=None,
        memo="manual test entry",
        created_by=None,
        lines=[
            _debit(ACCOUNT_CASH_ON_HAND, debit_amount),
            _credit(ACCOUNT_SALES_REVENUE, debit_amount),
        ],
    )


def test_reversing_an_automated_sale_journal_is_refused(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("4.000000")
    )
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
    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
        )
    ).scalar_one()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=entry.id,
            reason="attempted reversal of an automated entry",
            reversed_by=cashier.id,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"

    # No reversal was created, and the original entry is untouched.
    reversal_count = len(
        list(
            db.execute(
                select(JournalEntry).where(JournalEntry.reversal_of_id == entry.id)
            ).scalars()
        )
    )
    assert reversal_count == 0


@pytest.mark.parametrize(
    "source_type", ["SALE", "PURCHASE_RECEIPT", "PURCHASE_RETURN", "STOCK_ADJUSTMENT"]
)
def test_reversing_any_automated_source_type_is_refused(db: Session, source_type: str) -> None:
    """Direct proof for every automated source_type, not just SALE —
    constructs the entry via _post_journal directly rather than running
    the full operational flow for each type, since the point under test
    is reverse_journal_entry's source_type check, not the posting logic
    (already covered elsewhere)."""
    from app.modules.accounting.service import _credit, _debit, _post_journal

    store = make_store(db)
    db.commit()
    entry = _post_journal(
        db,
        store_id=store.id,
        posting_date=date(2024, 1, 1),
        source_type=source_type,
        source_id=123456,
        memo="synthetic automated entry",
        created_by=None,
        lines=[
            _debit(ACCOUNT_CASH_ON_HAND, Decimal("5.00")),
            _credit(ACCOUNT_SALES_REVENUE, Decimal("5.00")),
        ],
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db, journal_entry_id=entry.id, reason="x", reversed_by=None, caller_store_id=None
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


def test_reverse_journal_entry_creates_an_opposite_balanced_entry(db: Session) -> None:
    store = make_store(db)
    db.commit()
    entry = _post_manual_entry(db, store_id=store.id, debit_amount=Decimal("10.00"))
    db.commit()
    original_lines = {line.account_id: (line.debit, line.credit) for line in _lines_for(db, entry)}

    reversal = accounting_service.reverse_journal_entry(
        db,
        journal_entry_id=entry.id,
        reason="test reversal",
        reversed_by=None,
        caller_store_id=None,
    )
    db.commit()

    assert reversal.entry_type == "REVERSAL"
    assert reversal.reversal_of_id == entry.id
    _assert_balanced(db, reversal)
    reversal_lines = {
        line.account_id: (line.debit, line.credit) for line in _lines_for(db, reversal)
    }
    assert reversal_lines == {
        acc: (credit, debit) for acc, (debit, credit) in original_lines.items()
    }


def test_reversing_an_already_reversed_entry_returns_the_same_reversal_idempotently(
    db: Session,
) -> None:
    store = make_store(db)
    db.commit()
    entry = _post_manual_entry(db, store_id=store.id, debit_amount=Decimal("10.00"))
    db.commit()

    first = accounting_service.reverse_journal_entry(
        db, journal_entry_id=entry.id, reason="r1", reversed_by=None, caller_store_id=None
    )
    db.commit()
    second = accounting_service.reverse_journal_entry(
        db, journal_entry_id=entry.id, reason="r2", reversed_by=None, caller_store_id=None
    )
    db.commit()
    assert first.id == second.id


def test_cannot_reverse_a_reversal_entry(db: Session) -> None:
    store = make_store(db)
    db.commit()
    entry = _post_manual_entry(db, store_id=store.id, debit_amount=Decimal("10.00"))
    db.commit()
    reversal = accounting_service.reverse_journal_entry(
        db, journal_entry_id=entry.id, reason="r1", reversed_by=None, caller_store_id=None
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=reversal.id,
            reason="r2",
            reversed_by=None,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "CANNOT_REVERSE_REVERSAL"


def test_reverse_journal_entry_not_found(db: Session) -> None:
    with pytest.raises(NotFoundError):
        accounting_service.reverse_journal_entry(
            db, journal_entry_id=999999999, reason="x", reversed_by=None, caller_store_id=None
        )


# --- Reports ---------------------------------------------------------------


def test_trial_balance_totals_debits_equal_totals_credits(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("4.000000")
    )
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
    )
    db.commit()

    rows = accounting_service.trial_balance(db, store_id=store.id)
    total_debit = sum((r.total_debit for r in rows), Decimal("0"))
    total_credit = sum((r.total_credit for r in rows), Decimal("0"))
    assert total_debit == total_credit
    assert total_debit > 0


def test_profit_and_loss_reflects_a_simple_sale(db: Session) -> None:
    store, cashier, product = _setup_sale_fixture(
        db, price=Decimal("10.00"), cost=Decimal("4.000000")
    )
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("30.00"))],
    )
    db.commit()

    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.net_sales == Decimal("30.00")
    assert pnl.cogs == Decimal("12.000000")
    assert pnl.gross_profit == Decimal("18.000000")
    assert pnl.net_income == Decimal("18.000000")


def test_inventory_reconciliation_matches_after_receipt_and_sale(db: Session) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    supplier = make_supplier(db)
    product = make_product(db, store, current_price=Decimal("15.00"))
    db.commit()

    po, item, _ = _po_with_item(
        db, store, supplier, quantity=Decimal("20"), unit_cost=Decimal("6.000000"), product=product
    )
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("20"), Decimal("6.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("5"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("75.00"))],
    )
    db.commit()

    rows = accounting_service.inventory_reconciliation(db, store_id=store.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.discrepancy == Decimal("0.000000")
    db.refresh(product)
    assert row.operational_valuation == (
        product.current_qty_on_hand * product.current_cost
    ).quantize(Decimal("0.000001"))
