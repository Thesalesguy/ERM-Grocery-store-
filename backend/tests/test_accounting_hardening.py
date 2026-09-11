"""M4 hardening audit: the required comprehensive end-to-end reconciliation
scenario (audit Section 24), atomic-rollback proofs (Section 16), and a
few targeted adversarial checks not already covered by test_accounting*.py.

See docs/M4_HARDENING_AUDIT.md for the full audit writeup, including the
one real (bounded, pre-existing, non-critical) discrepancy this file's
comprehensive scenario surfaces and explains.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import InventoryMovement, StockAdjustment
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput, PurchaseReturnLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
    unique_suffix,
)


def _run_scenario(db: Session, store, supplier, cashier) -> dict:
    """Audit Section 24's exact scenario:
    1. Receive 10 @ 10
    2. Sell 3
    3. Receive 5 @ 20
    4. Sell 4
    5. Stock adjustment +2
    6. Purchase return 1 unit
    """
    product = make_product(db, store, current_price=Decimal("25.00"))
    db.commit()

    def _po_item(qty, cost):
        po = make_purchase_order(db, store, supplier)
        item = PurchaseOrderItem(
            purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
        )
        db.add(item)
        db.commit()
        return po, item

    po1, item1 = _po_item(Decimal("10"), Decimal("10.000000"))
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po1.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item1.id, Decimal("10"), Decimal("10.000000"))],
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
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("75.00"))],
    )
    db.commit()

    po2, item2 = _po_item(Decimal("5"), Decimal("20.000000"))
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po2.id,
        received_date=date(2024, 1, 2),
        lines=[GoodsReceiptLineInput(item2.id, Decimal("5"), Decimal("20.000000"))],
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
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("4"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("100.00"))],
    )
    db.commit()

    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("2"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="found extra",
        created_by=cashier.id,
    )
    db.commit()

    purchasing_service.create_purchase_return(
        db,
        purchase_order_id=po2.id,
        store_id=store.id,
        return_date=date(2024, 1, 3),
        lines=[PurchaseReturnLineInput(product_id=product.id, quantity=Decimal("1"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    db.refresh(product)
    return {"product": product}


def test_comprehensive_reconciliation_scenario_store_a(db: Session) -> None:
    """Audit Section 24. Independently hand-derived expected values (see
    docs/M4_HARDENING_AUDIT.md for the full worked arithmetic):

    qty=9, WAC=14.166667, inventory value=127.500003 (qty x WAC)
    revenue=175.00, COGS=86.666668, gross profit=88.333332
    Purchase Clearing net credit=185.833333
    Inventory Adjustment Gain=28.333334

    GL Inventory balance independently computed as 127.499999 — NOT
    bit-identical to qty x WAC (127.500003). This is a real, bounded,
    pre-existing precision limitation (docs/M4_HARDENING_AUDIT.md
    Section 10/24): 170/12 (the WAC recompute at the second receipt)
    does not terminate at 6 decimal places, so the *stored, rounded*
    WAC (14.166667) is not bit-identical to the true average — the
    same ~5e-7-per-recompute drift M1's compute_new_wac docstring
    already documented and accepted for WAC storage itself, now
    visible in reconciliation once a scenario has more than one
    WAC-changing receipt. Asserting exact equality here would be
    dishonest; the assertion below bounds the drift instead.
    """
    store = make_store(db)
    supplier = make_supplier(db)
    cashier = make_user(db, store)
    result = _run_scenario(db, store, supplier, cashier)
    product = result["product"]

    assert product.current_qty_on_hand == Decimal("9.000")
    assert product.current_cost == Decimal("14.166667")

    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.net_sales == Decimal("175.00")
    assert pnl.cogs == Decimal("86.666668")
    assert pnl.gross_profit == Decimal("88.333332")
    assert pnl.other_income == Decimal("28.333334")  # Inventory Adjustment Gain
    assert pnl.net_income == pnl.gross_profit + pnl.other_income - pnl.operating_expenses

    rows = accounting_service.trial_balance(db, store_id=store.id)
    assert sum(r.total_debit for r in rows) == sum(r.total_credit for r in rows)

    from app.modules.accounting.constants import ACCOUNT_PURCHASE_CLEARING

    clearing_row = next(r for r in rows if r.account_code == ACCOUNT_PURCHASE_CLEARING)
    assert clearing_row.total_credit - clearing_row.total_debit == Decimal("185.833333")

    reconciliation = accounting_service.inventory_reconciliation(db, store_id=store.id)
    assert len(reconciliation) == 1
    row = reconciliation[0]
    operational_valuation = product.current_qty_on_hand * product.current_cost
    assert row.operational_valuation == operational_valuation.quantize(Decimal("0.000001"))
    # Bounded, documented WAC-rounding drift — not exact equality. The
    # theoretical bound is ~5e-7 per WAC recompute event; this scenario
    # has exactly one (the second receipt), so any drift here beyond a
    # few millionths of a currency unit would indicate a REAL bug, not
    # the known limitation.
    discrepancy = abs(row.discrepancy)
    assert discrepancy <= Decimal("0.00001"), (
        f"discrepancy {row.discrepancy} exceeds the documented WAC-rounding bound — "
        "investigate as a real bug, do not raise the tolerance"
    )
    assert discrepancy > 0  # proves this is the known rounding artifact, not a fluke zero


def test_comprehensive_reconciliation_scenario_store_isolation(db: Session) -> None:
    """Repeats the Section 24 scenario in Store B with different numbers
    and proves Store A's reports/reconciliation are completely
    unaffected — and vice versa."""
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    cashier_a = make_user(db, store_a)
    cashier_b = make_user(db, store_b)

    _run_scenario(db, store_a, supplier, cashier_a)

    # Store B: a different, simpler scenario (single receipt/sale) so its
    # numbers are trivially distinguishable from Store A's.
    product_b = make_product(db, store_b, current_price=Decimal("50.00"))
    db.commit()
    po_b = make_purchase_order(db, store_b, supplier)
    item_b = PurchaseOrderItem(
        purchase_order_id=po_b.id,
        product_id=product_b.id,
        quantity_ordered=Decimal("6"),
        unit_cost=Decimal("30.000000"),
    )
    db.add(item_b)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po_b.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item_b.id, Decimal("6"), Decimal("30.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store_b.id,
        cashier_id=cashier_b.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product_b.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("100.00"))],
    )
    db.commit()

    pnl_a = accounting_service.profit_and_loss(db, store_id=store_a.id)
    pnl_b = accounting_service.profit_and_loss(db, store_id=store_b.id)
    assert pnl_a.net_sales == Decimal("175.00")
    assert pnl_b.net_sales == Decimal("100.00")
    assert pnl_a.net_sales != pnl_b.net_sales

    recon_a = accounting_service.inventory_reconciliation(db, store_id=store_a.id)
    recon_b = accounting_service.inventory_reconciliation(db, store_id=store_b.id)
    assert len(recon_a) == 1 and recon_a[0].store_id == store_a.id
    assert len(recon_b) == 1 and recon_b[0].store_id == store_b.id

    # Consolidated (no store filter) sees both.
    recon_all = accounting_service.inventory_reconciliation(db)
    assert {r.store_id for r in recon_all} >= {store_a.id, store_b.id}


# --- Section 16: atomic rollback -------------------------------------------


def test_sale_journal_posting_failure_rolls_back_the_whole_sale(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure inside post_sale_journal (after Sale/SaleItems/
    Payments/InventoryMovements/audit row are all already flushed) and
    proves NONE of it survives — no Sale row, no inventory movement, no
    stock change, no journal, matching finalize_sale's own 'a failed sale
    creates nothing' invariant now extended through the accounting call."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    db.commit()
    original_qty = product.current_qty_on_hand

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside accounting posting")

    monkeypatch.setattr(accounting_service, "post_sale_journal", _boom)

    txn_id = f"txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=txn_id,
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
    db.rollback()

    assert (
        db.execute(select(Sale).where(Sale.client_transaction_id == txn_id)).scalar_one_or_none()
        is None
    )
    db.refresh(product)
    assert product.current_qty_on_hand == original_qty  # stock untouched
    movements = (
        db.execute(select(InventoryMovement).where(InventoryMovement.product_id == product.id))
        .scalars()
        .all()
    )
    assert movements == []
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE", JournalEntry.store_id == store.id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_goods_receipt_journal_posting_failure_rolls_back_the_whole_receipt(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("5.000000"),
    )
    db.add(item)
    db.commit()
    original_qty = product.current_qty_on_hand
    original_cost = product.current_cost

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside receipt accounting posting")

    monkeypatch.setattr(accounting_service, "post_goods_receipt_journal", _boom)

    txn_id = f"txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        purchasing_service.receive_goods(
            db,
            purchase_order_id=po.id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("5.000000"))],
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    db.refresh(product)
    db.refresh(item)
    assert product.current_qty_on_hand == original_qty
    assert product.current_cost == original_cost
    assert item.quantity_received == Decimal("0.000")
    movements = (
        db.execute(select(InventoryMovement).where(InventoryMovement.product_id == product.id))
        .scalars()
        .all()
    )
    assert movements == []
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_RECEIPT", JournalEntry.store_id == store.id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_stock_adjustment_journal_posting_failure_rolls_back_the_whole_adjustment(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    user = make_user(db, store)
    product = make_product(
        db, store, current_cost=Decimal("3.000000"), current_qty_on_hand=Decimal("10")
    )
    db.commit()
    original_qty = product.current_qty_on_hand

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside adjustment accounting posting")

    monkeypatch.setattr(accounting_service, "post_stock_adjustment_journal", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        inventory_service.create_stock_adjustment(
            db,
            store_id=store.id,
            product_id=product.id,
            quantity_delta=Decimal("5"),
            reason_code="STOCKTAKE_CORRECTION",
            notes="x",
            created_by=user.id,
        )
    db.rollback()

    db.refresh(product)
    assert product.current_qty_on_hand == original_qty
    adjustments = (
        db.execute(select(StockAdjustment).where(StockAdjustment.product_id == product.id))
        .scalars()
        .all()
    )
    assert adjustments == []
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "STOCK_ADJUSTMENT", JournalEntry.store_id == store.id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_sale_inventory_movement_failure_rolls_back_sale_and_leaves_no_journal(
    db: Session,
) -> None:
    """A real (not monkeypatched) failure mode: insufficient stock
    discovered mid-cart (second line short) must roll back the first
    line's already-flushed movement too — no partial sale, no journal
    for a sale that never completed."""
    store = make_store(db)
    cashier = make_user(db, store)
    product_ok = make_product(
        db,
        store,
        current_price=Decimal("5.00"),
        current_cost=Decimal("2.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    product_short = make_product(
        db,
        store,
        current_price=Decimal("5.00"),
        current_cost=Decimal("2.000000"),
        current_qty_on_hand=Decimal("1"),
        allow_negative_stock=False,
    )
    db.commit()

    with pytest.raises(ConflictError):
        sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{unique_suffix()}",
            caller_store_id=None,
            lines=[
                SaleLineInput(product_id=product_ok.id, quantity=Decimal("1")),
                SaleLineInput(product_id=product_short.id, quantity=Decimal("5")),  # only 1 on hand
            ],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("30.00"))],
        )
    db.rollback()

    db.refresh(product_ok)
    assert product_ok.current_qty_on_hand == Decimal("10.000")  # untouched
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE", JournalEntry.store_id == store.id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


# --- Idempotency reinforcement: operational + accounting layers agree ------


def test_operational_and_accounting_idempotency_reinforce_each_other(db: Session) -> None:
    """Two sequential retries (not just one) with the same
    client_transaction_id — proves the fast path is stable across
    repeated retries, not just a single retry, and that the accounting
    layer's own uniqueness constraint would catch it even if the fast
    path were ever bypassed (see test_accounting_idempotency.py's direct
    _post_journal test for that half)."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    db.commit()
    txn_id = f"txn-{unique_suffix()}"

    results = []
    for _ in range(3):
        sale = sales_service.finalize_sale(
            db,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=txn_id,
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
        db.commit()
        results.append(sale.id)

    assert len(set(results)) == 1  # all three calls returned the same sale
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE", JournalEntry.source_id == results[0]
            )
        )
        .scalars()
        .all()
    )
    assert len(journals) == 1
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("9.000")  # exactly one unit sold, not three
