"""M5 Session G: end-to-end financial reconciliation, extending M4's
hardening-audit comprehensive-scenario style
(tests/test_accounting_hardening.py::test_comprehensive_reconciliation_scenario_store_a)
to the new SALE_RETURN path specifically.

Scenario (independently hand-derived expected values below):
1. Receive 10 @ 10.000000                          -> qty=10,  WAC=10.000000
2. Sale A: sell 3 @ price 25.00                     -> qty=7,  WAC=10.000000 (unchanged)
3. Return 1 unit from Sale A (restock)               -> qty=8,  WAC=10.000000
4. New Sale B: sell 2 @ price 25.00                  -> qty=6,  WAC=10.000000 (unchanged)
5. WAC change: receive 5 @ 20.000000                 -> qty=11, WAC=14.545455
6. Another return: return 1 unit from Sale B         -> qty=12, WAC=14.166667
   (restock, at Sale B's FROZEN cost of 10.000000 —
   not the now-current WAC of 14.545455)

This exercises exactly the sequence M5's task called out explicitly:
Sale -> Return -> New Sale -> WAC change -> Another Return, proving the
whole system (P&L, trial balance, GL-vs-operational reconciliation)
stays correct across a composite path, not just the isolated per-return
unit tests in test_sales_returns.py.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.inventory import service as inventory_service
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
    unique_suffix,
)


def test_sale_return_new_sale_wac_change_another_return_reconciles(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    cashier = make_user(db, store)
    product = make_product(db, store, current_price=Decimal("25.00"))
    db.commit()

    def _po_item(qty: Decimal, cost: Decimal) -> tuple:
        po = make_purchase_order(db, store, supplier)
        item = PurchaseOrderItem(
            purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
        )
        db.add(item)
        db.commit()
        return po, item

    # 1. Receive 10 @ 10.000000
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

    # 2. Sale A: sell 3
    sale_a = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("75.00"))],
    )
    db.commit()
    assert sale_a.items[0].unit_cost_at_sale == Decimal("10.000000")

    # 3. Return 1 unit from Sale A
    sales_service.create_sale_return(
        db,
        sale_id=sale_a.id,
        store_id=store.id,
        return_date=date(2024, 1, 1),
        lines=[SaleReturnLineInput(sale_item_id=sale_a.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("8.000")
    assert product.current_cost == Decimal("10.000000")

    # 4. New Sale B: sell 2, still at the un-moved WAC of 10.000000
    sale_b = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("50.00"))],
    )
    db.commit()
    assert sale_b.items[0].unit_cost_at_sale == Decimal("10.000000")

    # 5. WAC change: receive 5 @ 20.000000
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
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("11.000")
    assert product.current_cost == Decimal("14.545455")

    # 6. Another return: 1 unit from Sale B, AFTER the WAC moved. Must
    # blend at Sale B's own frozen cost (10.000000), not the current WAC.
    sale_return_b = sales_service.create_sale_return(
        db,
        sale_id=sale_b.id,
        store_id=store.id,
        return_date=date(2024, 1, 3),
        lines=[SaleReturnLineInput(sale_item_id=sale_b.items[0].id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("12.000")
    assert product.current_cost == Decimal("14.166667")  # blended at frozen 10.0, not 14.545455
    assert sale_return_b.items[0].unit_cost_refunded == Decimal("10.000000")

    # --- Sale status transitions -----------------------------------------
    db.refresh(sale_a)
    db.refresh(sale_b)
    assert sale_a.status == "PARTIALLY_REFUNDED"  # 1 of 3 returned
    assert sale_b.status == "PARTIALLY_REFUNDED"  # 1 of 2 returned

    # --- Ledger-level movement audit trail ---------------------------------
    return_movements = inventory_service.list_movements(
        db, product_id=product.id, movement_type="SALE_RETURN"
    )
    assert len(return_movements) == 2
    assert {m.quantity_delta for m in return_movements} == {Decimal("1.000")}

    # --- P&L: net_sales/COGS/gross_profit hand-derived -----------------
    # Sales Revenue: Cr 75.00 (A) + Cr 50.00 (B) - Dr 25.00 (return A) -
    #   Dr 25.00 (return B) = net credit 75.00. No discounts/tax in this
    #   scenario, so net_sales = 75.00.
    # COGS: Dr 30.00 (A, 3x10.00) + Dr 20.00 (B, 2x10.00) - Cr 10.00
    #   (return A, 1x10.00) - Cr 10.00 (return B, 1x10.00) = net debit
    #   30.00 — exactly the COGS of the 3 units that were never returned
    #   (2 retained from A + 1 retained from B == 3 units x 10.00).
    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.net_sales == Decimal("75.00")
    assert pnl.cogs == Decimal("30.00")
    assert pnl.gross_profit == Decimal("45.00")
    assert pnl.net_income == pnl.gross_profit + pnl.other_income - pnl.operating_expenses

    # --- Trial balance always balances --------------------------------
    rows = accounting_service.trial_balance(db, store_id=store.id)
    assert sum(r.total_debit for r in rows) == sum(r.total_credit for r in rows)

    # --- GL Inventory vs operational valuation: bounded WAC-rounding
    # drift only, same documented tolerance as M4's own comprehensive
    # scenario (docs/M4_HARDENING_AUDIT.md Section 10/24) — two
    # WAC-recompute events here (steps 5 and 6), so a slightly larger
    # (but still tiny) drift than M4's single-recompute scenario is
    # expected and hand-verified above (-0.000004), not a fresh bug.
    reconciliation = accounting_service.inventory_reconciliation(db, store_id=store.id)
    assert len(reconciliation) == 1
    row = reconciliation[0]
    operational_valuation = product.current_qty_on_hand * product.current_cost
    assert row.operational_valuation == operational_valuation.quantize(Decimal("0.000001"))
    assert row.gl_inventory_balance == Decimal("170.000000")
    assert row.discrepancy == Decimal("-0.000004")
    assert abs(row.discrepancy) <= Decimal("0.00001"), (
        f"discrepancy {row.discrepancy} exceeds the documented WAC-rounding bound — "
        "investigate as a real bug, do not raise the tolerance"
    )
