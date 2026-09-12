"""Replenishment: M8's read-only suggested-reorder report, plus M9's
persisted recommendation → approval → execution lifecycle built on top of
it. See docs/M9_SUPPLY_CHAIN_DESIGN.md for the full design — in
particular Design Decision 1 (`compute_position`/`compute_positions_bulk`
is the ONE authoritative position formula both the M8 report and the M9
generator call, so they can never silently drift), Design Decision 8 (the
execution transaction's exact locking/revalidation/rounding order), and
Design Decision 9 (a generated PO/transfer never advances past DRAFT —
`app.modules.purchasing.service._create_purchase_order_inner` and
`app.modules.transfers.service._create_transfer_inner` are the ONLY two
mutation entry points this module ever calls; it never calls
`app.modules.accounting.service` directly, by construction).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.audit import service as audit_service
from app.modules.inventory import service as inventory_service
from app.modules.products.models import Product
from app.modules.purchasing.models import PurchaseOrder, PurchaseOrderItem, Supplier
from app.modules.purchasing.service import PurchaseOrderItemInput, _create_purchase_order_inner
from app.modules.replenishment.models import (
    _CANCELLABLE_PLAN_STATUSES,
    ReplenishmentPlan,
    SupplierProduct,
)
from app.modules.transfers.models import InterStoreTransfer, InterStoreTransferLine
from app.modules.transfers.service import TransferLineInput, _create_transfer_inner

_OPEN_PO_STATUSES = ("DRAFT", "ORDERED", "PARTIALLY_RECEIVED")


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access "
            f"{noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


def _enforce_plan_access(caller_store_id: int | None, plan: ReplenishmentPlan) -> None:
    """A store-scoped caller may act on a plan if their store is the
    DESTINATION, or — for a transfer plan — the SOURCE (mirrors
    app.modules.transfers.service's own from/to symmetry: either side's
    staff may see/act on a transfer touching their store)."""
    if caller_store_id is None:
        return
    if caller_store_id == plan.destination_store_id:
        return
    if plan.source_type == "TRANSFER" and caller_store_id == plan.source_store_id:
        return
    raise ForbiddenError(
        f"Your account is scoped to store {caller_store_id} and cannot access this "
        "replenishment plan",
        error_code="STORE_ACCESS_DENIED",
    )


# --- Design Decision 1: the one authoritative position calculation ----------


@dataclass(frozen=True)
class PositionBreakdown:
    on_hand: Decimal
    inbound_transfer_qty: Decimal
    open_purchase_order_qty: Decimal
    position: Decimal  # on_hand + inbound_transfer_qty + open_purchase_order_qty


def _inbound_transfer_quantities(db: Session, product_ids: list[int]) -> dict[int, Decimal]:
    rows = db.execute(
        select(
            InterStoreTransferLine.destination_product_id,
            InterStoreTransferLine.shipped_quantity,
            InterStoreTransferLine.received_quantity,
        )
        .join(InterStoreTransfer, InterStoreTransfer.id == InterStoreTransferLine.transfer_id)
        .where(
            InterStoreTransfer.status == "SHIPPED",
            InterStoreTransferLine.destination_product_id.in_(product_ids),
        )
    ).all()
    result: dict[int, Decimal] = {}
    for destination_product_id, shipped, received in rows:
        remaining = shipped - received
        if remaining > 0:
            result[destination_product_id] = (
                result.get(destination_product_id, Decimal("0")) + remaining
            )
    return result


def _open_purchase_order_quantities(db: Session, product_ids: list[int]) -> dict[int, Decimal]:
    rows = db.execute(
        select(
            PurchaseOrderItem.product_id,
            PurchaseOrderItem.quantity_ordered,
            PurchaseOrderItem.quantity_received,
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(
            PurchaseOrder.status.in_(_OPEN_PO_STATUSES),
            PurchaseOrderItem.product_id.in_(product_ids),
        )
    ).all()
    result: dict[int, Decimal] = {}
    for product_id, ordered, received in rows:
        remaining = ordered - received
        if remaining > 0:
            result[product_id] = result.get(product_id, Decimal("0")) + remaining
    return result


def compute_positions_bulk(db: Session, products: list[Product]) -> dict[int, PositionBreakdown]:
    """The batched form of `compute_position` — one query for inbound
    transfers and one for open POs across every product given, instead of
    N+1. Used by both `get_replenishment_suggestions` (M8) and
    `generate_replenishment_plans` (M9) so the two can never compute this
    differently."""
    if not products:
        return {}
    product_ids = [p.id for p in products]
    inbound = _inbound_transfer_quantities(db, product_ids)
    open_po = _open_purchase_order_quantities(db, product_ids)
    result: dict[int, PositionBreakdown] = {}
    for product in products:
        inbound_qty = inbound.get(product.id, Decimal("0"))
        open_po_qty = open_po.get(product.id, Decimal("0"))
        result[product.id] = PositionBreakdown(
            on_hand=product.current_qty_on_hand,
            inbound_transfer_qty=inbound_qty,
            open_purchase_order_qty=open_po_qty,
            position=product.current_qty_on_hand + inbound_qty + open_po_qty,
        )
    return result


def compute_position(db: Session, product: Product) -> PositionBreakdown:
    """Single-product convenience wrapper — used at execution time, where
    only one (locked) product's fresh position is needed. Calls the exact
    same batched helpers `compute_positions_bulk` uses, at N=1, so there is
    still only one formula, not two."""
    return compute_positions_bulk(db, [product])[product.id]


# --- M8: read-only suggested-reorder report (unchanged behavior) -----------


@dataclass(frozen=True)
class ReplenishmentSuggestion:
    product_id: int
    store_id: int
    sku: str
    name: str
    reorder_point: Decimal
    current_qty_on_hand: Decimal
    inbound_transfer_qty: Decimal
    open_purchase_order_qty: Decimal
    inventory_position: Decimal
    shortfall: Decimal
    suggested_transfer_quantity: Decimal
    suggested_purchase_quantity: Decimal
    sister_store_surplus_source_store_id: int | None


def get_replenishment_suggestions(
    db: Session, *, store_id: int | None = None
) -> list[ReplenishmentSuggestion]:
    """One row per product whose inventory_position has fallen at or
    below its reorder_point. Unchanged from M8 except that the position
    calculation now delegates to `compute_positions_bulk` (Design Decision
    1) instead of duplicating the on_hand+inbound+open_po arithmetic
    inline — the M8 test suite for this function must still pass
    unmodified, proving this is a pure refactor, not a behavior change."""
    query = select(Product).where(Product.reorder_point.is_not(None), Product.is_active.is_(True))
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    products = list(db.execute(query).scalars().all())
    if not products:
        return []

    positions = compute_positions_bulk(db, products)

    suggestions: list[ReplenishmentSuggestion] = []
    for product in products:
        pos = positions[product.id]
        assert product.reorder_point is not None  # guaranteed by the query filter above
        shortfall = max(Decimal("0"), product.reorder_point - pos.position)
        if shortfall <= 0:
            continue

        suggested_transfer = Decimal("0")
        surplus_source_store_id: int | None = None
        sister_products = (
            db.execute(
                select(Product).where(
                    Product.sku == product.sku,
                    Product.store_id != product.store_id,
                    Product.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        for sister_product in sister_products:
            sister_reorder = sister_product.reorder_point or Decimal("0")
            sister_surplus = sister_product.current_qty_on_hand - sister_reorder
            if sister_surplus > 0:
                suggested_transfer = min(shortfall, sister_surplus)
                surplus_source_store_id = sister_product.store_id
                break
        suggested_purchase = shortfall - suggested_transfer

        suggestions.append(
            ReplenishmentSuggestion(
                product_id=product.id,
                store_id=product.store_id,
                sku=product.sku,
                name=product.name,
                reorder_point=product.reorder_point,
                current_qty_on_hand=pos.on_hand,
                inbound_transfer_qty=pos.inbound_transfer_qty,
                open_purchase_order_qty=pos.open_purchase_order_qty,
                inventory_position=pos.position,
                shortfall=shortfall,
                suggested_transfer_quantity=suggested_transfer,
                suggested_purchase_quantity=suggested_purchase,
                sister_store_surplus_source_store_id=surplus_source_store_id,
            )
        )
    return suggestions


# --- M9: deterministic ranking (Design Decision 6) --------------------------


def rank_source_stores(
    db: Session, product: Product, shortfall: Decimal
) -> list[tuple[int, Decimal]]:
    """Candidate source stores carrying the same SKU, each with surplus
    ABOVE ITS OWN reorder point (never eating into a source store's own
    trigger threshold — task Section 7), sorted by descending available
    surplus, ties broken by ascending store_id (design answer #3)."""
    sister_products = (
        db.execute(
            select(Product).where(
                Product.sku == product.sku,
                Product.store_id != product.store_id,
                Product.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    candidates: list[tuple[int, Decimal]] = []
    for sister in sister_products:
        sister_reorder = sister.reorder_point or Decimal("0")
        surplus = sister.current_qty_on_hand - sister_reorder
        if surplus > 0:
            candidates.append((sister.store_id, surplus))
    candidates.sort(key=lambda c: (-c[1], c[0]))
    return candidates


def get_current_supplier_product(
    db: Session, supplier_id: int, product_id: int, *, as_of: date | None = None
) -> SupplierProduct | None:
    """Design Decision 2's deterministic price rule: the `is_active` row
    with the greatest `effective_date` not after `as_of` (defaults to
    today). Used both for display and, at execution, for the authoritative
    price snapshot — always re-read fresh, never trusted from a stale
    plan column (design answer #18)."""
    as_of = as_of or date.today()
    return db.execute(
        select(SupplierProduct)
        .where(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.product_id == product_id,
            SupplierProduct.is_active.is_(True),
            SupplierProduct.effective_date <= as_of,
        )
        .order_by(SupplierProduct.effective_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def create_supplier_product(
    db: Session,
    *,
    supplier_id: int,
    product_id: int,
    supplier_sku: str | None,
    pack_size: Decimal,
    unit_cost: Decimal,
    minimum_order_quantity: Decimal | None,
    lead_time_days: int | None,
    effective_date: date,
    actor_id: int | None,
) -> SupplierProduct:
    """Insert-only (Design Decision 2): a price/terms change is always a
    NEW row with a later effective_date, never an UPDATE of an existing
    row — so historical POs, which snapshot price at generation/execution
    time, are never retroactively affected by a later price change."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    row = SupplierProduct(
        supplier_id=supplier_id,
        product_id=product_id,
        supplier_sku=supplier_sku,
        pack_size=pack_size,
        unit_cost=unit_cost,
        minimum_order_quantity=minimum_order_quantity,
        lead_time_days=lead_time_days,
        effective_date=effective_date,
        is_active=True,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"A price for supplier {supplier_id}/product {product_id} effective "
            f"{effective_date} already exists",
            error_code="DUPLICATE_SUPPLIER_PRODUCT",
        ) from exc
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SUPPLIER_PRODUCT_CREATED",
        entity_type="supplier_product",
        entity_id=row.id,
        after={
            "supplier_id": supplier_id,
            "product_id": product_id,
            "unit_cost": str(unit_cost),
            "effective_date": effective_date.isoformat(),
        },
    )
    db.commit()
    db.refresh(row)
    return row


def list_supplier_products(
    db: Session,
    *,
    supplier_id: int | None = None,
    product_id: int | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SupplierProduct]:
    query = select(SupplierProduct).order_by(SupplierProduct.id.desc()).limit(limit).offset(offset)
    if supplier_id is not None:
        query = query.where(SupplierProduct.supplier_id == supplier_id)
    if product_id is not None:
        query = query.where(SupplierProduct.product_id == product_id)
    if is_active is not None:
        query = query.where(SupplierProduct.is_active == is_active)
    return list(db.execute(query).scalars().all())


def apply_moq_and_pack_rounding(
    need: Decimal, minimum_order_quantity: Decimal | None, pack_size: Decimal | None
) -> Decimal:
    """The one authoritative rounding function (Design Decision 7):
    max(need, MOQ), THEN round up to the next whole multiple of pack_size
    (default 1 = no rounding). Order is fixed and never duplicated inline
    at any call site."""
    quantity = need
    if minimum_order_quantity is not None and quantity < minimum_order_quantity:
        quantity = minimum_order_quantity
    pack = pack_size if pack_size and pack_size > 0 else Decimal("1")
    if pack != Decimal("1"):
        remainder = quantity % pack
        if remainder != 0:
            quantity = quantity + (pack - remainder)
    return quantity


def rank_suppliers(db: Session, product: Product, quantity: Decimal) -> list[SupplierProduct]:
    """Deterministic total order (design answer #11): active suppliers
    only; preferred (`Product.default_supplier_id`) first; then lowest
    price; then least MOQ/pack-rounding waste; then shortest lead time;
    then lowest supplier_id as the final tiebreak — never database row
    order."""
    supplier_ids = (
        db.execute(
            select(SupplierProduct.supplier_id)
            .where(SupplierProduct.product_id == product.id)
            .distinct()
        )
        .scalars()
        .all()
    )
    ranked: list[tuple[SupplierProduct, bool, Decimal]] = []
    for supplier_id in supplier_ids:
        candidate = get_current_supplier_product(db, supplier_id, product.id)
        if candidate is None:
            continue
        supplier = db.get(Supplier, supplier_id)
        if supplier is None or not supplier.is_active:
            continue
        rounded = apply_moq_and_pack_rounding(
            quantity, candidate.minimum_order_quantity, candidate.pack_size
        )
        waste = rounded - quantity
        is_preferred = product.default_supplier_id == supplier_id
        ranked.append((candidate, is_preferred, waste))
    ranked.sort(
        key=lambda t: (
            0 if t[1] else 1,
            t[0].unit_cost,
            t[2],
            t[0].lead_time_days if t[0].lead_time_days is not None else 2**31,
            t[0].supplier_id,
        )
    )
    return [t[0] for t in ranked]


def classify_urgency(product: Product, position: Decimal) -> str:
    """Design answer #12: at/below zero is always URGENT; below the
    configured minimum_stock_quantity (when set) is also URGENT;
    otherwise (below reorder_point, at/above minimum) is NORMAL."""
    if position <= 0:
        return "URGENT"
    if product.minimum_stock_quantity is not None and position < product.minimum_stock_quantity:
        return "URGENT"
    return "NORMAL"


# --- M9: recommendation generation -------------------------------------


def generate_replenishment_plans(
    db: Session,
    *,
    store_id: int | None = None,
    product_ids: list[int] | None = None,
    created_by: int | None = None,
    caller_store_id: int | None = None,
) -> list[ReplenishmentPlan]:
    """Persists one or more sibling `ReplenishmentPlan` rows (Design
    Decision 5) per product whose position has fallen to/below its
    reorder_point, splitting the shortfall across ranked source stores
    first (Design Decision 6) and a ranked supplier for the remainder.
    Skips a product if an active (RECOMMENDED/APPROVED) plan already
    exists for it — the generation-time half of duplicate-replenishment
    prevention (design answer #16), so re-running generation repeatedly
    never piles up redundant recommendations for the same shortage."""
    if caller_store_id is not None:
        if store_id is not None and store_id != caller_store_id:
            raise ForbiddenError(
                f"Your account is scoped to store {caller_store_id} and cannot generate "
                f"recommendations for store {store_id}",
                error_code="STORE_ACCESS_DENIED",
            )
        store_id = caller_store_id

    query = select(Product).where(Product.reorder_point.is_not(None), Product.is_active.is_(True))
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    products = list(db.execute(query).scalars().all())
    if not products:
        return []

    positions = compute_positions_bulk(db, products)
    batch_id = str(uuid.uuid4())
    created_plans: list[ReplenishmentPlan] = []

    for product in products:
        pos = positions[product.id]
        assert product.reorder_point is not None
        target = product.target_stock_quantity or product.reorder_point
        shortfall = target - pos.position
        if pos.position > product.reorder_point or shortfall <= 0:
            continue

        # Transactional locking (design answer #16(a)): serializes two
        # concurrent generation runs racing over the SAME (store, product)
        # shortage. A plain service-level "already active?" check alone is
        # TOCTOU-vulnerable — two callers could both pass it before either
        # commits. A row lock isn't usable here because there is no
        # existing plan ROW to lock yet on a first-time shortage; an
        # advisory lock needs no such row and is released automatically at
        # transaction end either way (same rationale as
        # app.modules.accounting.service.reverse_journal_entry's own use of
        # pg_advisory_xact_lock). Keyed on (store_id, product_id) rather
        # than a single combined int so it's held for exactly the
        # (destination store, product) pair being decided, held for the
        # rest of THIS transaction — which correctly still allows this same
        # call to insert multiple SIBLING plans for that pair (Design
        # Decision 5) while blocking any OTHER concurrent transaction from
        # doing so until this one commits or rolls back.
        db.execute(select(func.pg_advisory_xact_lock(product.store_id, product.id)))

        already_active = db.execute(
            select(ReplenishmentPlan.id).where(
                ReplenishmentPlan.destination_store_id == product.store_id,
                ReplenishmentPlan.product_id == product.id,
                ReplenishmentPlan.status.in_(("RECOMMENDED", "APPROVED")),
            )
        ).first()
        if already_active is not None:
            continue

        urgency = classify_urgency(product, pos.position)
        remaining = shortfall

        for source_store_id, available in rank_source_stores(db, product, remaining):
            if remaining <= 0:
                break
            take = min(remaining, available)
            if take <= 0:
                continue
            plan = ReplenishmentPlan(
                generation_batch_id=batch_id,
                destination_store_id=product.store_id,
                product_id=product.id,
                needed_quantity=shortfall,
                suggested_quantity=take,
                source_type="TRANSFER",
                source_store_id=source_store_id,
                urgency=urgency,
                reason=(
                    f"Position {pos.position} is at/below reorder point {product.reorder_point} "
                    f"(target {target}, shortfall {shortfall}). Store {source_store_id} has "
                    f"surplus of {available} above its own reorder point; sourcing {take} from "
                    "it covers part of the shortfall without touching that store's own trigger "
                    "threshold."
                ),
            )
            db.add(plan)
            created_plans.append(plan)
            remaining -= take

        if remaining > 0:
            ranked = rank_suppliers(db, product, remaining)
            if ranked:
                best = ranked[0]
                rounded = apply_moq_and_pack_rounding(
                    remaining, best.minimum_order_quantity, best.pack_size
                )
                is_preferred = product.default_supplier_id == best.supplier_id
                rounding_note = (
                    f" (rounded up from {remaining} for MOQ {best.minimum_order_quantity}/pack "
                    f"size {best.pack_size})"
                    if rounded != remaining
                    else ""
                )
                plan = ReplenishmentPlan(
                    generation_batch_id=batch_id,
                    destination_store_id=product.store_id,
                    product_id=product.id,
                    needed_quantity=shortfall,
                    suggested_quantity=rounded,
                    source_type="SUPPLIER",
                    supplier_id=best.supplier_id,
                    suggested_unit_cost=best.unit_cost,
                    urgency=urgency,
                    reason=(
                        f"Position {pos.position} is at/below reorder point "
                        f"{product.reorder_point} "
                        f"(target {target}, shortfall {shortfall}). No further store surplus "
                        f"available; supplier {best.supplier_id} selected "
                        f"({'preferred' if is_preferred else 'lowest ranked price/terms'}, "
                        f"unit cost {best.unit_cost}) to cover the remaining {remaining}"
                        f"{rounding_note}."
                    ),
                )
                db.add(plan)
                created_plans.append(plan)
                remaining = Decimal("0")
            # else: no supplier carries this product either — surfaced via
            # get_exceptions() as SUPPLIER_PRICE_MISSING/no candidate, not
            # silently dropped and not fabricated as a plan with no source.

    if created_plans:
        db.flush()
        for plan in created_plans:
            audit_service.log_event(
                db,
                user_id=created_by,
                action="REPLENISHMENT_PLAN_GENERATED",
                entity_type="replenishment_plan",
                entity_id=plan.id,
                after={
                    "destination_store_id": plan.destination_store_id,
                    "product_id": plan.product_id,
                    "source_type": plan.source_type,
                    "suggested_quantity": str(plan.suggested_quantity),
                    "urgency": plan.urgency,
                },
            )
        db.commit()
        for plan in created_plans:
            db.refresh(plan)
    return created_plans


# --- M9: read/status-transition functions -----------------------------


def get_plan(db: Session, plan_id: int) -> ReplenishmentPlan:
    plan = db.get(ReplenishmentPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Replenishment plan {plan_id} not found")
    return plan


def list_plans(
    db: Session,
    *,
    store_id: int | None = None,
    status: str | None = None,
    generation_batch_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReplenishmentPlan]:
    query = (
        select(ReplenishmentPlan)
        .order_by(ReplenishmentPlan.created_at.desc(), ReplenishmentPlan.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(
            or_(
                ReplenishmentPlan.destination_store_id == store_id,
                ReplenishmentPlan.source_store_id == store_id,
            )
        )
    if status is not None:
        query = query.where(ReplenishmentPlan.status == status)
    if generation_batch_id is not None:
        query = query.where(ReplenishmentPlan.generation_batch_id == generation_batch_id)
    return list(db.execute(query).scalars().all())


def get_remaining_need(db: Session, plan: ReplenishmentPlan) -> Decimal:
    """Section 11: "remaining need" for the shortage a plan belongs to —
    computed on read from its siblings, never a separately maintained
    running total (Design Decision 5)."""
    siblings = list(
        db.execute(
            select(ReplenishmentPlan).where(
                ReplenishmentPlan.generation_batch_id == plan.generation_batch_id,
                ReplenishmentPlan.destination_store_id == plan.destination_store_id,
                ReplenishmentPlan.product_id == plan.product_id,
            )
        )
        .scalars()
        .all()
    )
    if not siblings:
        return Decimal("0")
    executed_total = sum(
        (s.executed_quantity or Decimal("0") for s in siblings if s.status == "EXECUTED"),
        Decimal("0"),
    )
    return max(Decimal("0"), siblings[0].needed_quantity - executed_total)


def is_plan_fulfilled(db: Session, plan: ReplenishmentPlan) -> bool | None:
    """Design Decision 4's derived FULFILLED label — None if the question
    doesn't apply yet (not EXECUTED), else whether the linked document has
    been fully received."""
    if plan.status != "EXECUTED":
        return None
    if plan.generated_purchase_order_id is not None:
        items = list(
            db.execute(
                select(PurchaseOrderItem).where(
                    PurchaseOrderItem.purchase_order_id == plan.generated_purchase_order_id
                )
            )
            .scalars()
            .all()
        )
        return bool(items) and all(
            item.quantity_received >= item.quantity_ordered for item in items
        )
    if plan.generated_transfer_id is not None:
        lines = list(
            db.execute(
                select(InterStoreTransferLine).where(
                    InterStoreTransferLine.transfer_id == plan.generated_transfer_id
                )
            )
            .scalars()
            .all()
        )
        return bool(lines) and all(
            line.shipped_quantity > 0 and line.received_quantity >= line.shipped_quantity
            for line in lines
        )
    return False


def approve_plan(
    db: Session, plan_id: int, *, actor_id: int | None, caller_store_id: int | None
) -> ReplenishmentPlan:
    """RECOMMENDED -> APPROVED. Idempotent by state, mirroring every other
    status-transition function in this codebase (design answer #2/#19)."""
    plan = db.execute(
        select(ReplenishmentPlan).where(ReplenishmentPlan.id == plan_id).with_for_update()
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Replenishment plan {plan_id} not found")
    _enforce_plan_access(caller_store_id, plan)
    if plan.status == "APPROVED":
        return plan
    if plan.status != "RECOMMENDED":
        raise ConflictError(
            f"Plan {plan_id} is {plan.status}, not RECOMMENDED — only a recommended plan "
            "can be approved",
            error_code="INVALID_PLAN_STATE",
        )
    plan.status = "APPROVED"
    plan.approved_by = actor_id
    plan.approved_at = datetime.now(UTC)
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="REPLENISHMENT_PLAN_APPROVED",
        entity_type="replenishment_plan",
        entity_id=plan.id,
        after={"status": "APPROVED"},
    )
    db.commit()
    db.refresh(plan)
    return plan


def cancel_plan(
    db: Session,
    plan_id: int,
    *,
    actor_id: int | None,
    caller_store_id: int | None,
    reason: str | None = None,
) -> ReplenishmentPlan:
    """Cancellable from RECOMMENDED/APPROVED/STALE — never from EXECUTED
    (mirrors M8's "never cancel a POSTED stock count" precedent)."""
    plan = db.execute(
        select(ReplenishmentPlan).where(ReplenishmentPlan.id == plan_id).with_for_update()
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Replenishment plan {plan_id} not found")
    _enforce_plan_access(caller_store_id, plan)
    if plan.status == "CANCELLED":
        return plan
    if plan.status not in _CANCELLABLE_PLAN_STATUSES:
        raise ConflictError(
            f"Plan {plan_id} is {plan.status} and cannot be cancelled",
            error_code="INVALID_PLAN_STATE",
        )
    before_status = plan.status
    plan.status = "CANCELLED"
    plan.cancelled_by = actor_id
    plan.cancelled_at = datetime.now(UTC)
    plan.cancellation_reason = reason
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="REPLENISHMENT_PLAN_CANCELLED",
        entity_type="replenishment_plan",
        entity_id=plan.id,
        before={"status": before_status},
        after={"status": "CANCELLED", "reason": reason},
    )
    db.commit()
    db.refresh(plan)
    return plan


# --- M9: execution (Design Decision 8) ----------------------------------


def execute_plan(
    db: Session,
    plan_id: int,
    *,
    actor_id: int | None,
    caller_store_id: int | None,
    client_transaction_id: str,
) -> ReplenishmentPlan:
    """APPROVED -> EXECUTED: the one financially/operationally-atomic
    transition. Locks the plan row first (serializes concurrent/retried
    execution of the SAME plan — design answer #16), re-validates the
    authoritative position fresh under a product-row lock (design answer
    #18), caps the executed quantity at the originally approved quantity
    (design answer #17), applies MOQ/pack rounding fresh (never reusing a
    stale generation-time value), creates the DRAFT PO/transfer via the
    non-committing `_inner` function (Design Decision 3), links it back,
    and commits once."""
    existing = db.execute(
        select(ReplenishmentPlan).where(
            ReplenishmentPlan.execution_client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    plan = db.execute(
        select(ReplenishmentPlan).where(ReplenishmentPlan.id == plan_id).with_for_update()
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Replenishment plan {plan_id} not found")
    _enforce_plan_access(caller_store_id, plan)

    if plan.status == "EXECUTED":
        return plan
    if plan.execution_client_transaction_id == client_transaction_id:
        return plan
    if plan.status != "APPROVED":
        raise ConflictError(
            f"Plan {plan_id} is {plan.status}, not APPROVED — only an approved plan can be "
            "executed",
            error_code="INVALID_PLAN_STATE",
        )

    # Determine every product this execution touches WITHOUT populating the
    # identity map from an un-locked full-entity read (the exact M7
    # concurrency bug class: see docs/M7_HARDENING_AUDIT.md Section 2) —
    # scalar-only preview queries, then lock everything in one ascending
    # pass.
    destination_sku = db.execute(
        select(Product.sku).where(Product.id == plan.product_id)
    ).scalar_one_or_none()
    if destination_sku is None:
        raise NotFoundError(f"Product {plan.product_id} not found")

    source_product_id: int | None = None
    if plan.source_type == "TRANSFER":
        source_product_id = db.execute(
            select(Product.id).where(
                Product.store_id == plan.source_store_id, Product.sku == destination_sku
            )
        ).scalar_one_or_none()
        if source_product_id is None:
            raise NotFoundError(
                f"Source store {plan.source_store_id} no longer carries SKU {destination_sku!r}"
            )

    ids_to_lock: list[int] = sorted(
        {plan.product_id} | ({source_product_id} if source_product_id is not None else set())
    )
    locked_products = {
        pid: inventory_service.lock_product_for_update(db, pid) for pid in ids_to_lock
    }
    destination_product = locked_products[plan.product_id]
    source_product = locked_products.get(source_product_id) if source_product_id else None

    position = compute_position(db, destination_product)
    target = (
        destination_product.target_stock_quantity
        or destination_product.reorder_point
        or Decimal("0")
    )
    current_shortfall = target - position.position

    if current_shortfall <= 0:
        plan.status = "STALE"
        plan.stale_detected_at = datetime.now(UTC)
        plan.stale_reason = (
            f"Position recovered to {position.position} (target {target}) before execution — "
            f"original shortfall at generation was {plan.needed_quantity}."
        )
        audit_service.log_event(
            db,
            user_id=actor_id,
            action="REPLENISHMENT_PLAN_MARKED_STALE",
            entity_type="replenishment_plan",
            entity_id=plan.id,
            after={"stale_reason": plan.stale_reason},
        )
        db.commit()
        raise ConflictError(
            f"Plan {plan_id} is stale: {plan.stale_reason} Reopen with a fresh recommendation "
            "if replenishment is still needed.",
            error_code="STALE_RECOMMENDATION",
        )

    executable_quantity = min(plan.suggested_quantity, current_shortfall)

    if plan.source_type == "SUPPLIER":
        if plan.supplier_id is None:
            raise ValidationAppError("Plan has no supplier_id", error_code="INVALID_PLAN")
        supplier = db.get(Supplier, plan.supplier_id)
        supplier_product = get_current_supplier_product(db, plan.supplier_id, plan.product_id)
        if supplier is None or not supplier.is_active or supplier_product is None:
            plan.status = "STALE"
            plan.stale_detected_at = datetime.now(UTC)
            plan.stale_reason = (
                f"Supplier {plan.supplier_id} is no longer active or no longer carries this "
                "product at any price."
            )
            audit_service.log_event(
                db,
                user_id=actor_id,
                action="REPLENISHMENT_PLAN_MARKED_STALE",
                entity_type="replenishment_plan",
                entity_id=plan.id,
                after={"stale_reason": plan.stale_reason},
            )
            db.commit()
            raise ConflictError(plan.stale_reason, error_code="SUPPLIER_NO_LONGER_AVAILABLE")

        rounded_quantity = apply_moq_and_pack_rounding(
            executable_quantity, supplier_product.minimum_order_quantity, supplier_product.pack_size
        )
        purchase_order = _create_purchase_order_inner(
            db,
            store_id=plan.destination_store_id,
            supplier_id=plan.supplier_id,
            order_date=date.today(),
            lines=[
                PurchaseOrderItemInput(
                    product_id=plan.product_id,
                    quantity_ordered=rounded_quantity,
                    unit_cost=supplier_product.unit_cost,
                )
            ],
            created_by=actor_id,
            caller_store_id=caller_store_id,
            replenishment_plan_id=plan.id,
        )
        plan.generated_purchase_order_id = purchase_order.id
        plan.executed_unit_cost = supplier_product.unit_cost
    else:
        assert source_product is not None  # guaranteed by source_type == "TRANSFER" above
        source_surplus = max(
            Decimal("0"),
            source_product.current_qty_on_hand - (source_product.reorder_point or Decimal("0")),
        )
        rounded_quantity = min(executable_quantity, source_surplus)
        if rounded_quantity <= 0:
            plan.status = "STALE"
            plan.stale_detected_at = datetime.now(UTC)
            plan.stale_reason = (
                f"Source store {plan.source_store_id} no longer has surplus above its own "
                "reorder point."
            )
            audit_service.log_event(
                db,
                user_id=actor_id,
                action="REPLENISHMENT_PLAN_MARKED_STALE",
                entity_type="replenishment_plan",
                entity_id=plan.id,
                after={"stale_reason": plan.stale_reason},
            )
            db.commit()
            raise ConflictError(plan.stale_reason, error_code="SOURCE_STORE_INSUFFICIENT_SURPLUS")

        transfer = _create_transfer_inner(
            db,
            from_store_id=plan.source_store_id,  # type: ignore[arg-type]
            to_store_id=plan.destination_store_id,
            requested_date=date.today(),
            lines=[
                TransferLineInput(
                    source_product_id=source_product.id,
                    requested_quantity=rounded_quantity,
                    destination_product_id=destination_product.id,
                )
            ],
            requested_by=actor_id,
            caller_store_id=caller_store_id,
            replenishment_plan_id=plan.id,
        )
        plan.generated_transfer_id = transfer.id

    before = {"suggested_quantity": str(plan.suggested_quantity)}
    plan.status = "EXECUTED"
    plan.executed_by = actor_id
    plan.executed_at = datetime.now(UTC)
    plan.executed_quantity = rounded_quantity
    plan.execution_client_transaction_id = client_transaction_id

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="REPLENISHMENT_PLAN_EXECUTED",
        entity_type="replenishment_plan",
        entity_id=plan.id,
        before=before,
        after={
            "executed_quantity": str(rounded_quantity),
            "generated_purchase_order_id": plan.generated_purchase_order_id,
            "generated_transfer_id": plan.generated_transfer_id,
        },
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(ReplenishmentPlan).where(
                ReplenishmentPlan.execution_client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner
    db.refresh(plan)
    return plan


# --- M9: metrics and exceptions (Sections 15-16) ------------------------


@dataclass(frozen=True)
class SupplyChainMetrics:
    store_id: int | None
    open_purchase_order_qty: Decimal  # Σ(quantity_ordered - quantity_received) on open POs
    inbound_transfer_qty: Decimal  # Σ(shipped_quantity - received_quantity) on SHIPPED transfers
    products_below_reorder_point: int
    products_below_minimum: int  # URGENT-classified products (design answer #12)
    overdue_purchase_order_count: int  # ORDERED/PARTIALLY_RECEIVED POs past expected_date


def get_supply_chain_metrics(db: Session, *, store_id: int | None = None) -> SupplyChainMetrics:
    """Every figure here has the documented formula in its own field
    comment above — no fabricated metric (e.g. "days of supply" or "fill
    rate") is included: this system has no consumption-rate or
    on-time-delivery data to derive either honestly."""
    po_query = (
        select(PurchaseOrderItem, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(PurchaseOrder.status.in_(_OPEN_PO_STATUSES))
    )
    if store_id is not None:
        po_query = po_query.where(PurchaseOrder.store_id == store_id)
    open_po_qty = Decimal("0")
    overdue_pos: set[int] = set()
    today = date.today()
    for item, po in db.execute(po_query).all():
        remaining = item.quantity_ordered - item.quantity_received
        if remaining > 0:
            open_po_qty += remaining
            if (
                po.status in ("ORDERED", "PARTIALLY_RECEIVED")
                and po.expected_date is not None
                and po.expected_date < today
            ):
                overdue_pos.add(po.id)

    transfer_query = (
        select(InterStoreTransferLine)
        .join(InterStoreTransfer, InterStoreTransfer.id == InterStoreTransferLine.transfer_id)
        .where(InterStoreTransfer.status == "SHIPPED")
    )
    if store_id is not None:
        transfer_query = transfer_query.where(InterStoreTransfer.to_store_id == store_id)
    inbound_qty = Decimal("0")
    for line in db.execute(transfer_query).scalars().all():
        remaining = line.shipped_quantity - line.received_quantity
        if remaining > 0:
            inbound_qty += remaining

    product_query = select(Product).where(
        Product.reorder_point.is_not(None), Product.is_active.is_(True)
    )
    if store_id is not None:
        product_query = product_query.where(Product.store_id == store_id)
    products = list(db.execute(product_query).scalars().all())
    positions = compute_positions_bulk(db, products)
    below_reorder = 0
    below_minimum = 0
    for product in products:
        pos = positions[product.id]
        if pos.position <= product.reorder_point:  # type: ignore[operator]
            below_reorder += 1
        if classify_urgency(product, pos.position) == "URGENT":
            below_minimum += 1

    return SupplyChainMetrics(
        store_id=store_id,
        open_purchase_order_qty=open_po_qty,
        inbound_transfer_qty=inbound_qty,
        products_below_reorder_point=below_reorder,
        products_below_minimum=below_minimum,
        overdue_purchase_order_count=len(overdue_pos),
    )


@dataclass(frozen=True)
class SupplyChainException:
    code: str
    product_id: int | None
    store_id: int | None
    purchase_order_id: int | None
    plan_id: int | None
    message: str


def get_exceptions(db: Session, *, store_id: int | None = None) -> list[SupplyChainException]:
    """A deliberately small, explainable exception list — every row here
    is a direct query result, never a heuristic. Does not include a
    generic workflow engine (task's own instruction)."""
    exceptions: list[SupplyChainException] = []
    today = date.today()

    po_query = select(PurchaseOrder).where(
        PurchaseOrder.status.in_(("ORDERED", "PARTIALLY_RECEIVED")),
        PurchaseOrder.expected_date.is_not(None),
        PurchaseOrder.expected_date < today,
    )
    if store_id is not None:
        po_query = po_query.where(PurchaseOrder.store_id == store_id)
    for po in db.execute(po_query).scalars().all():
        exceptions.append(
            SupplyChainException(
                code="PURCHASE_ORDER_OVERDUE",
                product_id=None,
                store_id=po.store_id,
                purchase_order_id=po.id,
                plan_id=po.replenishment_plan_id,
                message=(
                    f"Purchase order {po.purchase_number} was expected {po.expected_date} "
                    f"and is still {po.status}."
                ),
            )
        )

    product_query = select(Product).where(Product.is_active.is_(True))
    if store_id is not None:
        product_query = product_query.where(Product.store_id == store_id)
    for product in db.execute(product_query).scalars().all():
        if product.current_qty_on_hand <= 0:
            exceptions.append(
                SupplyChainException(
                    code="STOCKOUT",
                    product_id=product.id,
                    store_id=product.store_id,
                    purchase_order_id=None,
                    plan_id=None,
                    message=f"Product {product.sku} is at {product.current_qty_on_hand} on hand.",
                )
            )
        elif (
            product.minimum_stock_quantity is not None
            and product.current_qty_on_hand < product.minimum_stock_quantity
        ):
            exceptions.append(
                SupplyChainException(
                    code="BELOW_MINIMUM_STOCK",
                    product_id=product.id,
                    store_id=product.store_id,
                    purchase_order_id=None,
                    plan_id=None,
                    message=(
                        f"Product {product.sku} is at {product.current_qty_on_hand}, below its "
                        f"configured minimum of {product.minimum_stock_quantity}."
                    ),
                )
            )
        if product.default_supplier_id is not None:
            supplier = db.get(Supplier, product.default_supplier_id)
            if supplier is not None and not supplier.is_active:
                exceptions.append(
                    SupplyChainException(
                        code="PREFERRED_SUPPLIER_INACTIVE",
                        product_id=product.id,
                        store_id=product.store_id,
                        purchase_order_id=None,
                        plan_id=None,
                        message=(
                            f"Product {product.sku}'s preferred supplier "
                            f"{supplier.name} is inactive."
                        ),
                    )
                )
            elif (
                supplier is not None
                and get_current_supplier_product(db, supplier.id, product.id) is None
            ):
                exceptions.append(
                    SupplyChainException(
                        code="SUPPLIER_PRICE_MISSING",
                        product_id=product.id,
                        store_id=product.store_id,
                        purchase_order_id=None,
                        plan_id=None,
                        message=(
                            f"No active price is on file for {product.sku} from its "
                            f"preferred supplier {supplier.name}."
                        ),
                    )
                )

    stale_query = select(ReplenishmentPlan).where(ReplenishmentPlan.status == "STALE")
    if store_id is not None:
        stale_query = stale_query.where(ReplenishmentPlan.destination_store_id == store_id)
    for plan in db.execute(stale_query).scalars().all():
        exceptions.append(
            SupplyChainException(
                code="REPLENISHMENT_STALE",
                product_id=plan.product_id,
                store_id=plan.destination_store_id,
                purchase_order_id=None,
                plan_id=plan.id,
                message=plan.stale_reason or "Plan is stale.",
            )
        )

    return exceptions
