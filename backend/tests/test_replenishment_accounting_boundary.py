"""M9 hardening pass, Phase 7: proves the strict accounting boundary
holds — no inventory, AP, expense, or cash/GL entry is ever created
merely because a recommendation exists, is approved, or is executed into
a DRAFT PO/transfer. Only the pre-existing M3 (goods receiving) and M8
(transfer shipment/receipt) operational workflows create accounting, at
their own already-hardened lifecycle points — never M9."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.modules.accounting.models import JournalEntry
from app.modules.replenishment import service as replenishment_service
from tests.factories import make_product, make_store, make_supplier, make_supplier_product


def _journal_count(db: Session) -> int:
    return db.query(JournalEntry).count()


def test_generation_creates_no_journal_entry(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    before = _journal_count(db)

    replenishment_service.generate_replenishment_plans(db, store_id=store.id)

    assert _journal_count(db) == before


def test_approval_creates_no_journal_entry(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    before = _journal_count(db)

    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)

    assert _journal_count(db) == before


def test_execution_into_draft_po_creates_no_journal_entry(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    before = _journal_count(db)

    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="acct-po"
    )

    assert executed.status == "EXECUTED"
    assert _journal_count(db) == before

    from app.modules.purchasing.models import PurchaseOrder

    po = db.get(PurchaseOrder, executed.generated_purchase_order_id)
    assert po.status == "DRAFT"


def test_execution_into_draft_transfer_creates_no_journal_entry(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    sku = "SKU-ACCT-TRANSFER"
    make_product(
        db, store_a, sku=sku, current_qty_on_hand=Decimal("50"), reorder_point=Decimal("10")
    )
    make_product(
        db, store_b, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    before = _journal_count(db)

    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="acct-transfer"
    )

    assert executed.status == "EXECUTED"
    assert _journal_count(db) == before

    from app.modules.transfers.models import InterStoreTransfer

    transfer = db.get(InterStoreTransfer, executed.generated_transfer_id)
    assert transfer.status == "DRAFT"


def test_only_submit_and_receive_create_accounting_downstream(db: Session) -> None:
    """The FULL downstream lifecycle still works and creates accounting
    at exactly the pre-existing M3 lifecycle points — proving M9 doesn't
    somehow prevent legitimate accounting either, only defers it to the
    correct point."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="acct-downstream"
    )
    db.commit()
    before_submit = _journal_count(db)

    from app.modules.purchasing import service as purchasing_service

    purchasing_service.submit_purchase_order(
        db, executed.generated_purchase_order_id, actor_id=None, caller_store_id=None
    )
    db.commit()
    # Submission alone (DRAFT -> ORDERED) also creates no accounting in
    # this codebase's M3 design — accounting begins at RECEIPT.
    assert _journal_count(db) == before_submit

    from app.modules.purchasing.models import PurchaseOrderItem

    item = (
        db.query(PurchaseOrderItem)
        .filter_by(purchase_order_id=executed.generated_purchase_order_id)
        .one()
    )
    purchasing_service.receive_goods(
        db,
        purchase_order_id=executed.generated_purchase_order_id,
        received_date=date.today(),
        client_transaction_id="acct-downstream-receive",
        caller_store_id=None,
        lines=[
            purchasing_service.GoodsReceiptLineInput(
                purchase_order_item_id=item.id,
                quantity_received=item.quantity_ordered,
                unit_cost=item.unit_cost,
            )
        ],
    )
    db.commit()

    # NOW, at the real M3 receiving event, accounting is created — proving
    # M9 didn't somehow block legitimate downstream accounting either.
    assert _journal_count(db) > before_submit


def test_failed_execution_leaves_no_journal_entry(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed M9 execution (forced failure inside the transaction) must
    never leave a journal entry behind, even transiently — re-proves
    Phase 2's failure-injection guarantee from the accounting angle
    specifically."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    before = _journal_count(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(replenishment_service, "_create_purchase_order_inner", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="acct-fail"
        )
    db.rollback()

    assert _journal_count(db) == before
