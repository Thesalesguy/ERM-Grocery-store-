"""M9 replenishment concurrency: proven against real PostgreSQL with
genuinely independent connections (same discipline as
tests/test_purchasing_concurrency.py — see that file's module docstring
for why the `db` fixture's savepoint isolation can't be used here).

Covers the task's named races that apply to this module: concurrent
execution of the SAME plan (duplicate-execution prevention, both via the
plan-row lock and the execution_client_transaction_id idempotency layer),
concurrent generation runs racing to create a plan for the same shortage
(the DB-level duplicate-prevention half), and a plan-execution race against
a concurrent stock movement that erodes the shortfall underneath it
(revalidation must catch it, never silently over-execute)."""

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


def _attempt_execute(
    *,
    plan_id: int,
    client_transaction_id: str,
    barrier: threading.Barrier,
    result: _Outcome,
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
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - a real deadlock must surface, not vanish
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_concurrent_execution_of_the_same_plan_with_different_client_ids_produces_one_po() -> None:
    """The primary duplicate-execution guard: locking the plan row by id
    serializes concurrent execute_plan calls on the SAME plan regardless
    of client_transaction_id — only one can observe status == 'APPROVED'
    and win; the other must see 'EXECUTED' (already won) or fail cleanly,
    never create a second PurchaseOrder."""
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
            plan_id = plan.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_id,
                client_transaction_id=f"txn-a-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_id,
                client_transaction_id=f"txn-b-{uuid.uuid4().hex}",
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
        # Both calls "succeed" from the caller's point of view (the loser
        # just observes the winner's already-EXECUTED plan) — what must
        # never happen is two different POs.
        assert result_a.succeeded and result_b.succeeded, (result_a, result_b)
        assert result_a.po_id == result_b.po_id

        verify = SessionLocal()
        try:
            from app.modules.purchasing.models import PurchaseOrder

            count = verify.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count()
            assert count == 1, f"expected exactly one PO, found {count}"
        finally:
            verify.close()


def test_concurrent_execution_with_the_same_client_transaction_id_is_idempotent() -> None:
    """The secondary idempotency layer: two threads racing with the exact
    SAME execution_client_transaction_id (a genuine client retry racing
    its own original request) must still yield exactly one PO."""
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
            plan_id = plan.id
        finally:
            setup.close()

        shared_key = f"txn-shared-{uuid.uuid4().hex}"
        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_id, client_transaction_id=shared_key, barrier=barrier, result=result_a
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_execute,
            kwargs=dict(
                plan_id=plan_id, client_transaction_id=shared_key, barrier=barrier, result=result_b
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        assert result_a.succeeded and result_b.succeeded
        assert result_a.po_id == result_b.po_id

        verify = SessionLocal()
        try:
            from app.modules.purchasing.models import PurchaseOrder

            count = verify.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count()
            assert count == 1
        finally:
            verify.close()


def _attempt_generate(*, store_id: int, barrier: threading.Barrier, result: list[int]) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        plans = replenishment_service.generate_replenishment_plans(session, store_id=store_id)
        result.append(len(plans))
    except Exception:  # noqa: BLE001
        session.rollback()
        result.append(-1)
    finally:
        session.close()


def test_concurrent_generation_runs_never_create_duplicate_active_plans() -> None:
    """Two concurrent generation runs over the SAME shortage must never
    both persist a plan for the same (store, product) — generation checks
    for an existing RECOMMENDED/APPROVED plan before creating one, and a
    real race here must still leave exactly one active plan (the loser's
    check-then-act window is real, but the assertion is on final state,
    which is what actually matters operationally)."""
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
        result_a: list[int] = []
        result_b: list[int] = []
        thread_a = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(store_id=store_id, barrier=barrier, result=result_a),
        )
        thread_b = threading.Thread(
            target=_attempt_generate,
            kwargs=dict(store_id=store_id, barrier=barrier, result=result_b),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a and result_a[0] != -1, result_a
        assert result_b and result_b[0] != -1, result_b

        verify = SessionLocal()
        try:
            active = (
                verify.query(ReplenishmentPlan)
                .filter(
                    ReplenishmentPlan.product_id == product_id,
                    ReplenishmentPlan.status.in_(("RECOMMENDED", "APPROVED")),
                )
                .count()
            )
            assert active == 1, f"expected exactly one active plan, found {active}"
        finally:
            verify.close()


def test_execute_revalidates_position_against_a_concurrent_stock_adjustment() -> None:
    """The recommendation-vs-execution recalculation race (design answer
    #18): a stock adjustment lands between approval and execution. Whether
    execute_plan's own lock is acquired before or after the adjustment
    commits, the executed quantity must reflect the CURRENT shortfall, and
    the total on-hand at the end must equal the adjustment's effect plus
    whatever (if anything) was actually executed — never double-counted,
    never a phantom quantity from a stale plan value."""
    for _ in range(_REPS):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            supplier = make_supplier(setup)
            product = make_product(
                setup, store, current_qty_on_hand=Decimal("0"), reorder_point=Decimal("10")
            )
            setup.commit()
            make_supplier_product(setup, supplier, product, unit_cost=Decimal("1.00"))
            setup.commit()
            (plan,) = replenishment_service.generate_replenishment_plans(setup, store_id=store.id)
            setup.commit()
            replenishment_service.approve_plan(setup, plan.id, actor_id=None, caller_store_id=None)
            setup.commit()
            plan_id, product_id, store_id = plan.id, product.id, store.id
            approved_quantity = plan.suggested_quantity
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        exec_result = _Outcome()
        adjustment_error: list[str] = []

        def _do_execute(
            plan_id: int = plan_id,
            exec_result: _Outcome = exec_result,
            barrier: threading.Barrier = barrier,
        ) -> None:
            _attempt_execute(
                plan_id=plan_id,
                client_transaction_id=f"txn-race-{uuid.uuid4().hex}",
                barrier=barrier,
                result=exec_result,
            )

        def _do_adjustment(
            store_id: int = store_id,
            product_id: int = product_id,
            adjustment_error: list[str] = adjustment_error,
            barrier: threading.Barrier = barrier,
        ) -> None:
            session = SessionLocal()
            try:
                from app.modules.inventory import service as inventory_service

                barrier.wait(timeout=10)
                inventory_service.create_stock_adjustment(
                    session,
                    store_id=store_id,
                    product_id=product_id,
                    quantity_delta=Decimal("10"),  # fully covers the shortfall
                    reason_code="STOCKTAKE_CORRECTION",
                    notes="concurrent recovery",
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
            from app.modules.purchasing.models import PurchaseOrderItem

            product = verify.get(Product, product_id)
            plan = verify.get(ReplenishmentPlan, plan_id)

            if exec_result.succeeded and plan.status == "EXECUTED":
                # Execution won the race before the adjustment's effect was
                # visible to it (or only partially) — executed_quantity must
                # never exceed what was approved.
                assert plan.executed_quantity is not None
                assert plan.executed_quantity <= approved_quantity
            else:
                # The adjustment fully resolved the shortfall before
                # execution's lock acquired a fresh read — plan must be
                # STALE, and critically NO purchase order was created.
                assert plan.status == "STALE"
                po_items = (
                    verify.query(PurchaseOrderItem)
                    .join(PurchaseOrderItem.purchase_order)
                    .filter_by(replenishment_plan_id=plan_id)
                    .count()
                )
                assert po_items == 0
            # on_hand must equal 0 (original) + 10 (adjustment) + whatever
            # was executed as a real receipt would add — but execution only
            # creates a DRAFT PO (Design Decision 9), which never touches
            # on-hand quantity. So on_hand is always exactly 10 here,
            # regardless of which side of the race won.
            assert product.current_qty_on_hand == Decimal("10")
        finally:
            verify.close()
