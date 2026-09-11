"""Inventory ledger read/write helpers and the Weighted Average Cost formula.

This is intentionally small: docs/TECHNICAL_BLUEPRINT.md Section D and the
M1 task instructions ask for the data model and the WAC math to be proven
correct, not a full Inventory Service API (that's Milestone M2). Every
function here is used either directly by tests (to prove the formula) or
by app.modules.purchasing.service.receive_goods (to prove one real,
transactional, row-locked vertical slice through the ledger).
"""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.audit import service as audit_service
from app.modules.inventory.models import InventoryMovement, StockAdjustment
from app.modules.products.models import Product

_WAC_QUANTUM = Decimal("0.000001")  # matches Numeric(14, 6) storage precision


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
) -> StockAdjustment:
    """The full authenticated, transactional, audited stock-adjustment
    flow (M2 task Section 6): the API can never overwrite
    current_qty_on_hand directly — every change is this function creating
    one StockAdjustment (the human-facing record) plus exactly one
    InventoryMovement (the ledger entry), atomically, with the actor
    recorded on both and an audit_logs entry to match. Does not commit —
    the caller (the route handler) does, once this returns successfully.
    """
    adjustment = StockAdjustment(
        store_id=store_id,
        product_id=product_id,
        quantity_delta=quantity_delta,
        reason_code=reason_code,
        notes=notes,
        created_by=created_by,
    )
    db.add(adjustment)
    db.flush()

    product = lock_product_for_update(db, product_id)
    movement_type = "STOCK_ADJUSTMENT_IN" if quantity_delta > 0 else "STOCK_ADJUSTMENT_OUT"
    record_movement(
        db,
        product=product,
        store_id=store_id,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        unit_cost_at_movement=product.current_cost,
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
    return adjustment
