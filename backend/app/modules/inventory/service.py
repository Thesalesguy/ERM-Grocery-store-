"""Inventory ledger read/write helpers and the Weighted Average Cost formula.

This is intentionally small: docs/TECHNICAL_BLUEPRINT.md Section D and the
M1 task instructions ask for the data model and the WAC math to be proven
correct, not a full Inventory Service API (that's Milestone M2). Every
function here is used either directly by tests (to prove the formula) or
by app.modules.purchasing.service.receive_goods (to prove one real,
transactional, row-locked vertical slice through the ledger).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.audit import service as audit_service
from app.modules.auth.models import Store
from app.modules.inventory.models import (
    _CANCELLABLE_STOCK_COUNT_STATUSES,
    _COUNTABLE_STOCK_COUNT_STATUSES,
    InventoryMovement,
    StockAdjustment,
    StockCount,
    StockCountLine,
)
from app.modules.products.models import Product

_WAC_QUANTUM = Decimal("0.000001")  # matches Numeric(14, 6) storage precision


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Local duplicate of the same check every other service module makes
    — see e.g. app.modules.purchasing.service._enforce_store_access's
    docstring for why this is duplicated rather than imported."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access "
            f"{noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


def compute_new_wac(
    *,
    existing_qty: Decimal,
    existing_wac: Decimal,
    received_qty: Decimal,
    received_unit_cost: Decimal,
) -> Decimal:
    """docs/TECHNICAL_BLUEPRINT.md Section D:

        New WAC = (existing_qty * existing_wac + received_qty * received_unit_cost)
                  / (existing_qty + received_qty)

    Computed fresh from the two (qty, cost) pairs at full Decimal
    precision — never incrementally adjusted — so rounding only ever
    happens once, at the end, to the column's storage precision (6
    decimal places). This bounds any rounding drift to at most ~5e-7 per
    recompute, which is immaterial at currency scale and is the same
    precision/drift tradeoff established in docs/TECHNICAL_BLUEPRINT.md
    assumption #10.
    """
    total_qty = existing_qty + received_qty
    if total_qty == 0:
        # Reversing the last unit of stock exactly to zero — there is no
        # remaining inventory to have a cost basis, so there is nothing to
        # average. Fall back to the incoming cost rather than dividing by
        # zero; the next receipt starts a fresh average from qty=0.
        return received_unit_cost.quantize(_WAC_QUANTUM, rounding=ROUND_HALF_UP)

    total_value = (existing_qty * existing_wac) + (received_qty * received_unit_cost)
    return (total_value / total_qty).quantize(_WAC_QUANTUM, rounding=ROUND_HALF_UP)


def list_movements(
    db: Session,
    *,
    product_id: int | None = None,
    movement_type: str | None = None,
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[InventoryMovement]:
    """Read-only ledger listing. Movements are created by
    app.modules.purchasing.service.receive_goods, create_stock_adjustment
    (above), and app.modules.sales.service.finalize_sale — never directly
    by a route handler.
    """
    query = (
        select(InventoryMovement)
        .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if product_id is not None:
        query = query.where(InventoryMovement.product_id == product_id)
    if movement_type is not None:
        query = query.where(InventoryMovement.movement_type == movement_type)
    if store_id is not None:
        query = query.where(InventoryMovement.store_id == store_id)
    return list(db.execute(query).scalars().all())


def get_quantity_on_hand_from_ledger(db: Session, product_id: int) -> Decimal:
    """The authoritative on-hand quantity: SUM of every movement ever
    posted for this product. `products.current_qty_on_hand` is a cache of
    this value maintained transactionally alongside each movement — it
    must always equal this function's result (see
    tests/test_inventory.py::test_qty_on_hand_matches_ledger_sum).
    """
    total = db.execute(
        select(func.coalesce(func.sum(InventoryMovement.quantity_delta), 0)).where(
            InventoryMovement.product_id == product_id
        )
    ).scalar_one()
    return Decimal(total)


def lock_product_for_update(db: Session, product_id: int) -> Product:
    """Acquire a row-level lock on the product for the rest of the current
    transaction, so two concurrent movements against the same product
    serialize instead of racing and corrupting the cached quantity
    (docs/TECHNICAL_BLUEPRINT.md Section D, edge case 7).
    """
    return db.execute(
        select(Product).where(Product.id == product_id).with_for_update()
    ).scalar_one()


def record_movement(
    db: Session,
    *,
    product: Product,
    store_id: int,
    movement_type: str,
    quantity_delta: Decimal,
    unit_cost_at_movement: Decimal,
    reference_type: str,
    reference_id: int | None,
    reason: str | None = None,
    created_by: int | None = None,
    new_product_cost: Decimal | None = None,
) -> InventoryMovement:
    """Append one ledger row and update the product's cached qty/cost.

    `product` must already be locked FOR UPDATE by the caller within the
    current transaction (see lock_product_for_update) — this function
    does not lock or commit; the caller owns the transaction boundary, so
    a failure anywhere in a multi-line operation (e.g. goods receiving)
    rolls back every line, never just some of them (BR-3).

    Raises ConflictError (INSUFFICIENT_STOCK) if applying quantity_delta
    would take the product negative and the product does not have
    allow_negative_stock set (docs/TECHNICAL_BLUEPRINT.md BR-7, default
    safe policy).
    """
    new_qty = product.current_qty_on_hand + quantity_delta
    if new_qty < 0 and not product.allow_negative_stock:
        raise ConflictError(
            f"Insufficient stock for product {product.id}: "
            f"{product.current_qty_on_hand} on hand, requested change {quantity_delta}",
            error_code="INSUFFICIENT_STOCK",
        )

    product.current_qty_on_hand = new_qty
    if new_product_cost is not None:
        product.current_cost = new_product_cost

    movement = InventoryMovement(
        store_id=store_id,
        product_id=product.id,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        unit_cost_at_movement=unit_cost_at_movement,
        resulting_quantity_on_hand=new_qty,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        created_by=created_by,
    )
    db.add(movement)
    db.flush()
    return movement


def list_stock_levels(
    db: Session,
    *,
    store_id: int | None = None,
    search: str | None = None,
    low_stock_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Product]:
    query = select(Product).order_by(Product.name).limit(limit).offset(offset)
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Product.name.ilike(pattern), Product.sku.ilike(pattern)))
    if low_stock_only:
        query = query.where(
            Product.reorder_point.is_not(None),
            Product.current_qty_on_hand <= Product.reorder_point,
        )
    return list(db.execute(query).scalars().all())


def get_stock_level(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")
    return product


def create_stock_adjustment(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    quantity_delta: Decimal,
    reason_code: str,
    notes: str | None,
    created_by: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
    stock_count_id: int | None = None,
    client_transaction_id: str | None = None,
) -> StockAdjustment:
    """The full authenticated, transactional, audited stock-adjustment
    flow (M2 task Section 6): the API can never overwrite
    current_qty_on_hand directly — every change is this function creating
    one StockAdjustment (the human-facing record) plus exactly one
    InventoryMovement (the ledger entry), atomically, with the actor
    recorded on both and an audit_logs entry to match. Does not commit —
    the caller (the route handler, or post_stock_count below calling this
    once per variance line within its own larger transaction) does, once
    this returns successfully.

    `stock_count_id` (M8): set only when this adjustment is being created
    BY post_stock_count, tracing a STOCKTAKE_CORRECTION back to the count
    that produced it (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design
    Decision 1"). Callers already hold the product's row lock in that
    case (post_stock_count locks every affected product before creating
    any adjustment); lock_product_for_update below is a harmless re-lock
    of an already-locked row in that path, and the ONLY lock acquisition
    for the ordinary single-adjustment route-handler path.

    `client_transaction_id` (M15 pre-milestone hardening, docs/
    M15_DESIGN.md "Pre-M15 hardening"): optional, mirroring
    Sale.client_transaction_id's idempotency pattern exactly —
    finalize_sale's fast-path lookup, then an IntegrityError-recovery
    block after the flush below catches a genuinely concurrent duplicate.
    `post_stock_count` never passes one (each of its internal per-line
    calls has no natural per-call client key; that path's idempotency is
    already provided by StockCount's own status-based checks), so a
    caller that omits it gets exactly the pre-M15 behavior — no key, no
    fast path, no uniqueness check, unchanged from before this hardening.
    """
    if client_transaction_id is not None:
        existing = db.execute(
            select(StockAdjustment).where(
                StockAdjustment.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    adjustment = StockAdjustment(
        store_id=store_id,
        product_id=product_id,
        quantity_delta=quantity_delta,
        reason_code=reason_code,
        notes=notes,
        created_by=created_by,
        stock_count_id=stock_count_id,
        client_transaction_id=client_transaction_id,
    )
    db.add(adjustment)
    if client_transaction_id is not None:
        # Only take the rollback-and-recover path when a key was actually
        # supplied — post_stock_count's internal calls (client_transaction_id
        # always None) must keep flushing directly with no try/except
        # inserted around them, so an unrelated IntegrityError during a
        # multi-line count posting still propagates and rolls back the
        # WHOLE posting transaction exactly as it did before this
        # hardening, rather than this function swallowing it and rolling
        # back only its own partial work.
        try:
            db.flush()
        except IntegrityError:
            # A genuinely concurrent duplicate submission (two requests
            # with the same client_transaction_id racing before either
            # committed) — mirrors finalize_sale's identical recovery
            # block exactly.
            db.rollback()
            winner = db.execute(
                select(StockAdjustment).where(
                    StockAdjustment.client_transaction_id == client_transaction_id
                )
            ).scalar_one_or_none()
            if winner is None:
                raise
            return winner
    else:
        db.flush()

    product = lock_product_for_update(db, product_id)
    movement_type = "STOCK_ADJUSTMENT_IN" if quantity_delta > 0 else "STOCK_ADJUSTMENT_OUT"
    unit_cost_at_movement = product.current_cost
    record_movement(
        db,
        product=product,
        store_id=store_id,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        unit_cost_at_movement=unit_cost_at_movement,
        reference_type="stock_adjustment",
        reference_id=adjustment.id,
        reason=notes,
        created_by=created_by,
    )

    audit_service.log_event(
        db,
        user_id=created_by,
        action="STOCK_ADJUSTMENT_CREATED",
        entity_type="stock_adjustment",
        entity_id=adjustment.id,
        after={
            "product_id": product_id,
            "quantity_delta": quantity_delta,
            "reason_code": reason_code,
            "resulting_quantity_on_hand": product.current_qty_on_hand,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # Accounting posting shares this same uncommitted transaction — see
    # app.modules.accounting.service.post_stock_adjustment_journal's
    # docstring (docs/M4_ACCOUNTING_CORE.md Section 19).
    accounting_service.post_stock_adjustment_journal(
        db,
        stock_adjustment=adjustment,
        unit_cost=unit_cost_at_movement,
        created_by=created_by,
    )
    db.flush()
    return adjustment


# --- Stock counts / physical inventory (M8) ---------------------------------
#
# See docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decisions 1-5" for the
# full lifecycle/concurrency design. Posting reuses create_stock_adjustment
# above unchanged (called once per nonzero-variance line, inside THIS
# function's own transaction — create_stock_adjustment never commits, so
# every adjustment it creates for one count posts atomically together).


def _generate_count_number(store_id: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"SC{store_id}-{timestamp}"


@dataclass(frozen=True)
class StockCountDriftLine:
    stock_count_line_id: int
    product_id: int
    expected_quantity: Decimal
    current_quantity_on_hand: Decimal


def create_stock_count(
    db: Session,
    *,
    store_id: int,
    category_id: int | None = None,
    product_ids: list[int] | None = None,
    notes: str | None = None,
    created_by: int | None = None,
    caller_store_id: int | None = None,
) -> StockCount:
    """DRAFT: header + scope only. Scope is the UNION of `category_id`
    (every product currently in that category, in this store — including
    inactive ones, Design Decision 4) and any explicit `product_ids`
    given, expanded into StockCountLine rows NOW with `expected_quantity`
    left NULL until open_stock_count captures it. A product added to the
    category after this call is never swept in later — scope is fixed at
    DRAFT time by these rows' mere existence (Design Decision 1)."""
    _enforce_store_access(caller_store_id, store_id, "stock counts")
    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")

    resolved_ids: set[int] = set(product_ids or [])
    if category_id is not None:
        resolved_ids |= set(
            db.execute(
                select(Product.id).where(
                    Product.store_id == store_id, Product.category_id == category_id
                )
            ).scalars()
        )
    if not resolved_ids:
        raise ValidationAppError(
            "A stock count must include at least one product (via category_id and/or "
            "an explicit product list)",
            error_code="EMPTY_STOCK_COUNT_SCOPE",
        )

    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(resolved_ids))).scalars()
    }
    missing = resolved_ids - set(products)
    if missing:
        raise NotFoundError(f"Product(s) {sorted(missing)} not found")
    for product in products.values():
        if product.store_id != store_id:
            raise ConflictError(
                f"Product {product.id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )

    count = StockCount(
        store_id=store_id,
        count_number=_generate_count_number(store_id),
        status="DRAFT",
        category_id=category_id,
        notes=notes,
        created_by=created_by,
    )
    db.add(count)
    db.flush()
    for product_id in sorted(resolved_ids):
        db.add(StockCountLine(stock_count_id=count.id, product_id=product_id))

    audit_service.log_event(
        db,
        user_id=created_by,
        action="STOCK_COUNT_CREATED",
        entity_type="stock_count",
        entity_id=count.id,
        after={
            "store_id": store_id,
            "category_id": category_id,
            "line_count": len(resolved_ids),
        },
    )
    db.commit()
    db.refresh(count)
    return count


def get_stock_count(db: Session, stock_count_id: int) -> StockCount:
    count = db.get(StockCount, stock_count_id)
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    return count


def list_stock_counts(
    db: Session,
    *,
    store_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StockCount]:
    query = (
        select(StockCount)
        .order_by(StockCount.created_at.desc(), StockCount.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(StockCount.store_id == store_id)
    if status is not None:
        query = query.where(StockCount.status == status)
    return list(db.execute(query).scalars().all())


def get_stock_count_lines(db: Session, stock_count_id: int) -> list[StockCountLine]:
    return list(
        db.execute(
            select(StockCountLine)
            .where(StockCountLine.stock_count_id == stock_count_id)
            .order_by(StockCountLine.id)
        )
        .scalars()
        .all()
    )


def open_stock_count(
    db: Session, stock_count_id: int, *, actor_id: int | None, caller_store_id: int | None
) -> StockCount:
    """DRAFT -> OPEN: captures expected_quantity/expected_unit_cost per
    line, one product lock at a time in ascending product_id order (the
    deadlock-safe pattern used everywhere else in this codebase). THIS is
    the exact instant "expected quantity" means anything
    (docs/M8_ADVANCED_INVENTORY_DESIGN.md 'Design Decision 2'). Idempotent
    by state: calling this on an already-OPEN count is a no-op, mirroring
    every other status-transition function in this codebase."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status == "OPEN":
        return count
    if count.status != "DRAFT":
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status}, not DRAFT — only a draft "
            "stock count can be opened",
            error_code="INVALID_STOCK_COUNT_STATE",
        )

    lines = list(
        db.execute(
            select(StockCountLine)
            .where(StockCountLine.stock_count_id == stock_count_id)
            .order_by(StockCountLine.product_id)
        ).scalars()
    )
    if not lines:
        raise ConflictError(
            f"Stock count {stock_count_id} has no lines to open",
            error_code="EMPTY_STOCK_COUNT_SCOPE",
        )

    for line in lines:
        product = lock_product_for_update(db, line.product_id)
        line.expected_quantity = product.current_qty_on_hand
        line.expected_unit_cost = product.current_cost

    count.status = "OPEN"
    count.opened_by = actor_id
    count.opened_at = datetime.now(UTC)

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_OPENED",
        entity_type="stock_count",
        entity_id=count.id,
        after={"line_count": len(lines)},
    )
    db.commit()
    db.refresh(count)
    return count


def record_count_entry(
    db: Session,
    stock_count_id: int,
    *,
    product_id: int,
    counted_quantity: Decimal,
    counted_by: int,
    caller_store_id: int | None,
) -> StockCountLine:
    """Records — or, if the line already has a counted_quantity,
    RECOUNTS (Design Decision 3) — one line's physical count.

    Locks the StockCount header FIRST, serializing against review/post/
    cancel transitions and against a concurrent count of a DIFFERENT
    product on the SAME count. This is a deliberately coarser lock than
    per-product: a stock count is a low-frequency, human-paced document,
    not a high-throughput path like POS sales, so correctness here
    matters far more than intra-count parallelism (docs/
    M8_ADVANCED_INVENTORY_DESIGN.md 'Design Decision 5' table, "two users
    counting the same product" row).

    A RECOUNT also re-snapshots expected_quantity/expected_unit_cost from
    the product's CURRENT values under a fresh lock — this is what makes
    a recount the correct resolution for posting-time drift (Design
    Decision 3): the comparison basis moves forward with reality instead
    of staying silently stale. A first-time count entry does not touch
    expected_quantity (already correctly set by open_stock_count) and
    therefore takes no product lock at all — keeping the common case
    lock-free."""
    if counted_quantity < 0:
        raise ValidationAppError(
            "Counted quantity cannot be negative", error_code="INVALID_QUANTITY"
        )

    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status not in _COUNTABLE_STOCK_COUNT_STATUSES:
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status} and cannot accept a count entry "
            "(must be OPEN or COUNTED)",
            error_code="INVALID_STOCK_COUNT_STATE",
        )

    line = db.execute(
        select(StockCountLine).where(
            StockCountLine.stock_count_id == stock_count_id,
            StockCountLine.product_id == product_id,
        )
    ).scalar_one_or_none()
    if line is None:
        raise NotFoundError(
            f"Product {product_id} is not in scope for stock count {stock_count_id}"
        )

    is_recount = line.counted_quantity is not None
    before = {
        "counted_quantity": line.counted_quantity,
        "expected_quantity": line.expected_quantity,
        "recount_number": line.recount_number,
    }
    if is_recount:
        product = lock_product_for_update(db, product_id)
        line.expected_quantity = product.current_qty_on_hand
        line.expected_unit_cost = product.current_cost

    line.counted_quantity = counted_quantity
    line.counted_by = counted_by
    line.counted_at = datetime.now(UTC)
    line.recount_number += 1

    audit_service.log_event(
        db,
        user_id=counted_by,
        action="STOCK_COUNT_LINE_RECOUNTED" if is_recount else "STOCK_COUNT_LINE_COUNTED",
        entity_type="stock_count_line",
        entity_id=line.id,
        before=before,
        after={
            "counted_quantity": str(counted_quantity),
            "expected_quantity": str(line.expected_quantity),
            "recount_number": line.recount_number,
        },
    )
    db.commit()
    db.refresh(line)
    return line


def mark_stock_count_counted(
    db: Session, stock_count_id: int, *, actor_id: int | None, caller_store_id: int | None
) -> StockCount:
    """OPEN -> COUNTED. A pure checkpoint — does not require every line to
    have a non-null counted_quantity (a legitimately never-counted line
    must still be visible to the reviewer, not silently blocked or
    dropped — Design Decision 2/4)."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status == "COUNTED":
        return count
    if count.status != "OPEN":
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status}, not OPEN — only an open "
            "stock count can be marked counted",
            error_code="INVALID_STOCK_COUNT_STATE",
        )
    count.status = "COUNTED"
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_MARKED_COUNTED",
        entity_type="stock_count",
        entity_id=count.id,
        after={"status": "COUNTED"},
    )
    db.commit()
    db.refresh(count)
    return count


def review_stock_count(
    db: Session, stock_count_id: int, *, actor_id: int | None, caller_store_id: int | None
) -> StockCount:
    """COUNTED -> REVIEWED: the checkpoint requiring
    `inventory.count.review` (a different permission tier than counting
    itself — Design Decision 2), with no inventory/accounting effect
    yet."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status == "REVIEWED":
        return count
    if count.status != "COUNTED":
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status}, not COUNTED — only a counted "
            "stock count can be reviewed",
            error_code="INVALID_STOCK_COUNT_STATE",
        )
    count.status = "REVIEWED"
    count.reviewed_by = actor_id
    count.reviewed_at = datetime.now(UTC)
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_REVIEWED",
        entity_type="stock_count",
        entity_id=count.id,
        after={"status": "REVIEWED"},
    )
    db.commit()
    db.refresh(count)
    return count


def reopen_stock_count_for_recount(
    db: Session,
    stock_count_id: int,
    *,
    actor_id: int | None,
    caller_store_id: int | None,
    reason: str | None = None,
) -> StockCount:
    """REVIEWED -> COUNTED: the explicit, auditable path back to counting
    — required after post_stock_count refuses to post due to drift
    (Design Decision 3/5), or whenever a reviewer wants more lines
    counted before posting. Never implicit: posting drift never silently
    reopens the count itself."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status != "REVIEWED":
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status}, not REVIEWED — only a "
            "reviewed stock count can be reopened for recount",
            error_code="INVALID_STOCK_COUNT_STATE",
        )
    count.status = "COUNTED"
    count.reviewed_by = None
    count.reviewed_at = None
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_REOPENED_FOR_RECOUNT",
        entity_type="stock_count",
        entity_id=count.id,
        after={"status": "COUNTED", "reason": reason},
    )
    db.commit()
    db.refresh(count)
    return count


def post_stock_count(
    db: Session, stock_count_id: int, *, actor_id: int, caller_store_id: int | None
) -> StockCount:
    """REVIEWED -> POSTED: the one financially-atomic transition.

    Locks the StockCount header first (idempotent-by-state: an
    already-POSTED count returns unchanged, mirroring
    post_purchase_invoice's precedent), then every distinct PRODUCT
    referenced by a counted line, ascending product_id order
    (deadlock-safe). For each counted line, under that lock, compares the
    product's CURRENT current_qty_on_hand against the line's stored
    expected_quantity:

    - If they match for EVERY counted line, the whole count posts:
      variance = counted_quantity - current_qty_on_hand is applied via
      create_stock_adjustment (reason STOCKTAKE_CORRECTION,
      stock_count_id set), once per nonzero-variance line, all inside
      this one uncommitted transaction — one commit for the whole count.
    - If ANY line's current quantity has drifted from its expected
      snapshot (something else posted against that product during the
      count window), the ENTIRE posting is refused
      (STOCK_COUNT_DRIFT_DETECTED, naming every drifted line) — nothing
      is posted, nothing is half-applied, and the caller must reopen the
      count and recount the named lines before retrying (Design Decision
      5's mandatory drift-detection policy — never silently overwrite a
      movement that happened after the snapshot)."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status == "POSTED":
        return count
    if count.status != "REVIEWED":
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status}, not REVIEWED — only a "
            "reviewed stock count can be posted",
            error_code="INVALID_STOCK_COUNT_STATE",
        )

    lines = list(
        db.execute(
            select(StockCountLine)
            .where(StockCountLine.stock_count_id == stock_count_id)
            .order_by(StockCountLine.product_id)
        ).scalars()
    )
    counted_lines = [line for line in lines if line.counted_quantity is not None]
    if not counted_lines:
        raise ConflictError(
            f"Stock count {stock_count_id} has no counted lines to post",
            error_code="EMPTY_STOCK_COUNT_POSTING",
        )

    distinct_product_ids = sorted({line.product_id for line in counted_lines})
    locked_products = {
        product_id: lock_product_for_update(db, product_id) for product_id in distinct_product_ids
    }

    drifted: list[StockCountDriftLine] = []
    for line in counted_lines:
        product = locked_products[line.product_id]
        # Guaranteed non-null: a line can only reach _COUNTABLE_STOCK_COUNT_STATUSES
        # (and therefore be recorded in counted_lines) after open_stock_count has
        # already snapshotted expected_quantity for every line.
        assert line.expected_quantity is not None
        if product.current_qty_on_hand != line.expected_quantity:
            drifted.append(
                StockCountDriftLine(
                    stock_count_line_id=line.id,
                    product_id=line.product_id,
                    expected_quantity=line.expected_quantity,
                    current_quantity_on_hand=product.current_qty_on_hand,
                )
            )
    if drifted:
        detail = ", ".join(
            f"product {d.product_id} (expected {d.expected_quantity}, now "
            f"{d.current_quantity_on_hand})"
            for d in drifted
        )
        raise ConflictError(
            f"Stock count {stock_count_id} cannot be posted: book quantity changed since "
            f"counting began for {len(drifted)} line(s) — reopen the count and recount: "
            f"{detail}",
            error_code="STOCK_COUNT_DRIFT_DETECTED",
        )

    adjustments_created = 0
    for line in counted_lines:
        product = locked_products[line.product_id]
        # Guaranteed non-null: counted_lines was filtered on
        # `counted_quantity is not None` above.
        assert line.counted_quantity is not None
        variance = line.counted_quantity - product.current_qty_on_hand
        if variance == 0:
            continue
        create_stock_adjustment(
            db,
            store_id=count.store_id,
            product_id=line.product_id,
            quantity_delta=variance,
            reason_code="STOCKTAKE_CORRECTION",
            notes=f"Stock count {count.count_number}",
            created_by=actor_id,
            stock_count_id=count.id,
        )
        adjustments_created += 1

    count.status = "POSTED"
    count.posted_by = actor_id
    count.posted_at = datetime.now(UTC)

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_POSTED",
        entity_type="stock_count",
        entity_id=count.id,
        after={
            "counted_line_count": len(counted_lines),
            "adjustments_created": adjustments_created,
        },
    )
    db.commit()
    db.refresh(count)
    return count


def cancel_stock_count(
    db: Session,
    stock_count_id: int,
    *,
    actor_id: int | None,
    caller_store_id: int | None,
    reason: str | None = None,
) -> StockCount:
    """Cancellable from DRAFT/OPEN/COUNTED/REVIEWED — never from POSTED
    (Design Decision 2). Has no inventory/accounting effect to undo,
    since nothing is ever posted before this point — a pure status flip
    plus audit entry."""
    count = db.execute(
        select(StockCount).where(StockCount.id == stock_count_id).with_for_update()
    ).scalar_one_or_none()
    if count is None:
        raise NotFoundError(f"Stock count {stock_count_id} not found")
    _enforce_store_access(caller_store_id, count.store_id, "this stock count")
    if count.status == "CANCELLED":
        return count
    if count.status not in _CANCELLABLE_STOCK_COUNT_STATUSES:
        raise ConflictError(
            f"Stock count {stock_count_id} is {count.status} and cannot be cancelled",
            error_code="INVALID_STOCK_COUNT_STATE",
        )
    before_status = count.status
    count.status = "CANCELLED"
    count.cancelled_by = actor_id
    count.cancelled_at = datetime.now(UTC)
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STOCK_COUNT_CANCELLED",
        entity_type="stock_count",
        entity_id=count.id,
        before={"status": before_status},
        after={"status": "CANCELLED", "reason": reason},
    )
    db.commit()
    db.refresh(count)
    return count
