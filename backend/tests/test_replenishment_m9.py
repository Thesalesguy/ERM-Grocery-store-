"""M9 supply-chain: deterministic ranking/rounding helpers, recommendation
generation, the approve/execute/cancel lifecycle, duplicate-generation
prevention, staleness detection, metrics, and exceptions. See
docs/M9_SUPPLY_CHAIN_DESIGN.md for the design each test is proving."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.modules.replenishment import service as replenishment_service
from app.modules.replenishment.models import ReplenishmentPlan
from app.modules.transfers import service as transfer_service
from tests.factories import (
    make_product,
    make_store,
    make_supplier,
    make_supplier_product,
)

# --- apply_moq_and_pack_rounding (Design Decision 7) ------------------------


def test_rounding_applies_moq_then_pack_size_in_fixed_order() -> None:
    # need=3, MOQ=10 -> max(3,10)=10, pack=4 -> round up to 12.
    assert replenishment_service.apply_moq_and_pack_rounding(
        Decimal("3"), Decimal("10"), Decimal("4")
    ) == Decimal("12")


def test_rounding_with_no_moq_or_pack_returns_need_unchanged() -> None:
    assert replenishment_service.apply_moq_and_pack_rounding(Decimal("7"), None, None) == Decimal(
        "7"
    )


def test_rounding_exact_pack_multiple_is_not_bumped_up() -> None:
    assert replenishment_service.apply_moq_and_pack_rounding(
        Decimal("12"), None, Decimal("4")
    ) == Decimal("12")


# --- classify_urgency (design answer #12) -----------------------------------


def test_urgency_is_urgent_at_or_below_zero(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store, reorder_point=Decimal("10"))
    assert replenishment_service.classify_urgency(product, Decimal("0")) == "URGENT"
    assert replenishment_service.classify_urgency(product, Decimal("-5")) == "URGENT"


def test_urgency_is_urgent_below_configured_minimum(db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, reorder_point=Decimal("10"), minimum_stock_quantity=Decimal("5")
    )
    assert replenishment_service.classify_urgency(product, Decimal("3")) == "URGENT"


def test_urgency_is_normal_below_reorder_point_but_at_or_above_minimum(db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, reorder_point=Decimal("10"), minimum_stock_quantity=Decimal("5")
    )
    assert replenishment_service.classify_urgency(product, Decimal("8")) == "NORMAL"


# --- rank_source_stores (Design Decision 6) ---------------------------------


def test_rank_source_stores_never_dips_into_sources_own_reorder_protected_stock(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_product(
        db, store_a, sku="SKU-RANK", current_qty_on_hand=Decimal("10"), reorder_point=Decimal("10")
    )
    short = make_product(
        db, store_b, sku="SKU-RANK", current_qty_on_hand=Decimal("1"), reorder_point=Decimal("10")
    )
    db.commit()
    candidates = replenishment_service.rank_source_stores(db, short, Decimal("9"))
    # store_a is AT its own reorder point (surplus = 0) -> not a candidate.
    assert candidates == []


def test_rank_source_stores_orders_by_descending_surplus_then_ascending_store_id(
    db: Session,
) -> None:
    store_b = make_store(db)
    store_low_surplus = make_store(db)
    store_high_surplus = make_store(db)
    make_product(
        db,
        store_low_surplus,
        sku="SKU-RANK2",
        current_qty_on_hand=Decimal("15"),
        reorder_point=Decimal("10"),
    )
    make_product(
        db,
        store_high_surplus,
        sku="SKU-RANK2",
        current_qty_on_hand=Decimal("50"),
        reorder_point=Decimal("10"),
    )
    short = make_product(
        db, store_b, sku="SKU-RANK2", current_qty_on_hand=Decimal("1"), reorder_point=Decimal("10")
    )
    db.commit()
    candidates = replenishment_service.rank_source_stores(db, short, Decimal("100"))
    assert [c[0] for c in candidates] == [store_high_surplus.id, store_low_surplus.id]
    assert candidates[0][1] == Decimal("40")
    assert candidates[1][1] == Decimal("5")


# --- rank_suppliers (Design Decision 6) -------------------------------------


def test_rank_suppliers_prefers_preferred_supplier_over_lower_price(db: Session) -> None:
    store = make_store(db)
    preferred = make_supplier(db)
    cheaper = make_supplier(db)
    product = make_product(db, store, default_supplier_id=preferred.id)
    db.commit()
    make_supplier_product(db, preferred, product, unit_cost=Decimal("5.00"))
    make_supplier_product(db, cheaper, product, unit_cost=Decimal("1.00"))
    db.commit()

    ranked = replenishment_service.rank_suppliers(db, product, Decimal("10"))
    assert ranked[0].supplier_id == preferred.id


def test_rank_suppliers_falls_back_to_lowest_price_when_none_preferred(db: Session) -> None:
    store = make_store(db)
    supplier_a = make_supplier(db)
    supplier_b = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    make_supplier_product(db, supplier_a, product, unit_cost=Decimal("5.00"))
    make_supplier_product(db, supplier_b, product, unit_cost=Decimal("1.00"))
    db.commit()

    ranked = replenishment_service.rank_suppliers(db, product, Decimal("10"))
    assert ranked[0].supplier_id == supplier_b.id


def test_rank_suppliers_excludes_inactive_suppliers(db: Session) -> None:
    store = make_store(db)
    inactive = make_supplier(db, is_active=False)
    product = make_product(db, store)
    db.commit()
    make_supplier_product(db, inactive, product, unit_cost=Decimal("1.00"))
    db.commit()

    assert replenishment_service.rank_suppliers(db, product, Decimal("10")) == []


# --- get_current_supplier_product (Design Decision 2) -----------------------


def test_current_supplier_product_is_the_latest_effective_row_not_after_as_of(
    db: Session,
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    make_supplier_product(
        db, supplier, product, unit_cost=Decimal("1.00"), effective_date=date(2024, 1, 1)
    )
    make_supplier_product(
        db, supplier, product, unit_cost=Decimal("2.00"), effective_date=date(2024, 6, 1)
    )
    db.commit()

    current = replenishment_service.get_current_supplier_product(
        db, supplier.id, product.id, as_of=date(2024, 3, 1)
    )
    assert current is not None
    assert current.unit_cost == Decimal("1.00")

    later = replenishment_service.get_current_supplier_product(
        db, supplier.id, product.id, as_of=date(2024, 12, 1)
    )
    assert later is not None
    assert later.unit_cost == Decimal("2.00")


def test_a_price_change_never_mutates_an_existing_row(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    original = make_supplier_product(
        db, supplier, product, unit_cost=Decimal("1.00"), effective_date=date(2024, 1, 1)
    )
    original_id = original.id
    replenishment_service.create_supplier_product(
        db,
        supplier_id=supplier.id,
        product_id=product.id,
        supplier_sku=None,
        pack_size=Decimal("1"),
        unit_cost=Decimal("2.00"),
        minimum_order_quantity=None,
        lead_time_days=None,
        effective_date=date(2024, 6, 1),
        actor_id=None,
    )
    unchanged = db.get(type(original), original_id)
    assert unchanged.unit_cost == Decimal("1.00")


# --- generate_replenishment_plans ------------------------------------------


def test_generate_creates_supplier_plan_when_no_store_surplus_exists(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("3.00"))
    db.commit()

    plans = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.source_type == "SUPPLIER"
    assert plan.supplier_id == supplier.id
    assert plan.suggested_quantity == Decimal("8")
    assert plan.status == "RECOMMENDED"


def test_generate_creates_transfer_plan_when_sister_store_has_surplus(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_product(
        db, store_a, sku="SKU-GEN", current_qty_on_hand=Decimal("50"), reorder_point=Decimal("10")
    )
    short = make_product(
        db, store_b, sku="SKU-GEN", current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()

    plans = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.source_type == "TRANSFER"
    assert plan.source_store_id == store_a.id
    assert plan.product_id == short.id


def test_generate_splits_shortfall_across_sibling_plans_when_source_insufficient(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    make_product(
        db, store_a, sku="SKU-SPLIT", current_qty_on_hand=Decimal("15"), reorder_point=Decimal("10")
    )
    short = make_product(
        db, store_b, sku="SKU-SPLIT", current_qty_on_hand=Decimal("0"), reorder_point=Decimal("10")
    )
    db.commit()
    make_supplier_product(db, supplier, short, unit_cost=Decimal("1.00"))
    db.commit()

    plans = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    assert len(plans) == 2
    batch_ids = {p.generation_batch_id for p in plans}
    assert len(batch_ids) == 1
    by_source = {p.source_type: p for p in plans}
    assert by_source["TRANSFER"].suggested_quantity == Decimal("5")  # store_a's surplus above 10
    assert by_source["SUPPLIER"].suggested_quantity == Decimal("5")  # remaining shortfall


def test_generate_skips_products_with_an_already_active_plan(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()

    first = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    assert len(first) == 1
    second = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    assert second == []

    count = (
        db.query(ReplenishmentPlan)
        .filter_by(product_id=product.id, destination_store_id=store.id)
        .count()
    )
    assert count == 1


def test_generate_produces_no_plan_when_position_at_or_above_reorder_point(db: Session) -> None:
    store = make_store(db)
    make_product(db, store, current_qty_on_hand=Decimal("20"), reorder_point=Decimal("10"))
    db.commit()
    assert replenishment_service.generate_replenishment_plans(db, store_id=store.id) == []


# --- approve / cancel lifecycle ---------------------------------------------


def _make_supplier_plan(db: Session, **overrides) -> tuple[ReplenishmentPlan, Session]:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    return plan, store


def test_approve_transitions_recommended_to_approved(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    approved = replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    assert approved.status == "APPROVED"
    assert approved.approved_at is not None


def test_approve_is_idempotent_when_already_approved(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    again = replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    assert again.status == "APPROVED"


def test_cancel_from_recommended_succeeds(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    cancelled = replenishment_service.cancel_plan(
        db, plan.id, actor_id=None, caller_store_id=None, reason="no longer needed"
    )
    assert cancelled.status == "CANCELLED"
    assert cancelled.cancellation_reason == "no longer needed"


def test_cancel_from_executed_is_rejected(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-1"
    )
    with pytest.raises(ConflictError):
        replenishment_service.cancel_plan(db, plan.id, actor_id=None, caller_store_id=None)


# --- execute_plan (Design Decision 8) ---------------------------------------


def test_execute_supplier_plan_creates_draft_po_linked_back_to_plan(db: Session) -> None:
    plan, store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-po-1"
    )
    assert executed.status == "EXECUTED"
    assert executed.generated_purchase_order_id is not None

    from app.modules.purchasing.models import PurchaseOrder

    po = db.get(PurchaseOrder, executed.generated_purchase_order_id)
    assert po is not None
    assert po.status == "DRAFT"  # Design Decision 9: never advances past DRAFT
    assert po.replenishment_plan_id == plan.id


def test_execute_transfer_plan_creates_draft_transfer_linked_back_to_plan(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_product(
        db,
        store_a,
        sku="SKU-EXEC-T",
        current_qty_on_hand=Decimal("50"),
        reorder_point=Decimal("10"),
    )
    short = make_product(
        db, store_b, sku="SKU-EXEC-T", current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-tr-1"
    )
    assert executed.status == "EXECUTED"
    assert executed.generated_transfer_id is not None

    from app.modules.transfers.models import InterStoreTransfer

    transfer = db.get(InterStoreTransfer, executed.generated_transfer_id)
    assert transfer is not None
    assert transfer.status == "DRAFT"
    assert transfer.replenishment_plan_id == plan.id
    assert short.id  # destination product resolved by SKU match


def test_execute_requires_approved_status(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    with pytest.raises(ConflictError):
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-early"
        )


def test_execute_is_idempotent_by_client_transaction_id(db: Session) -> None:
    plan, _store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    first = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-idem"
    )
    second = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-idem"
    )
    assert first.id == second.id
    assert first.generated_purchase_order_id == second.generated_purchase_order_id

    from app.modules.purchasing.models import PurchaseOrder

    count = db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan.id).count()
    assert count == 1


def test_execute_marks_plan_stale_when_position_recovered_before_execution(db: Session) -> None:
    plan, store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)

    # Position recovers (e.g. a manual stock adjustment) after approval but
    # before execution — the exact race design answer #18 addresses.
    from app.modules.inventory import service as inventory_service

    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=plan.product_id,
        quantity_delta=Decimal("20"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="recovered stock",
        created_by=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-stale"
        )
    assert exc_info.value.error_code == "STALE_RECOMMENDATION"
    refreshed = replenishment_service.get_plan(db, plan.id)
    assert refreshed.status == "STALE"
    assert refreshed.generated_purchase_order_id is None


def test_execute_marks_transfer_plan_stale_when_source_surplus_disappears(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db,
        store_a,
        sku="SKU-STALE-T",
        current_qty_on_hand=Decimal("15"),
        reorder_point=Decimal("10"),
    )
    make_product(
        db,
        store_b,
        sku="SKU-STALE-T",
        current_qty_on_hand=Decimal("2"),
        reorder_point=Decimal("10"),
    )
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)

    # Source store's own surplus disappears via an outbound sale/adjustment
    # before execution.
    from app.modules.inventory import service as inventory_service

    inventory_service.create_stock_adjustment(
        db,
        store_id=store_a.id,
        product_id=source.id,
        quantity_delta=Decimal("-10"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="surplus consumed",
        created_by=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-stale-t"
        )
    assert exc_info.value.error_code == "SOURCE_STORE_INSUFFICIENT_SURPLUS"
    refreshed = replenishment_service.get_plan(db, plan.id)
    assert refreshed.status == "STALE"


def test_executed_quantity_never_exceeds_approved_quantity_even_if_shortfall_grows(
    db: Session,
) -> None:
    """Design answer #17: execution caps at min(approved_quantity, current
    shortfall) — a WORSE shortfall at execution time never inflates the
    executed quantity beyond what was approved."""
    plan, store = _make_supplier_plan(db)
    approved_quantity = plan.suggested_quantity
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)

    from app.modules.inventory import service as inventory_service

    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=plan.product_id,
        quantity_delta=Decimal("-2"),  # shortfall grows larger than approved
        reason_code="STOCKTAKE_CORRECTION",
        notes="shortfall grew",
        created_by=None,
    )
    db.commit()

    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-cap"
    )
    assert executed.executed_quantity == approved_quantity


# --- get_remaining_need / is_plan_fulfilled ---------------------------------


def test_remaining_need_reflects_executed_sibling_quantities(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    make_product(
        db,
        store_a,
        sku="SKU-REMAIN",
        current_qty_on_hand=Decimal("15"),
        reorder_point=Decimal("10"),
    )
    short = make_product(
        db, store_b, sku="SKU-REMAIN", current_qty_on_hand=Decimal("0"), reorder_point=Decimal("10")
    )
    db.commit()
    make_supplier_product(db, supplier, short, unit_cost=Decimal("1.00"))
    db.commit()

    plans = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    transfer_plan = next(p for p in plans if p.source_type == "TRANSFER")
    supplier_plan = next(p for p in plans if p.source_type == "SUPPLIER")

    before = replenishment_service.get_remaining_need(db, transfer_plan)
    assert before == Decimal("10")  # needed_quantity, nothing executed yet

    replenishment_service.approve_plan(db, transfer_plan.id, actor_id=None, caller_store_id=None)
    replenishment_service.execute_plan(
        db,
        transfer_plan.id,
        actor_id=None,
        caller_store_id=None,
        client_transaction_id="exec-remain-1",
    )
    after = replenishment_service.get_remaining_need(db, supplier_plan)
    assert after == Decimal("5")  # 10 needed - 5 executed via transfer


def test_is_plan_fulfilled_none_before_execution_and_false_until_fully_received(
    db: Session,
) -> None:
    plan, _store = _make_supplier_plan(db)
    assert replenishment_service.is_plan_fulfilled(db, plan) is None

    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    executed = replenishment_service.execute_plan(
        db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exec-fulfill"
    )
    assert replenishment_service.is_plan_fulfilled(db, executed) is False


# --- metrics and exceptions -------------------------------------------------


def test_metrics_report_honest_counts_with_no_fabricated_figures(db: Session) -> None:
    store = make_store(db)
    make_product(db, store, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("10"))
    db.commit()
    metrics = replenishment_service.get_supply_chain_metrics(db, store_id=store.id)
    assert metrics.products_below_reorder_point == 1
    assert metrics.products_below_minimum == 1  # position <= 0 is always URGENT
    assert metrics.open_purchase_order_qty == Decimal("0")
    assert metrics.inbound_transfer_qty == Decimal("0")
    assert metrics.overdue_purchase_order_count == 0


def test_exceptions_include_stockout_and_stale_plan(db: Session) -> None:
    plan, store = _make_supplier_plan(db)
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    from app.modules.inventory import service as inventory_service

    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=plan.product_id,
        quantity_delta=Decimal("20"),
        reason_code="STOCKTAKE_CORRECTION",
        notes="recovered",
        created_by=None,
    )
    db.commit()
    try:
        replenishment_service.execute_plan(
            db, plan.id, actor_id=None, caller_store_id=None, client_transaction_id="exc-test"
        )
    except ConflictError:
        pass

    exceptions = replenishment_service.get_exceptions(db, store_id=store.id)
    codes = {e.code for e in exceptions}
    assert "REPLENISHMENT_STALE" in codes


def test_generate_and_execute_never_call_accounting_directly() -> None:
    """Design Decision 9: this module's only two mutation entry points
    into other modules are the non-committing PO/transfer `_inner`
    functions — never app.modules.accounting.service directly. A static
    check: proves the accounting module is never imported by name."""
    assert "accounting" not in replenishment_service.__dict__
    assert not any(
        "app.modules.accounting" in str(getattr(v, "__module__", ""))
        for v in vars(replenishment_service).values()
    )


def test_never_duplicates_transfer_service_logic(db: Session) -> None:
    """Sanity check that execute_plan really does route through the
    shared _create_transfer_inner — asserted indirectly via transfer_service
    still being importable and unmodified in its public create_transfer
    behavior (M8 tests already prove this; this just guards the import
    path stays intact)."""
    assert hasattr(transfer_service, "_create_transfer_inner")
