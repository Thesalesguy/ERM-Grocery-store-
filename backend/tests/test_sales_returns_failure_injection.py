"""M5 failure-injection tests: forced failures at multiple stages of
create_sale_return, each proving a complete rollback with no half-applied
return — same discipline and monkeypatch technique as
tests/test_accounting_hardening.py's Section 16 (atomic rollback), applied
to the return/void workflow specifically (not yet covered there since
SALE_RETURN post-dates that file).

Each test forces a RuntimeError at a different point in the sequence:
record-creation -> inventory/WAC update -> accounting posting -> audit
logging, and asserts that NOTHING from that attempt survives: no
SaleReturn row, no SaleReturnItem, no inventory movement, no WAC change,
no quantity_returned change, no journal entry. The `db` fixture's
savepoint gives us a clean rollback boundary to assert against, exactly
as in test_accounting_hardening.py.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.audit import service as audit_service
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import InventoryMovement
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale, SaleItem, SaleReturn, SaleReturnItem
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def _sell(db: Session, store, cashier, product, *, quantity: Decimal) -> Sale:
    price = product.current_price
    return sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=quantity)],
        payments=[PaymentInput(payment_method="CASH", amount=price * quantity)],
    )


def _assert_return_never_happened(
    db: Session, *, sale: Sale, sale_item_id: int, product_id: int, txn_id: str
) -> None:
    # Scoped by this test's own sale_item/product/sale — the `db` fixture
    # shares a real database with the concurrency tests (which commit for
    # real, not via savepoint), so an unscoped query would also see
    # unrelated rows from earlier test files.
    assert (
        db.execute(
            select(SaleReturn).where(SaleReturn.client_transaction_id == txn_id)
        ).scalar_one_or_none()
        is None
    )
    assert (
        db.execute(
            select(SaleReturnItem).where(SaleReturnItem.sale_item_id == sale_item_id)
        ).scalar_one_or_none()
        is None
    )
    db.refresh(sale)
    assert sale.status == "COMPLETED"  # never advanced to (PARTIALLY_)REFUNDED
    item = db.get(SaleItem, sale_item_id)
    assert item.quantity_returned == Decimal("0")
    movements = (
        db.execute(
            select(InventoryMovement).where(
                InventoryMovement.product_id == product_id,
                InventoryMovement.movement_type == "SALE_RETURN",
            )
        )
        .scalars()
        .all()
    )
    assert movements == []
    journals = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE_RETURN", JournalEntry.store_id == sale.store_id
            )
        )
        .scalars()
        .all()
    )
    assert journals == []


def test_failure_during_inventory_movement_rolls_back_the_whole_return(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure inside record_movement, AFTER the SaleReturn/
    SaleReturnItem rows are already flushed (create_sale_return flushes
    the SaleReturn row and logs SALE_RETURN_INITIATED before looping over
    lines and calling record_movement per restocked line) — proves the
    return record itself doesn't survive when the inventory side fails."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    sale_item = db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id)).scalar_one()
    original_qty = product.current_qty_on_hand
    original_cost = product.current_cost

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside inventory movement recording")

    monkeypatch.setattr(inventory_service, "record_movement", _boom)

    txn_id = f"ret-txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date.today(),
            lines=[SaleReturnLineInput(sale_item_id=sale_item.id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    _assert_return_never_happened(
        db, sale=sale, sale_item_id=sale_item.id, product_id=product.id, txn_id=txn_id
    )
    db.refresh(product)
    assert product.current_qty_on_hand == original_qty
    assert product.current_cost == original_cost


def test_failure_during_accounting_posting_rolls_back_the_whole_return(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure inside post_sale_return_journal, AFTER the
    SaleReturn/SaleReturnItem rows, the inventory movement, the WAC
    update, and quantity_returned are ALL already flushed — proves the
    accounting call is not a bolt-on afterthought: if it fails, the
    operational side it would have described must not survive either."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    sale_item = db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id)).scalar_one()
    original_qty = product.current_qty_on_hand
    original_cost = product.current_cost

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside return accounting posting")

    monkeypatch.setattr(accounting_service, "post_sale_return_journal", _boom)

    txn_id = f"ret-txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date.today(),
            lines=[SaleReturnLineInput(sale_item_id=sale_item.id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    _assert_return_never_happened(
        db, sale=sale, sale_item_id=sale_item.id, product_id=product.id, txn_id=txn_id
    )
    db.refresh(product)
    # The inventory movement/WAC change from the failed attempt must not
    # survive the rollback either — restored to pre-return state.
    assert product.current_qty_on_hand == original_qty
    assert product.current_cost == original_cost


def test_failure_during_audit_logging_rolls_back_the_whole_return(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure inside audit_service.log_event on its SECOND call
    within create_sale_return (SALE_RETURN_COMPLETED, which fires after
    inventory/WAC/quantity_returned/status are all applied but before
    accounting posts) — proves audit logging is not a side channel that
    can silently desync from the operational data if it breaks."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    sale_item = db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id)).scalar_one()
    original_qty = product.current_qty_on_hand
    original_cost = product.current_cost

    call_count = {"n": 0}
    real_log_event = audit_service.log_event

    def _boom_on_second_call(db_arg, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("forced failure inside audit logging")
        return real_log_event(db_arg, **kwargs)

    monkeypatch.setattr(audit_service, "log_event", _boom_on_second_call)

    txn_id = f"ret-txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date.today(),
            lines=[SaleReturnLineInput(sale_item_id=sale_item.id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    _assert_return_never_happened(
        db, sale=sale, sale_item_id=sale_item.id, product_id=product.id, txn_id=txn_id
    )
    db.refresh(product)
    assert product.current_qty_on_hand == original_qty
    assert product.current_cost == original_cost


def test_failure_during_void_rolls_back_the_whole_void(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee, exercised through void_sale (the thin wrapper)
    rather than create_sale_return directly — proves the wrapper doesn't
    bypass or weaken the atomicity of the underlying return."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("20")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("2"))
    db.commit()
    sale_item = db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id)).scalar_one()
    original_qty = product.current_qty_on_hand
    original_cost = product.current_cost

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside void's accounting posting")

    monkeypatch.setattr(accounting_service, "post_sale_return_journal", _boom)

    txn_id = f"void-txn-{unique_suffix()}"
    with pytest.raises(RuntimeError, match="forced failure"):
        sales_service.void_sale(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date.today(),
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    db.rollback()

    _assert_return_never_happened(
        db, sale=sale, sale_item_id=sale_item.id, product_id=product.id, txn_id=txn_id
    )
    db.refresh(product)
    assert product.current_qty_on_hand == original_qty
    assert product.current_cost == original_cost
