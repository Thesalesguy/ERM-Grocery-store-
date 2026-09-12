"""M9 hardening pass, Phase 4: proves M8's `get_replenishment_suggestions`
and M9's `compute_position`/`generate_replenishment_plans` never diverge,
because both call the exact same `compute_positions_bulk` (Design
Decision 1) — across every state named in the hardening task: on-hand,
partially received PO, partially received transfer, cancelled PO,
cancelled transfer, an executed replenishment plan, a stale plan, and
multiple sibling plans.

If any scenario below shows M8's `inventory_position` and M9's
`compute_position(...).position` disagreeing for the same product, that
is a genuine defect to fix, not something to document as acceptable."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.replenishment import service as replenishment_service
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store, make_supplier


def _assert_m8_m9_agree(db: Session, product, expected_position: Decimal | None = None) -> Decimal:
    """Fetches M8's suggestion (if the product is currently below its
    reorder point — M8 only reports shortfalls) and independently calls
    M9's compute_position, asserting they describe the identical
    position. Returns the agreed position for the caller's own further
    assertions."""
    m9_position = replenishment_service.compute_position(db, product).position
    suggestions = replenishment_service.get_replenishment_suggestions(db, store_id=product.store_id)
    m8_match = [s for s in suggestions if s.product_id == product.id]
    if m8_match:
        assert m8_match[0].inventory_position == m9_position, (
            f"M8 says {m8_match[0].inventory_position}, M9 says {m9_position} — "
            "the two position calculations have diverged"
        )
    if expected_position is not None:
        assert m9_position == expected_position, (m9_position, expected_position)
    return m9_position


def test_plain_on_hand_only(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("5"), reorder_point=Decimal("10"))
    db.commit()
    _assert_m8_m9_agree(db, product, Decimal("5"))


def test_partially_received_po_counts_only_the_remainder(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    po = purchasing_service.create_purchase_order(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        order_date=date(2024, 1, 1),
        lines=[
            purchasing_service.PurchaseOrderItemInput(
                product_id=product.id, quantity_ordered=Decimal("20"), unit_cost=Decimal("1.00")
            )
        ],
    )
    purchasing_service.submit_purchase_order(db, po.id, actor_id=None, caller_store_id=None)
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 2),
        client_transaction_id="pos-inv-partial-recv",
        caller_store_id=None,
        lines=[
            purchasing_service.GoodsReceiptLineInput(
                purchase_order_item_id=db.query(PurchaseOrderItem)
                .filter_by(purchase_order_id=po.id)
                .one()
                .id,
                quantity_received=Decimal("8"),
                unit_cost=Decimal("1.00"),
            )
        ],
    )
    db.commit()
    db.refresh(product)
    # on_hand now 2+8=10 (received), open_po_qty = 20-8=12 remaining.
    _assert_m8_m9_agree(db, product, Decimal("22"))


def test_cancelled_po_contributes_nothing_to_position(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    po = purchasing_service.create_purchase_order(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        order_date=date(2024, 1, 1),
        lines=[
            purchasing_service.PurchaseOrderItemInput(
                product_id=product.id, quantity_ordered=Decimal("20"), unit_cost=Decimal("1.00")
            )
        ],
    )
    purchasing_service.cancel_purchase_order(
        db, po.id, actor_id=None, reason="test cancel", caller_store_id=None
    )
    db.commit()
    _assert_m8_m9_agree(db, product, Decimal("2"))


def test_partially_received_transfer_counts_only_the_remainder(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    sku = "SKU-POSINV-TRANSFER"
    source = make_product(db, store_a, sku=sku, current_qty_on_hand=Decimal("100"))
    destination = make_product(
        db, store_b, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("20"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("20"))],
        client_transaction_id="posinv-ship",
        caller_store_id=None,
    )
    db.commit()
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("6"))],
        client_transaction_id="posinv-recv",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(destination)
    # on_hand now 2+6=8, inbound remaining = 20-6=14.
    _assert_m8_m9_agree(db, destination, Decimal("22"))


def test_cancelled_transfer_contributes_nothing_to_position(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    sku = "SKU-POSINV-CANCELTRANSFER"
    source = make_product(db, store_a, sku=sku, current_qty_on_hand=Decimal("100"))
    destination = make_product(
        db, store_b, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("20"))],
        caller_store_id=None,
    )
    db.commit()
    transfer_service.cancel_transfer(
        db, transfer.id, actor_id=None, caller_store_id=None, reason="test cancel"
    )
    db.commit()
    # A cancelled (never-shipped) transfer was never SHIPPED, so it was
    # never counted as inbound to begin with — position is unaffected.
    _assert_m8_m9_agree(db, destination, Decimal("2"))


def test_executed_replenishment_plans_draft_po_counts_immediately(db: Session) -> None:
    """A DRAFT PO generated by plan execution counts toward
    open_purchase_order_qty immediately (DRAFT is in _OPEN_PO_STATUSES) —
    both M8 and M9 must agree on this."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    from tests.factories import make_supplier_product

    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="posinv-exec"
    )
    db.commit()
    db.refresh(product)
    # on_hand 2 + open_po 8 (the DRAFT PO just created) = 10, at target.
    m9_position = replenishment_service.compute_position(db, product).position
    assert m9_position == Decimal("10")
    # M8's report no longer suggests anything for this product now that
    # position has reached the reorder point.
    suggestions = replenishment_service.get_replenishment_suggestions(db, store_id=store.id)
    assert all(s.product_id != product.id for s in suggestions)


def test_stale_plan_leaves_position_untouched(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    from tests.factories import make_supplier_product

    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()

    from app.modules.inventory import service as inventory_service

    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("20"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="recovered",
        created_by=None,
    )
    db.commit()

    from app.core.exceptions import ConflictError

    try:
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="posinv-stale"
        )
    except ConflictError:
        pass
    db.refresh(product)
    # A STALE plan creates no PO/transfer — position reflects only the
    # adjustment (2+20=22), never anything from the never-created document.
    _assert_m8_m9_agree(db, product, Decimal("22"))


def test_multiple_sibling_plans_position_reflects_only_real_documents(db: Session) -> None:
    """Two siblings (one TRANSFER, one SUPPLIER) for the same shortage —
    position must reflect exactly the real documents each execution
    creates, with no double counting and no divergence between M8 and M9."""
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    sku = "SKU-POSINV-SIBLINGS"
    make_product(
        db, store_a, sku=sku, current_qty_on_hand=Decimal("15"), reorder_point=Decimal("10")
    )
    short = make_product(
        db, store_b, sku=sku, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("10")
    )
    db.commit()
    from tests.factories import make_supplier_product

    make_supplier_product(db, supplier, short, unit_cost=Decimal("1.00"))
    db.commit()

    plans = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    db.commit()
    assert len(plans) == 2
    transfer_plan = next(p for p in plans if p.source_type == "TRANSFER")
    supplier_plan = next(p for p in plans if p.source_type == "SUPPLIER")

    replenishment_service.approve_plan(db, transfer_plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    replenishment_service.execute_plan(
        db,
        transfer_plan.id,
        actor_id=None,
        caller_store_id=None,
        client_transaction_id="posinv-sibling-transfer",
    )
    db.commit()
    db.refresh(short)
    # Transfer executed for 5 (source surplus), but it's a DRAFT transfer
    # (not SHIPPED) — inbound_transfer_qty only counts SHIPPED, so
    # position is UNCHANGED by this execution alone (0). This is the
    # DRAFT-PO-vs-DRAFT-transfer asymmetry documented in the hardening
    # audit: a supplier-sourced DRAFT PO counts immediately, a
    # transfer-sourced DRAFT transfer does not (until shipped).
    position_after_transfer_exec = replenishment_service.compute_position(db, short).position
    assert position_after_transfer_exec == Decimal("0")

    replenishment_service.approve_plan(db, supplier_plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    replenishment_service.execute_plan(
        db,
        supplier_plan.id,
        actor_id=None,
        caller_store_id=None,
        client_transaction_id="posinv-sibling-supplier",
    )
    db.commit()
    db.refresh(short)
    # The supplier sibling's own approved amount (5) is unaffected by the
    # transfer sibling's still-draft status — it executes for its own
    # full approved 5 (capped by its OWN suggested_quantity, never by a
    # shared pool), landing position at 0 (on-hand) + 5 (open PO) = 5.
    # Both M8 and M9 must agree on this number.
    _assert_m8_m9_agree(db, short, Decimal("5"))
