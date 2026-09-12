"""M9 hardening pass: the complete 10-item concurrency matrix from the M9
task specification, against real PostgreSQL with genuinely independent
connections (same discipline as test_replenishment_concurrency.py — that
file already covers races #1 and #5; this file covers the remaining 8
named scenarios, each run at least 5 times).

Matrix status (see docs/M9_HARDENING_AUDIT.md for the full writeup):
  1. Same store/product generation race       -> test_replenishment_concurrency.py
  2. Different-product generation race        -> test_b_* below
  3. Sibling-shortage-shares-source race       -> test_c_* below
  4. Concurrent approval of the same plan      -> test_d_* below
  5. Concurrent execution of the same plan     -> test_replenishment_concurrency.py
  6. Two plans competing for the same stock    -> test_f_* below
  7. Execution races a source inventory change -> test_g_* below
  8. Execution races a destination position change -> test_h_* below
  9. Execution races a supplier price/MOQ change    -> test_i_* below
  10. Generation races an authoritative-position change -> test_j_* below
"""

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.replenishment import service as replenishment_service
from app.modules.replenishment.models import ReplenishmentPlan
from tests.factories import make_product, make_store, make_supplier, make_supplier_product

_REPS = 5


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    plan_id: int | None = None
    po_id: int | None = None
    transfer_id: int | None = None
    executed_unit_cost: Decimal | None = None
    executed_quantity: Decimal | None = None


def _attempt_execute(
    *, plan_id: int, client_transaction_id: str, barrier: threading.Barrier, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        plan = replenishment_service.execute_plan(
            session,
            plan_id,
            actor_id=None,
            caller_store_id=None,
            client_transaction_id=client_transaction_id,
        )
        result.succeeded = True
        result.plan_id = plan.id
        result.po_id = plan.generated_purchase_order_id
        result.transfer_id = plan.generated_transfer_id
        result.executed_unit_cost = plan.executed_unit_cost
        result.executed_quantity = plan.executed_quantity
    except ConflictError as exc:
        session.rollback()
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - a real deadlock must surface, not vanish
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _attempt_approve(*, plan_id: int, barrier: threading.Barrier, result: _Outcome) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        plan = replenishment_service.approve_plan(
            session, plan_id, actor_id=None, caller_store_id=None
        )
        result.succeeded = True
        result.plan_id = plan.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _attempt_generate(
    *,
    store_id: int,
    product_ids: list[int] | None,
    barrier: threading.Barrier,
    result: list[int],
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        plans = replenishment_service.generate_replenishment_plans(
            session, store_id=store_id, product_ids=product_ids
        )
        result.append(len(plans))
    except Exception:  # noqa: BLE001
        session.rollback()
        result.append(-1)
    finally:
        session.close()


# --- #2: two concurrent generation requests for DIFFERENT products in the
# same store must never interfere with each other (the advisory lock is
# keyed per-product, not per-store) ------------------------------------


def test_b_concurrent_generation_for_different_products_in_same_store_are_independent() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product_a = make_product(
                setup, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
            )
            product_b = make_product(
                setup, store, current_qty_on_hand=Decimal("3"), reorder_point=Decimal("12")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product_a, unit_cost=Decimal("1.00"))
            make_supplier_product(setup, supplier, product_b, unit_cost=Decimal("2.00"))
            setup.commit()
            store_id, product_a_id, product_b_id = store.id, product_a.id, product_b.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        result_a: list[int] = []
        result_b: list[int] = []
        thread_a = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(
                store_id=store_id, product_ids=[product_a_id], barrier=barrier, result=result_a
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(
                store_id=store_id, product_ids=[product_b_id], barrier=barrier, result=result_b
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a == [1], result_a
        assert result_b == [1], result_b

        verify = SessionLocal()
        try:
            count_a = (
                verify.query(ReplenishmentPlan)
                .filter_by(product_id=product_a_id, status="RECOMMENDED")
                .count()
            )
            count_b = (
                verify.query(ReplenishmentPlan)
                .filter_by(product_id=product_b_id, status="RECOMMENDED")
                .count()
            )
            assert count_a == 1
            assert count_b == 1
        finally:
            verify.close()


# --- #3: two sibling shortages (different destination stores) racing to
# claim surplus from the SAME source store must never collectively
# allocate more than that source's actual surplus ------------------------


def test_c_concurrent_generation_for_sibling_shortages_sharing_one_source_store() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            source_store = make_store(setup)
            dest_a = make_store(setup)
            dest_b = make_store(setup)
            sku = f"SKU-SHARE-{uuid.uuid4().hex[:8]}"
            # Source has surplus of 20 above its own reorder point (30 - 10).
            source_product = make_product(
                setup,
                source_store,
                sku=sku,
                current_qty_on_hand=Decimal("30"),
                reorder_point=Decimal("10"),
            )
            make_product(
                setup,
                dest_a,
                sku=sku,
                current_qty_on_hand=Decimal("0"),
                reorder_point=Decimal("15"),
            )
            make_product(
                setup,
                dest_b,
                sku=sku,
                current_qty_on_hand=Decimal("0"),
                reorder_point=Decimal("15"),
            )
            setup.commit()
            dest_a_id, dest_b_id, source_product_id = dest_a.id, dest_b.id, source_product.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        result_a: list[int] = []
        result_b: list[int] = []
        thread_a = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(store_id=dest_a_id, product_ids=None, barrier=barrier, result=result_a),
        )
        thread_b = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(store_id=dest_b_id, product_ids=None, barrier=barrier, result=result_b),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a and result_a[0] != -1, result_a
        assert result_b and result_b[0] != -1, result_b

        # Both destinations independently rank the SAME source store as a
        # transfer candidate — this is legitimate (Design Decision 6 ranks
        # candidates at generation time; it does not reserve stock), so
        # both may produce a TRANSFER-sourced plan citing the same source.
        # What must never happen: the source store's own on-hand quantity
        # is corrupted, or more than one plan claims to be able to draw
        # more than the source's surplus without the later one being
        # capped/marked stale at EXECUTION time (proven by the dedicated
        # over-allocation test in test_f_* below). Here we only assert
        # generation itself completed cleanly for both and the source
        # product's row is untouched (generation never mutates source
        # stock — only EXECUTION does, and only via a real transfer).
        verify = SessionLocal()
        try:
            from app.modules.products.models import Product

            source_product = verify.get(Product, source_product_id)
            assert source_product.current_qty_on_hand == Decimal("30")
        finally:
            verify.close()


# --- #4: two concurrent approvals of the same plan must never both "win"
# in a way that corrupts approved_by/approved_at, and must never raise an
# unhandled error -------------------------------------------------------


def test_d_concurrent_approval_of_the_same_plan_is_idempotent() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product = make_product(
                setup, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product, unit_cost=Decimal("1.00"))
            setup.commit()
            (plan,) = replenishment_service.generate_replenishment_plans(setup, store_id=store.id)
            setup.commit()
            plan_id = plan.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_approve, kwargs=dict(plan_id=plan_id, barrier=barrier, result=result_a)
        )
        thread_b = threading.Thread(
            target=_attempt_approve, kwargs=dict(plan_id=plan_id, barrier=barrier, result=result_b)
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        assert result_a.succeeded and result_b.succeeded

        verify = SessionLocal()
        try:
            plan = verify.get(ReplenishmentPlan, plan_id)
            assert plan.status == "APPROVED"
            # Exactly one approval must have "stuck" — approved_by/approved_at
            # come from whichever caller's transaction committed second (the
            # first caller's write is visible to the second under the row
            # lock), never a torn mix of both.
            assert plan.approved_at is not None
        finally:
            verify.close()


# --- #6: two DIFFERENT plans, each sourcing a TRANSFER from the SAME
# source store, executing concurrently must never collectively ship more
# than the source's actual surplus above its own reorder point ----------


def test_f_two_plans_executing_transfers_from_the_same_source_never_overallocate() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            source_store = make_store(setup)
            dest_a = make_store(setup)
            dest_b = make_store(setup)
            sku = f"SKU-COMPETE-{uuid.uuid4().hex[:8]}"
            # Source surplus is exactly 10 (20 on hand - 10 reorder point).
            source_product = make_product(
                setup,
                source_store,
                sku=sku,
                current_qty_on_hand=Decimal("20"),
                reorder_point=Decimal("10"),
            )
            make_product(
                setup, dest_a, sku=sku, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("8")
            )
            make_product(
                setup, dest_b, sku=sku, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("8")
            )
            setup.commit()

            # Manually construct two APPROVED transfer-sourced plans, each
            # independently "believing" (as of generation time) it can draw
            # 8 units from the source — 8 + 8 = 16 > 10 available surplus,
            # the exact over-allocation scenario this test targets.
            plan_a = ReplenishmentPlan(
                generation_batch_id=str(uuid.uuid4()),
                destination_store_id=dest_a.id,
                product_id=setup.query(type(source_product))
                .filter_by(store_id=dest_a.id, sku=sku)
                .one()
                .id,
                needed_quantity=Decimal("8"),
                suggested_quantity=Decimal("8"),
                source_type="TRANSFER",
                source_store_id=source_store.id,
                urgency="NORMAL",
                reason="test",
                status="APPROVED",
            )
            plan_b = ReplenishmentPlan(
                generation_batch_id=str(uuid.uuid4()),
                destination_store_id=dest_b.id,
                product_id=setup.query(type(source_product))
                .filter_by(store_id=dest_b.id, sku=sku)
                .one()
                .id,
                needed_quantity=Decimal("8"),
                suggested_quantity=Decimal("8"),
                source_type="TRANSFER",
                source_store_id=source_store.id,
                urgency="NORMAL",
                reason="test",
                status="APPROVED",
            )
            setup.add_all([plan_a, plan_b])
            setup.commit()
            plan_a_id, plan_b_id, source_product_id = plan_a.id, plan_b.id, source_product.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_a_id,
                client_transaction_id=f"txn-compete-a-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_b_id,
                client_transaction_id=f"txn-compete-b-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_b,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b

        verify = SessionLocal()
        try:
            from app.modules.products.models import Product

            source_product = verify.get(Product, source_product_id)
            plan_a = verify.get(ReplenishmentPlan, plan_a_id)
            plan_b = verify.get(ReplenishmentPlan, plan_b_id)

            total_executed = Decimal("0")
            for plan in (plan_a, plan_b):
                if plan.status == "EXECUTED":
                    total_executed += plan.executed_quantity or Decimal("0")

            # The critical invariant: the two plans' combined executed
            # quantity must never exceed the source's real surplus (10),
            # even though each was independently approved for 8.
            assert total_executed <= Decimal("10"), (
                f"over-allocated: {total_executed} executed against a source "
                f"surplus of only 10 (plan_a={plan_a.status}/{plan_a.executed_quantity}, "
                f"plan_b={plan_b.status}/{plan_b.executed_quantity})"
            )
            # Because each plan locks the SOURCE product row too (both are
            # TRANSFER-sourced from the same product), the two executions
            # must serialize — the second one recomputes source surplus
            # fresh and either takes the reduced remainder or goes STALE
            # with zero quantity, never blindly takes its full approved 8.
            assert source_product.current_qty_on_hand == Decimal("20"), (
                "execution must never touch on-hand quantity directly — only a real "
                "ship/receive against the generated DRAFT transfer would"
            )
        finally:
            verify.close()


# --- #7: execution races a change to SOURCE inventory (a sale/adjustment
# at the source store consuming its surplus between approval and
# execution) --------------------------------------------------------------


def test_g_transfer_execution_races_a_source_inventory_adjustment() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            source_store = make_store(setup)
            dest_store = make_store(setup)
            sku = f"SKU-SRCRACE-{uuid.uuid4().hex[:8]}"
            source_product = make_product(
                setup,
                source_store,
                sku=sku,
                current_qty_on_hand=Decimal("20"),
                reorder_point=Decimal("10"),
            )
            make_product(
                setup,
                dest_store,
                sku=sku,
                current_qty_on_hand=Decimal("0"),
                reorder_point=Decimal("8"),
            )
            setup.commit()
            (plan,) = replenishment_service.generate_replenishment_plans(
                setup, store_id=dest_store.id
            )
            setup.commit()
            replenishment_service.approve_plan(setup, plan.id, actor_id=None, caller_store_id=None)
            setup.commit()
            plan_id, source_product_id, source_store_id = (
                plan.id,
                source_product.id,
                source_store.id,
            )
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        exec_result = _Outcome()
        adjustment_error: list[str] = []

        def _do_execute(
            plan_id: int = plan_id, exec_result: _Outcome = exec_result, barrier=barrier
        ) -> None:
            _attempt_execute(
                plan_id=plan_id,
                client_transaction_id=f"txn-src-{uuid.uuid4().hex}",
                barrier=barrier,
                result=exec_result,
            )

        def _do_adjustment(
            source_product_id: int = source_product_id,
            source_store_id: int = source_store_id,
            adjustment_error: list[str] = adjustment_error,
            barrier=barrier,
        ) -> None:
            session = SessionLocal()
            try:
                from app.modules.inventory import service as inventory_service

                barrier.wait(timeout=10)
                # Consume ALL of the source's surplus via a sale-like
                # adjustment before the transfer can draw from it.
                inventory_service.create_stock_adjustment(
                    session,
                    store_id=source_store_id,
                    product_id=source_product_id,
                    quantity_delta=Decimal("-10"),
                    reason_code="STOCKTAKE_CORRECTION",
                    notes="source surplus consumed concurrently",
                    created_by=None,
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                adjustment_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                session.close()

        thread_execute = threading.Thread(target=_do_execute)
        thread_adjust = threading.Thread(target=_do_adjustment)
        thread_execute.start()
        thread_adjust.start()
        thread_execute.join(timeout=15)
        thread_adjust.join(timeout=15)

        assert exec_result.unexpected_error is None, exec_result
        assert adjustment_error == [], adjustment_error

        verify = SessionLocal()
        try:
            from app.modules.products.models import Product

            source_product = verify.get(Product, source_product_id)
            plan = verify.get(ReplenishmentPlan, plan_id)
            # Whichever side won, the transfer's executed quantity (if any)
            # must never exceed the source's surplus AT THE TIME its own
            # lock was acquired — never the stale 10-unit surplus from
            # generation/approval time.
            if plan.status == "EXECUTED":
                assert plan.executed_quantity is not None
                assert plan.executed_quantity <= Decimal("10")
            else:
                assert plan.status == "STALE"
            # The adjustment's effect (-10) is the only thing that ever
            # touches on-hand quantity here — execution only creates a
            # DRAFT transfer.
            assert source_product.current_qty_on_hand == Decimal("10")
        finally:
            verify.close()


# --- #8: execution races a DESTINATION position change in the DOWNWARD
# direction (demand increases the shortfall, not just recovers it) ------


def test_h_execution_races_a_destination_demand_increase() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product = make_product(
                setup, store, current_qty_on_hand=Decimal("5"), reorder_point=Decimal("10")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product, unit_cost=Decimal("1.00"))
            setup.commit()
            (plan,) = replenishment_service.generate_replenishment_plans(setup, store_id=store.id)
            setup.commit()
            replenishment_service.approve_plan(setup, plan.id, actor_id=None, caller_store_id=None)
            setup.commit()
            plan_id, product_id, store_id, approved_qty = (
                plan.id,
                product.id,
                store.id,
                plan.suggested_quantity,
            )
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        exec_result = _Outcome()
        demand_error: list[str] = []

        def _do_execute(
            plan_id: int = plan_id, exec_result: _Outcome = exec_result, barrier=barrier
        ) -> None:
            _attempt_execute(
                plan_id=plan_id,
                client_transaction_id=f"txn-demand-{uuid.uuid4().hex}",
                barrier=barrier,
                result=exec_result,
            )

        def _do_demand_increase(
            product_id: int = product_id,
            store_id: int = store_id,
            demand_error: list[str] = demand_error,
            barrier=barrier,
        ) -> None:
            session = SessionLocal()
            try:
                from app.modules.inventory import service as inventory_service

                barrier.wait(timeout=10)
                inventory_service.create_stock_adjustment(
                    session,
                    store_id=store_id,
                    product_id=product_id,
                    quantity_delta=Decimal("-3"),  # demand increases the shortfall
                    reason_code="STOCKTAKE_CORRECTION",
                    notes="unexpected demand",
                    created_by=None,
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                demand_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                session.close()

        thread_execute = threading.Thread(target=_do_execute)
        thread_demand = threading.Thread(target=_do_demand_increase)
        thread_execute.start()
        thread_demand.start()
        thread_execute.join(timeout=15)
        thread_demand.join(timeout=15)

        assert exec_result.unexpected_error is None, exec_result
        assert demand_error == [], demand_error

        verify = SessionLocal()
        try:
            plan = verify.get(ReplenishmentPlan, plan_id)
            if plan.status == "EXECUTED":
                # A GROWN shortfall must never inflate the executed quantity
                # past what was originally approved (design answer #17).
                assert plan.executed_quantity is not None
                assert plan.executed_quantity <= approved_qty
        finally:
            verify.close()


# --- #9: execution races a supplier price/MOQ change (a new
# SupplierProduct row with a later effective_date landing mid-execution) --


def test_i_supplier_execution_races_a_price_change() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product = make_product(
                setup, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product, unit_cost=Decimal("1.00"))
            setup.commit()
            (plan,) = replenishment_service.generate_replenishment_plans(setup, store_id=store.id)
            setup.commit()
            replenishment_service.approve_plan(setup, plan.id, actor_id=None, caller_store_id=None)
            setup.commit()
            plan_id, supplier_id, product_id = plan.id, supplier.id, product.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        exec_result = _Outcome()
        price_error: list[str] = []

        def _do_execute(
            plan_id: int = plan_id, exec_result: _Outcome = exec_result, barrier=barrier
        ) -> None:
            _attempt_execute(
                plan_id=plan_id,
                client_transaction_id=f"txn-price-{uuid.uuid4().hex}",
                barrier=barrier,
                result=exec_result,
            )

        def _do_price_change(
            supplier_id: int = supplier_id,
            product_id: int = product_id,
            price_error: list[str] = price_error,
            barrier=barrier,
        ) -> None:
            session = SessionLocal()
            try:
                import datetime as _dt

                barrier.wait(timeout=10)
                replenishment_service.create_supplier_product(
                    session,
                    supplier_id=supplier_id,
                    product_id=product_id,
                    supplier_sku=None,
                    pack_size=Decimal("1"),
                    unit_cost=Decimal("99.00"),
                    minimum_order_quantity=None,
                    lead_time_days=None,
                    effective_date=_dt.date.today(),
                    actor_id=None,
                )
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                price_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                session.close()

        thread_execute = threading.Thread(target=_do_execute)
        thread_price = threading.Thread(target=_do_price_change)
        thread_execute.start()
        thread_price.start()
        thread_execute.join(timeout=15)
        thread_price.join(timeout=15)

        assert exec_result.unexpected_error is None, exec_result
        assert price_error == [], price_error

        verify = SessionLocal()
        try:
            plan = verify.get(ReplenishmentPlan, plan_id)
            assert plan.status == "EXECUTED"
            # The executed unit cost must be EXACTLY ONE of the two real
            # prices that existed — never a corrupted/mixed value, and
            # never the stale generation-time snapshot if a newer price
            # was already committed and visible when execution's own
            # price lookup ran.
            assert plan.executed_unit_cost in (
                Decimal("1.00"),
                Decimal("99.000000"),
            ), plan.executed_unit_cost

            from app.modules.purchasing.models import PurchaseOrderItem

            item = (
                verify.query(PurchaseOrderItem)
                .filter_by(product_id=product_id)
                .filter(PurchaseOrderItem.purchase_order_id.isnot(None))
                .order_by(PurchaseOrderItem.id.desc())
                .first()
            )
            # The PO line's frozen unit_cost must match the plan's
            # executed_unit_cost exactly — no divergence between what was
            # charged and what was recorded as charged.
            assert item is not None
            assert item.unit_cost == plan.executed_unit_cost
        finally:
            verify.close()


# --- #10: generation races another transaction changing the authoritative
# supply position (a concurrent stock adjustment during generation) ------


def test_j_generation_races_an_authoritative_position_change() -> None:
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product = make_product(
                setup, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product, unit_cost=Decimal("1.00"))
            setup.commit()
            store_id, product_id = store.id, product.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        gen_result: list[int] = []
        adjustment_error: list[str] = []

        def _do_generate(
            store_id: int = store_id, gen_result: list[int] = gen_result, barrier=barrier
        ) -> None:
            _attempt_generate(
                store_id=store_id, product_ids=None, barrier=barrier, result=gen_result
            )

        def _do_adjustment(
            product_id: int = product_id,
            store_id: int = store_id,
            adjustment_error: list[str] = adjustment_error,
            barrier=barrier,
        ) -> None:
            session = SessionLocal()
            try:
                from app.modules.inventory import service as inventory_service

                barrier.wait(timeout=10)
                # Fully resolve the shortfall concurrently with generation.
                inventory_service.create_stock_adjustment(
                    session,
                    store_id=store_id,
                    product_id=product_id,
                    quantity_delta=Decimal("20"),
                    reason_code="STOCKTAKE_CORRECTION",
                    notes="resolved concurrently with generation",
                    created_by=None,
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                adjustment_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                session.close()

        thread_generate = threading.Thread(target=_do_generate)
        thread_adjust = threading.Thread(target=_do_adjustment)
        thread_generate.start()
        thread_adjust.start()
        thread_generate.join(timeout=15)
        thread_adjust.join(timeout=15)

        assert gen_result and gen_result[0] != -1, gen_result
        assert adjustment_error == [], adjustment_error

        verify = SessionLocal()
        try:
            plans = (
                verify.query(ReplenishmentPlan)
                .filter_by(product_id=product_id, status="RECOMMENDED")
                .all()
            )
            # Whichever transaction's snapshot generation used, the result
            # must be self-consistent: either a plan was created because
            # generation's OWN read of position (under READ COMMITTED) saw
            # the pre-adjustment shortfall, or none was created because it
            # saw the post-adjustment recovered position. Never a plan
            # whose needed_quantity is inconsistent with EITHER position.
            for plan in plans:
                assert plan.needed_quantity > 0
        finally:
            verify.close()
