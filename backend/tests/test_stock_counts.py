"""M8 Section 1: stock count lifecycle, scope, snapshotting, recounts, and
drift-detection posting — domain-level (calling app.modules.inventory.service
directly), matching tests/test_purchasing.py's style for the equivalent M3
domain tests. See docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decisions
1-5" for the full design this proves."""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting.constants import (
    ACCOUNT_INVENTORY,
    ACCOUNT_INVENTORY_ADJUSTMENT_GAIN,
    ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.inventory import service as inventory_service
from app.modules.inventory.models import StockAdjustment
from tests.factories import make_category, make_product, make_store, make_user


def _create_and_open(
    db: Session, store, *, product_ids: list[int] | None = None, category_id: int | None = None
):
    count = inventory_service.create_stock_count(
        db,
        store_id=store.id,
        category_id=category_id,
        product_ids=product_ids,
        created_by=None,
        caller_store_id=None,
    )
    return inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=None)


def test_create_stock_count_scopes_by_category_and_explicit_products(db: Session) -> None:
    store = make_store(db)
    category = make_category(db)
    in_category = make_product(db, store, category_id=category.id)
    explicit_only = make_product(db, store)
    not_included = make_product(db, store)
    db.commit()

    count = inventory_service.create_stock_count(
        db,
        store_id=store.id,
        category_id=category.id,
        product_ids=[explicit_only.id],
        created_by=None,
        caller_store_id=None,
    )
    line_product_ids = {
        line.product_id for line in inventory_service.get_stock_count_lines(db, count.id)
    }
    assert line_product_ids == {in_category.id, explicit_only.id}
    assert not_included.id not in line_product_ids


def test_create_stock_count_requires_at_least_one_product(db: Session) -> None:
    store = make_store(db)
    db.commit()
    with pytest.raises(ValidationAppError) as exc_info:
        inventory_service.create_stock_count(
            db, store_id=store.id, created_by=None, caller_store_id=None
        )
    assert exc_info.value.error_code == "EMPTY_STOCK_COUNT_SCOPE"


def test_open_snapshots_expected_quantity_and_cost(db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_qty_on_hand=Decimal("40"), current_cost=Decimal("3.500000")
    )
    db.commit()

    count = _create_and_open(db, store, product_ids=[product.id])
    (line,) = inventory_service.get_stock_count_lines(db, count.id)
    assert line.expected_quantity == Decimal("40")
    assert line.expected_unit_cost == Decimal("3.500000")
    assert count.status == "OPEN"

    # Moving stock AFTER opening must not retroactively change the
    # snapshot — it is fixed at the instant of opening (Design Decision 2).
    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("5"),
        reason_code="OTHER",
        notes=None,
        created_by=None,
    )
    db.commit()
    (line,) = inventory_service.get_stock_count_lines(db, count.id)
    assert line.expected_quantity == Decimal("40")


def test_open_is_idempotent_and_rejects_non_draft(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])

    # Calling open again on an already-OPEN count is a no-op, not an error.
    again = inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=None)
    assert again.status == "OPEN"

    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    with pytest.raises(ConflictError) as exc_info:
        inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=None)
    assert exc_info.value.error_code == "INVALID_STOCK_COUNT_STATE"


def test_record_count_entry_first_time_and_recount(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])

    line = inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("8"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    assert line.counted_quantity == Decimal("8")
    assert line.recount_number == 1
    assert line.expected_quantity == Decimal("10")  # untouched by a first-time count

    # A genuine change to book stock happens between the first count and
    # the recount — the recount must re-snapshot from CURRENT reality.
    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("2"),
        reason_code="OTHER",
        notes=None,
        created_by=None,
    )
    db.commit()

    recount = inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("9"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    assert recount.counted_quantity == Decimal("9")
    assert recount.recount_number == 2
    assert recount.expected_quantity == Decimal("12")  # re-snapshotted


def test_record_count_entry_rejects_negative_quantity(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    with pytest.raises(ValidationAppError):
        inventory_service.record_count_entry(
            db,
            count.id,
            product_id=product.id,
            counted_quantity=Decimal("-1"),
            counted_by=counter.id,
            caller_store_id=None,
        )


def test_record_count_entry_rejects_product_out_of_scope(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    in_scope = make_product(db, store)
    out_of_scope = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[in_scope.id])
    with pytest.raises(NotFoundError):
        inventory_service.record_count_entry(
            db,
            count.id,
            product_id=out_of_scope.id,
            counted_quantity=Decimal("1"),
            counted_by=counter.id,
            caller_store_id=None,
        )


def test_full_lifecycle_zero_variance_posts_no_adjustment_and_no_journal(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store, current_qty_on_hand=Decimal("15"), current_cost=Decimal("2"))
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("15"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    posted = inventory_service.post_stock_count(
        db, count.id, actor_id=reviewer.id, caller_store_id=None
    )

    assert posted.status == "POSTED"
    assert posted.posted_by == reviewer.id
    adjustments = db.query(StockAdjustment).filter_by(stock_count_id=count.id).all()
    assert adjustments == []


def test_full_lifecycle_shrinkage_posts_adjustment_and_shrinkage_journal(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(
        db, store, current_qty_on_hand=Decimal("20"), current_cost=Decimal("4.000000")
    )
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("17"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("17")

    adjustment = db.query(StockAdjustment).filter_by(stock_count_id=count.id).one()
    assert adjustment.quantity_delta == Decimal("-3")
    assert adjustment.reason_code == "STOCKTAKE_CORRECTION"

    journal_entry = (
        db.query(JournalEntry)
        .filter_by(source_type="STOCK_ADJUSTMENT", source_id=adjustment.id)
        .one()
    )
    lines = db.query(JournalLine).filter_by(journal_entry_id=journal_entry.id).all()
    by_account = {db.get(Account, line.account_id).code: line for line in lines}
    assert by_account[ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE].debit == Decimal("12.000000")
    assert by_account[ACCOUNT_INVENTORY].credit == Decimal("12.000000")


def test_full_lifecycle_surplus_posts_adjustment_and_gain_journal(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(
        db, store, current_qty_on_hand=Decimal("5"), current_cost=Decimal("2.000000")
    )
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("8"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    adjustment = db.query(StockAdjustment).filter_by(stock_count_id=count.id).one()
    assert adjustment.quantity_delta == Decimal("3")
    journal_entry = (
        db.query(JournalEntry)
        .filter_by(source_type="STOCK_ADJUSTMENT", source_id=adjustment.id)
        .one()
    )
    lines = db.query(JournalLine).filter_by(journal_entry_id=journal_entry.id).all()
    by_account = {db.get(Account, line.account_id).code: line for line in lines}
    assert by_account[ACCOUNT_INVENTORY].debit == Decimal("6.000000")
    assert by_account[ACCOUNT_INVENTORY_ADJUSTMENT_GAIN].credit == Decimal("6.000000")


def test_post_refuses_and_names_drifted_lines_without_posting_anything(db: Session) -> None:
    """The mandatory drift-detection policy (Design Decision 5): if book
    quantity moved after the snapshot was taken, the WHOLE posting is
    refused — nothing partially applied — and the count stays REVIEWED,
    not silently reopened."""
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    stable = make_product(db, store, current_qty_on_hand=Decimal("10"), current_cost=Decimal("1"))
    drifted = make_product(db, store, current_qty_on_hand=Decimal("10"), current_cost=Decimal("1"))
    db.commit()
    count = _create_and_open(db, store, product_ids=[stable.id, drifted.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=stable.id,
        counted_quantity=Decimal("10"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=drifted.id,
        counted_quantity=Decimal("10"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    # Something else posts against `drifted` AFTER the snapshot/count.
    inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=drifted.id,
        quantity_delta=Decimal("-2"),
        reason_code="DAMAGE",
        notes=None,
        created_by=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    assert exc_info.value.error_code == "STOCK_COUNT_DRIFT_DETECTED"
    assert str(drifted.id) in str(exc_info.value)

    db.refresh(count)
    assert count.status == "REVIEWED"  # unchanged — never silently posted or reopened
    # Nothing was posted for EITHER line, including the non-drifted one.
    assert db.query(StockAdjustment).filter_by(stock_count_id=count.id).count() == 0


def test_reopen_for_recount_returns_to_counted_and_clears_review(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("0"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    reopened = inventory_service.reopen_stock_count_for_recount(
        db, count.id, actor_id=None, caller_store_id=None, reason="drift"
    )
    assert reopened.status == "COUNTED"
    assert reopened.reviewed_by is None
    assert reopened.reviewed_at is None


@pytest.mark.parametrize("status_reached", ["DRAFT", "OPEN", "COUNTED", "REVIEWED"])
def test_cancel_allowed_from_every_pre_posted_status(db: Session, status_reached: str) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    count = inventory_service.create_stock_count(
        db, store_id=store.id, product_ids=[product.id], created_by=None, caller_store_id=None
    )
    if status_reached in ("OPEN", "COUNTED", "REVIEWED"):
        count = inventory_service.open_stock_count(
            db, count.id, actor_id=None, caller_store_id=None
        )
    if status_reached in ("COUNTED", "REVIEWED"):
        inventory_service.record_count_entry(
            db,
            count.id,
            product_id=product.id,
            counted_quantity=Decimal("0"),
            counted_by=counter.id,
            caller_store_id=None,
        )
        count = inventory_service.mark_stock_count_counted(
            db, count.id, actor_id=None, caller_store_id=None
        )
    if status_reached == "REVIEWED":
        count = inventory_service.review_stock_count(
            db, count.id, actor_id=reviewer.id, caller_store_id=None
        )

    cancelled = inventory_service.cancel_stock_count(
        db, count.id, actor_id=None, caller_store_id=None, reason="test"
    )
    assert cancelled.status == "CANCELLED"


def test_cancel_rejected_once_posted(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("0"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    with pytest.raises(ConflictError) as exc_info:
        inventory_service.cancel_stock_count(
            db, count.id, actor_id=None, caller_store_id=None, reason="too late"
        )
    assert exc_info.value.error_code == "INVALID_STOCK_COUNT_STATE"


def test_post_is_idempotent_by_state(db: Session) -> None:
    store = make_store(db)
    counter = make_user(db, store)
    reviewer = make_user(db, store)
    product = make_product(db, store)
    db.commit()
    count = _create_and_open(db, store, product_ids=[product.id])
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=Decimal("0"),
        counted_by=counter.id,
        caller_store_id=None,
    )
    inventory_service.mark_stock_count_counted(db, count.id, actor_id=None, caller_store_id=None)
    inventory_service.review_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)
    inventory_service.post_stock_count(db, count.id, actor_id=reviewer.id, caller_store_id=None)

    adjustments_before = db.query(StockAdjustment).filter_by(stock_count_id=count.id).count()
    # Calling post again on an already-POSTED count must return unchanged,
    # never create a second round of adjustments.
    again = inventory_service.post_stock_count(
        db, count.id, actor_id=reviewer.id, caller_store_id=None
    )
    assert again.status == "POSTED"
    assert (
        db.query(StockAdjustment).filter_by(stock_count_id=count.id).count() == adjustments_before
    )


def test_store_scoped_caller_cannot_touch_other_store_count(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    product = make_product(db, store_a)
    db.commit()
    count = inventory_service.create_stock_count(
        db, store_id=store_a.id, product_ids=[product.id], created_by=None, caller_store_id=None
    )
    with pytest.raises(ForbiddenError):
        inventory_service.open_stock_count(db, count.id, actor_id=None, caller_store_id=store_b.id)
