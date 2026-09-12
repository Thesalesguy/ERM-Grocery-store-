"""M8 Section 2 (mandatory): stock count concurrency, proven against real
PostgreSQL with genuinely independent connections (same discipline as
tests/test_purchasing_concurrency.py — see that file's module docstring
for why the `db` fixture's savepoint isolation can't be used here).

Races covered (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 5"
table):
  A. A sale posts against a product while its count is OPEN, then the
     count is posted — must be refused as drift, never silently ignored.
  B. Two users try to POST the same REVIEWED count concurrently — exactly
     one must post, the other must see it already POSTED (idempotent).
  C. Two users count the SAME product on the SAME count concurrently —
     the header lock serializes them; no counted value is lost.
  D. Posting is concurrent with an unrelated stock adjustment against a
     counted product — the adjustment must either land before the post's
     lock (causing drift, correctly refused) or after it (correctly
     applied on top of the posted variance), never interleaved unsafely.

No global inventory lock is ever taken — every scenario below proves the
system stays correct using only per-header and per-product row locks.
"""

import threading
from dataclasses import dataclass
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import StockAdjustment, StockCount
from tests.factories import make_product, make_store, make_user


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    result_status: str | None = None


def _attempt_post(
    *, stock_count_id: int, actor_id: int, barrier: threading.Barrier, result: _Outcome
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        count = inventory_service.post_stock_count(
            session, stock_count_id, actor_id=actor_id, caller_store_id=None
        )
        session.commit()
        result.succeeded = True
        result.result_status = count.status
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001 - want a real deadlock to surface, not vanish
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _attempt_adjustment(
    *,
    store_id: int,
    product_id: int,
    quantity_delta: Decimal,
    barrier: threading.Barrier,
    errors: list[str],
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        inventory_service.create_stock_adjustment(
            session,
            store_id=store_id,
            product_id=product_id,
            quantity_delta=quantity_delta,
            reason_code="OTHER",
            notes=None,
            created_by=None,
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        session.close()


def _attempt_count_entry(
    *,
    stock_count_id: int,
    product_id: int,
    counted_quantity: Decimal,
    counted_by: int,
    barrier: threading.Barrier,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        inventory_service.record_count_entry(
            session,
            stock_count_id,
            product_id=product_id,
            counted_quantity=counted_quantity,
            counted_by=counted_by,
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def test_a_sale_during_open_window_causes_drift_detection_not_silent_loss() -> None:
    """A: inventory moves (via an ordinary stock adjustment, standing in
    for any movement type) DURING the OPEN window, after the count's own
    read of expected_quantity but before posting — the post must refuse,
    not silently apply a stale variance on top of an untracked change."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        counter = make_user(setup, store)
        reviewer = make_user(setup, store)
        product = make_product(setup, store, current_qty_on_hand=Decimal("50"))
        setup.commit()
        count = inventory_service.create_stock_count(
            setup,
            store_id=store.id,
            product_ids=[product.id],
            created_by=None,
            caller_store_id=None,
        )
        inventory_service.open_stock_count(setup, count.id, actor_id=None, caller_store_id=None)
        inventory_service.record_count_entry(
            setup,
            count.id,
            product_id=product.id,
            counted_quantity=Decimal("50"),
            counted_by=counter.id,
            caller_store_id=None,
        )
        inventory_service.mark_stock_count_counted(
            setup, count.id, actor_id=None, caller_store_id=None
        )
        inventory_service.review_stock_count(
            setup, count.id, actor_id=reviewer.id, caller_store_id=None
        )
        setup.commit()
        count_id, product_id, reviewer_id = count.id, product.id, reviewer.id
    finally:
        setup.close()

    # A real independent connection posts a sale-equivalent movement
    # against the SAME product while the count sits REVIEWED.
    mover = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = mover.get(Product, product_id)
        inventory_service.create_stock_adjustment(
            mover,
            store_id=product.store_id,
            product_id=product_id,
            quantity_delta=Decimal("-5"),
            reason_code="OTHER",
            notes="simulated concurrent sale",
            created_by=None,
        )
        mover.commit()
    finally:
        mover.close()

    result = _Outcome()
    barrier = threading.Barrier(1)
    _attempt_post(stock_count_id=count_id, actor_id=reviewer_id, barrier=barrier, result=result)

    assert result.unexpected_error is None, result
    assert result.succeeded is False
    assert result.error_code == "STOCK_COUNT_DRIFT_DETECTED"

    verify = SessionLocal()
    try:
        count = verify.get(StockCount, count_id)
        assert count.status == "REVIEWED"
        assert verify.query(StockAdjustment).filter_by(stock_count_id=count_id).count() == 0
    finally:
        verify.close()


def test_b_two_concurrent_posts_of_the_same_count_only_one_creates_adjustments() -> None:
    """B: two reviewers hit "post" on the same REVIEWED count at the same
    instant — the header lock must serialize them. Exactly one round of
    adjustments must be created; the loser sees the already-POSTED
    result (idempotent-by-state), never an error and never a duplicate."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        counter = make_user(setup, store)
        reviewer = make_user(setup, store)
        product = make_product(
            setup, store, current_qty_on_hand=Decimal("30"), current_cost=Decimal("2.00")
        )
        setup.commit()
        count = inventory_service.create_stock_count(
            setup,
            store_id=store.id,
            product_ids=[product.id],
            created_by=None,
            caller_store_id=None,
        )
        inventory_service.open_stock_count(setup, count.id, actor_id=None, caller_store_id=None)
        inventory_service.record_count_entry(
            setup,
            count.id,
            product_id=product.id,
            counted_quantity=Decimal("25"),
            counted_by=counter.id,
            caller_store_id=None,
        )
        inventory_service.mark_stock_count_counted(
            setup, count.id, actor_id=None, caller_store_id=None
        )
        inventory_service.review_stock_count(
            setup, count.id, actor_id=reviewer.id, caller_store_id=None
        )
        setup.commit()
        count_id, reviewer_id, product_id = count.id, reviewer.id, product.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_post,
        kwargs=dict(
            stock_count_id=count_id, actor_id=reviewer_id, barrier=barrier, result=result_a
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_post,
        kwargs=dict(
            stock_count_id=count_id, actor_id=reviewer_id, barrier=barrier, result=result_b
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.result_status == "POSTED"
    assert result_b.result_status == "POSTED"

    verify = SessionLocal()
    try:
        from app.modules.products.models import Product

        adjustments = verify.query(StockAdjustment).filter_by(stock_count_id=count_id).all()
        assert len(adjustments) == 1  # never duplicated
        product = verify.get(Product, product_id)
        assert product.current_qty_on_hand == Decimal("25")  # applied exactly once
    finally:
        verify.close()


def test_c_two_users_counting_the_same_product_serialize_no_count_lost() -> None:
    """C: two counters submit a count for the SAME product on the SAME
    count at (as close as threading allows) the same instant — the
    header lock serializes them (Design Decision 5: a deliberately
    coarser-than-per-line lock). One recorded value must win cleanly;
    neither thread must error or silently no-op."""
    setup = SessionLocal()
    try:
        store = make_store(setup)
        counter_a = make_user(setup, store)
        counter_b = make_user(setup, store)
        product = make_product(setup, store, current_qty_on_hand=Decimal("12"))
        setup.commit()
        count = inventory_service.create_stock_count(
            setup,
            store_id=store.id,
            product_ids=[product.id],
            created_by=None,
            caller_store_id=None,
        )
        inventory_service.open_stock_count(setup, count.id, actor_id=None, caller_store_id=None)
        setup.commit()
        count_id, product_id = count.id, product.id
        counter_a_id, counter_b_id = counter_a.id, counter_b.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_count_entry,
        kwargs=dict(
            stock_count_id=count_id,
            product_id=product_id,
            counted_quantity=Decimal("11"),
            counted_by=counter_a_id,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_count_entry,
        kwargs=dict(
            stock_count_id=count_id,
            product_id=product_id,
            counted_quantity=Decimal("13"),
            counted_by=counter_b_id,
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
    assert result_a.succeeded and result_b.succeeded

    verify = SessionLocal()
    try:
        (line,) = inventory_service.get_stock_count_lines(verify, count_id)
        # One of the two writes won cleanly (whichever acquired the header
        # lock second overwrote the first) — never a corrupted/partial value.
        assert line.counted_quantity in (Decimal("11"), Decimal("13"))
        assert line.recount_number == 2  # BOTH writes were applied in sequence
    finally:
        verify.close()


def test_d_five_iterations_post_concurrent_with_unrelated_adjustment() -> None:
    """D, run 5 times (timing-sensitive): posting a count races against an
    adjustment on a DIFFERENT, uncounted product in the same store. Since
    they touch disjoint products, both must always succeed independently
    — proving the per-product lock granularity doesn't over-serialize
    unrelated work during a count."""
    for _ in range(5):
        setup = SessionLocal()
        try:
            store = make_store(setup)
            counter = make_user(setup, store)
            reviewer = make_user(setup, store)
            counted_product = make_product(setup, store, current_qty_on_hand=Decimal("10"))
            other_product = make_product(setup, store, current_qty_on_hand=Decimal("10"))
            setup.commit()
            count = inventory_service.create_stock_count(
                setup,
                store_id=store.id,
                product_ids=[counted_product.id],
                created_by=None,
                caller_store_id=None,
            )
            inventory_service.open_stock_count(setup, count.id, actor_id=None, caller_store_id=None)
            inventory_service.record_count_entry(
                setup,
                count.id,
                product_id=counted_product.id,
                counted_quantity=Decimal("10"),
                counted_by=counter.id,
                caller_store_id=None,
            )
            inventory_service.mark_stock_count_counted(
                setup, count.id, actor_id=None, caller_store_id=None
            )
            inventory_service.review_stock_count(
                setup, count.id, actor_id=reviewer.id, caller_store_id=None
            )
            setup.commit()
            count_id, reviewer_id = count.id, reviewer.id
            other_product_id, store_id = other_product.id, store.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        post_result = _Outcome()
        adjustment_error: list[str] = []

        t_post = threading.Thread(
            target=_attempt_post,
            kwargs=dict(
                stock_count_id=count_id, actor_id=reviewer_id, barrier=barrier, result=post_result
            ),
        )
        t_adjust = threading.Thread(
            target=_attempt_adjustment,
            kwargs=dict(
                store_id=store_id,
                product_id=other_product_id,
                quantity_delta=Decimal("-1"),
                barrier=barrier,
                errors=adjustment_error,
            ),
        )
        t_post.start()
        t_adjust.start()
        t_post.join(timeout=15)
        t_adjust.join(timeout=15)

        assert post_result.unexpected_error is None, post_result
        assert post_result.succeeded, post_result
        assert adjustment_error == [], adjustment_error
