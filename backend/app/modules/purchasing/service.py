"""Supplier, purchase-order, goods-receiving, and purchase-return business
logic — the M3 purchasing/WAC transactional core.

`receive_goods` is the highest-stakes function here (same tier as
app.modules.sales.service.finalize_sale): it changes inventory quantity,
inventory valuation (Weighted Average Cost), and purchase-order state
together, atomically. See docs/M3_PURCHASING_RECEIVING_WAC.md for the
full design writeup — locking strategy, idempotency, WAC edge cases, the
over-receipt policy, and the purchase-return cost-basis limitation.

Two commit conventions coexist here, matching the two conventions already
established elsewhere in this codebase: standalone single-step operations
(supplier CRUD, PO create/submit/cancel — one row or one small atomic
change, no reason to defer) commit directly, the same way
app.modules.products.service does for product CRUD. `receive_goods` and
`create_purchase_return` — genuinely multi-row, multi-effect transactions
— never call commit()/rollback() themselves; the caller (a route handler)
commits once, after either returns successfully, the same way
app.modules.sales.service.finalize_sale works (BR-3: a failure anywhere
inside either rolls back everything, never a partial write).
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.audit import service as audit_service
from app.modules.auth.models import Store
from app.modules.inventory import service as inventory_service
from app.modules.products.models import Product
from app.modules.purchasing.models import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
)

_ACTIVE_PO_STATUSES_FOR_RECEIVING = ("ORDERED", "PARTIALLY_RECEIVED")
_CANCELLABLE_PO_STATUSES = ("DRAFT", "ORDERED", "PARTIALLY_RECEIVED")


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Mirrors app.modules.auth.service.enforce_store_access, duplicated
    here (rather than imported) so this module — like
    app.modules.sales.service — has no dependency on the auth module and
    stays testable by calling its functions directly, the same design
    already established for finalize_sale (M2 hardening audit Section
    12: this check must live in the service layer, not only the route,
    because tests and future concurrency/idempotency harnesses call
    these functions directly)."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


# --- Suppliers (global/shared reference data — see the model docstring) --


@dataclass(frozen=True)
class SupplierInput:
    name: str
    code: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    tax_id: str | None = None


def create_supplier(db: Session, data: SupplierInput, *, actor_id: int | None = None) -> Supplier:
    supplier = Supplier(
        name=data.name,
        code=data.code,
        contact_name=data.contact_name,
        phone=data.phone,
        email=data.email,
        address=data.address,
        tax_id=data.tax_id,
    )
    db.add(supplier)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Supplier code {data.code!r} is already in use", error_code="DUPLICATE_SUPPLIER_CODE"
        ) from exc
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SUPPLIER_CREATED",
        entity_type="supplier",
        entity_id=supplier.id,
        after={"name": supplier.name, "code": supplier.code},
    )
    db.commit()
    db.refresh(supplier)
    return supplier


def get_supplier(db: Session, supplier_id: int) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")
    return supplier


def list_suppliers(
    db: Session,
    *,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Supplier]:
    query = select(Supplier).order_by(Supplier.name).limit(limit).offset(offset)
    if is_active is not None:
        query = query.where(Supplier.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Supplier.name.ilike(pattern), Supplier.code.ilike(pattern)))
    return list(db.execute(query).scalars().all())


def update_supplier(
    db: Session, supplier_id: int, changes: dict, *, actor_id: int | None = None
) -> Supplier:
    supplier = get_supplier(db, supplier_id)
    before = {field: getattr(supplier, field) for field in changes}
    for field, value in changes.items():
        setattr(supplier, field, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            "Supplier code is already in use", error_code="DUPLICATE_SUPPLIER_CODE"
        ) from exc
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SUPPLIER_UPDATED",
        entity_type="supplier",
        entity_id=supplier.id,
        before=before,
        after=changes,
    )
    db.commit()
    db.refresh(supplier)
    return supplier


def set_supplier_active(
    db: Session, supplier_id: int, is_active: bool, *, actor_id: int | None = None
) -> Supplier:
    supplier = get_supplier(db, supplier_id)
    was_active = supplier.is_active
    supplier.is_active = is_active
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SUPPLIER_ACTIVATED" if is_active else "SUPPLIER_DEACTIVATED",
        entity_type="supplier",
        entity_id=supplier.id,
        before={"is_active": was_active},
        after={"is_active": is_active},
    )
    db.commit()
    db.refresh(supplier)
    return supplier


# --- Purchase orders -------------------------------------------------------


@dataclass(frozen=True)
class PurchaseOrderItemInput:
    product_id: int
    quantity_ordered: Decimal
    unit_cost: Decimal


def _validate_po_items(db: Session, store_id: int, lines: list[PurchaseOrderItemInput]) -> None:
    if not lines:
        raise ValidationAppError(
            "A purchase order must have at least one line", error_code="EMPTY_PURCHASE_ORDER"
        )
    for line in lines:
        if line.quantity_ordered <= 0:
            raise ValidationAppError(
                "Ordered quantity must be positive", error_code="INVALID_QUANTITY"
            )
        if line.unit_cost < 0:
            raise ValidationAppError("Unit cost cannot be negative", error_code="INVALID_UNIT_COST")
        product = db.get(Product, line.product_id)
        if product is None:
            raise ValidationAppError(
                f"Product {line.product_id} does not exist", error_code="INVALID_PRODUCT"
            )
        if product.store_id != store_id:
            raise ConflictError(
                f"Product {line.product_id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )


def _generate_purchase_number(store_id: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"PO{store_id}-{timestamp}-{secrets.token_hex(3).upper()}"


def create_purchase_order(
    db: Session,
    *,
    store_id: int,
    supplier_id: int,
    order_date: date,
    lines: list[PurchaseOrderItemInput],
    expected_date: date | None = None,
    notes: str | None = None,
    created_by: int | None = None,
    caller_store_id: int | None = None,
) -> PurchaseOrder:
    """Created in DRAFT — items are freely editable/re-creatable until
    `submit_purchase_order` moves it to ORDERED (docs/
    M3_PURCHASING_RECEIVING_WAC.md "PO lifecycle"). Never touches
    inventory (docs/TECHNICAL_BLUEPRINT.md Section F invariant, unchanged
    from M1) — only a goods receipt does that."""
    _enforce_store_access(caller_store_id, store_id, "purchase orders")

    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise ValidationAppError(
            f"Supplier {supplier_id} does not exist or is inactive", error_code="INVALID_SUPPLIER"
        )
    _validate_po_items(db, store_id, lines)

    purchase_order = PurchaseOrder(
        store_id=store_id,
        supplier_id=supplier_id,
        purchase_number=_generate_purchase_number(store_id),
        status="DRAFT",
        order_date=order_date,
        expected_date=expected_date,
        notes=notes,
        created_by=created_by,
    )
    db.add(purchase_order)
    db.flush()
    for line in lines:
        db.add(
            PurchaseOrderItem(
                purchase_order_id=purchase_order.id,
                product_id=line.product_id,
                quantity_ordered=line.quantity_ordered,
                quantity_received=0,
                unit_cost=line.unit_cost,
            )
        )
    audit_service.log_event(
        db,
        user_id=created_by,
        action="PURCHASE_ORDER_CREATED",
        entity_type="purchase_order",
        entity_id=purchase_order.id,
        after={
            "purchase_number": purchase_order.purchase_number,
            "supplier_id": supplier_id,
            "line_count": len(lines),
        },
    )
    db.commit()
    db.refresh(purchase_order)
    return purchase_order


def get_purchase_order(db: Session, purchase_order_id: int) -> PurchaseOrder:
    po = db.get(PurchaseOrder, purchase_order_id)
    if po is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    return po


def list_purchase_orders(
    db: Session,
    *,
    store_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PurchaseOrder]:
    query = (
        select(PurchaseOrder)
        .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(PurchaseOrder.store_id == store_id)
    if supplier_id is not None:
        query = query.where(PurchaseOrder.supplier_id == supplier_id)
    if status is not None:
        query = query.where(PurchaseOrder.status == status)
    return list(db.execute(query).scalars().all())


def submit_purchase_order(
    db: Session, purchase_order_id: int, *, actor_id: int | None, caller_store_id: int | None = None
) -> PurchaseOrder:
    """DRAFT -> ORDERED. Items are frozen (immutable) from this point on —
    only receiving may touch quantity_received; unit_cost/quantity_ordered
    never change again, preserving BR-2 for purchasing the same way a
    sale's frozen line items do."""
    po = get_purchase_order(db, purchase_order_id)
    _enforce_store_access(caller_store_id, po.store_id, "this purchase order")
    if po.status != "DRAFT":
        raise ConflictError(
            f"Purchase order {purchase_order_id} is {po.status}, not DRAFT — only a draft "
            "purchase order can be submitted",
            error_code="INVALID_PO_STATE",
        )
    po.status = "ORDERED"
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PURCHASE_ORDER_SUBMITTED",
        entity_type="purchase_order",
        entity_id=po.id,
        after={"status": "ORDERED"},
    )
    db.commit()
    db.refresh(po)
    return po


def cancel_purchase_order(
    db: Session,
    purchase_order_id: int,
    *,
    actor_id: int | None,
    reason: str | None = None,
    caller_store_id: int | None = None,
) -> PurchaseOrder:
    po = get_purchase_order(db, purchase_order_id)
    _enforce_store_access(caller_store_id, po.store_id, "this purchase order")
    if po.status not in _CANCELLABLE_PO_STATUSES:
        raise ConflictError(
            f"Purchase order {purchase_order_id} is {po.status} and cannot be cancelled "
            f"(only {', '.join(_CANCELLABLE_PO_STATUSES)} orders can be)",
            error_code="INVALID_PO_STATE",
        )
    before_status = po.status
    po.status = "CANCELLED"
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PURCHASE_ORDER_CANCELLED",
        entity_type="purchase_order",
        entity_id=po.id,
        before={"status": before_status},
        after={"status": "CANCELLED", "reason": reason},
    )
    db.commit()
    db.refresh(po)
    return po


# --- Goods receiving (the core transactional boundary) ---------------------


@dataclass(frozen=True)
class GoodsReceiptLineInput:
    purchase_order_item_id: int
    quantity_received: Decimal
    unit_cost: Decimal
    condition_notes: str | None = None


def receive_goods(
    db: Session,
    *,
    purchase_order_id: int,
    received_date: date,
    lines: list[GoodsReceiptLineInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    received_by: int | None = None,
    notes: str | None = None,
) -> GoodsReceipt:
    """Atomically: validate -> lock the PO row -> lock affected product
    rows (sorted, deadlock-safe) -> verify PO/line state -> post one
    inventory movement + WAC recompute per line -> update received
    quantities -> advance PO status -> audit -> return (caller commits).

    Locking strategy (docs/M3_PURCHASING_RECEIVING_WAC.md "Locking
    strategy" has the full writeup — this is the load-bearing summary):

    1. The PurchaseOrder row is locked FOR UPDATE first, before anything
       else. This is what actually prevents the lost-update race M1 had:
       two concurrent receipts against the SAME PO now serialize on this
       one lock, so `purchase_order_item.quantity_received +=` is always
       read-then-written by exactly one transaction at a time — never two
       transactions racing on the same stale read.
    2. Every distinct PRODUCT referenced by this receipt is then locked
       FOR UPDATE, in ascending product_id order regardless of line
       order — the same deadlock-safe pattern
       app.modules.sales.service.finalize_sale uses, so a receipt and a
       sale (or another receipt against a different PO) touching the
       same products can never deadlock by acquiring them in opposite
       order.

    Idempotency (docs/M3_PURCHASING_RECEIVING_WAC.md "Idempotency
    strategy"): mirrors finalize_sale exactly — an early lookup by
    `client_transaction_id` is the fast path for a sequential retry
    (lost response, double-click); the UNIQUE constraint on
    `goods_receipts.client_transaction_id` is the real guarantee,
    recovered from gracefully if a genuinely concurrent duplicate
    request loses the fast-path race.
    """
    if caller_store_id is not None:
        # Checked against the PO's store before it's even loaded further,
        # so a store-scoped user gets the same STORE_ACCESS_DENIED
        # whether the PO doesn't exist, exists in another store, or
        # exists in their own store but they got the ID wrong — no branch
        # here leaks which case it was.
        po_store_id = db.execute(
            select(PurchaseOrder.store_id).where(PurchaseOrder.id == purchase_order_id)
        ).scalar_one_or_none()
        if po_store_id is not None and po_store_id != caller_store_id:
            raise ForbiddenError(
                f"Your account is scoped to store {caller_store_id} and cannot receive "
                f"against a purchase order in store {po_store_id}",
                error_code="STORE_ACCESS_DENIED",
            )

    existing = db.execute(
        select(GoodsReceipt).where(GoodsReceipt.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if not lines:
        raise ValidationAppError(
            "A goods receipt must have at least one line", error_code="EMPTY_RECEIPT"
        )
    for line in lines:
        if line.quantity_received <= 0:
            raise ValidationAppError(
                "Received quantity must be positive", error_code="INVALID_QUANTITY"
            )
        if line.unit_cost < 0:
            raise ValidationAppError("Unit cost cannot be negative", error_code="INVALID_UNIT_COST")

    # --- Lock the PO row first: see the locking-strategy docstring above.
    purchase_order = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id).with_for_update()
    ).scalar_one_or_none()
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    if purchase_order.status not in _ACTIVE_PO_STATUSES_FOR_RECEIVING:
        raise ConflictError(
            f"Cannot receive goods against purchase order {purchase_order_id}: status is "
            f"{purchase_order.status} (must be ORDERED or PARTIALLY_RECEIVED)",
            error_code="INVALID_PO_STATE",
        )

    # --- Resolve and validate every line's PO item, summing duplicate
    # lines against the same item the same way finalize_sale sums
    # duplicate cart lines for the same product.
    po_items_by_id: dict[int, PurchaseOrderItem] = {}
    received_qty_by_item: dict[int, Decimal] = {}
    line_details: list[tuple[PurchaseOrderItem, Decimal, Decimal, str | None]] = []
    for line in lines:
        po_item = po_items_by_id.get(line.purchase_order_item_id) or db.get(
            PurchaseOrderItem, line.purchase_order_item_id
        )
        if po_item is None or po_item.purchase_order_id != purchase_order_id:
            raise NotFoundError(
                f"Purchase order item {line.purchase_order_item_id} not found on "
                f"purchase order {purchase_order_id}"
            )
        po_items_by_id[po_item.id] = po_item
        received_qty_by_item[po_item.id] = (
            received_qty_by_item.get(po_item.id, Decimal("0")) + line.quantity_received
        )
        line_details.append((po_item, line.quantity_received, line.unit_cost, line.condition_notes))

    # --- Lock every distinct product row, ascending id order (deadlock-
    # safe — see the locking-strategy docstring above).
    distinct_product_ids = sorted({item.product_id for item in po_items_by_id.values()})
    locked_products: dict[int, Product] = {
        product_id: inventory_service.lock_product_for_update(db, product_id)
        for product_id in distinct_product_ids
    }

    receipt = GoodsReceipt(
        purchase_order_id=purchase_order_id,
        store_id=purchase_order.store_id,
        client_transaction_id=client_transaction_id,
        received_date=received_date,
        received_by=received_by,
        notes=notes,
    )
    db.add(receipt)
    try:
        db.flush()
    except IntegrityError:
        # Genuinely concurrent duplicate submission — see finalize_sale's
        # identical recovery block for the full explanation. Rolling back
        # here also releases the PO/product locks this attempt acquired.
        db.rollback()
        winner = db.execute(
            select(GoodsReceipt).where(GoodsReceipt.client_transaction_id == client_transaction_id)
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    over_receipt_lines: list[int] = []
    received_value_lines: list[tuple[Decimal, Decimal]] = []
    for po_item, quantity_received, unit_cost, condition_notes in line_details:
        received_value_lines.append((quantity_received, unit_cost))
        product = locked_products[po_item.product_id]
        new_wac = inventory_service.compute_new_wac(
            existing_qty=product.current_qty_on_hand,
            existing_wac=product.current_cost,
            received_qty=quantity_received,
            received_unit_cost=unit_cost,
        )
        inventory_service.record_movement(
            db,
            product=product,
            store_id=purchase_order.store_id,
            movement_type="PURCHASE_RECEIPT",
            quantity_delta=quantity_received,
            unit_cost_at_movement=unit_cost,
            reference_type="purchase_order",
            reference_id=purchase_order.id,
            created_by=received_by,
            new_product_cost=new_wac,
        )
        db.add(
            GoodsReceiptItem(
                goods_receipt_id=receipt.id,
                purchase_order_item_id=po_item.id,
                quantity_received=quantity_received,
                unit_cost=unit_cost,
                condition_notes=condition_notes,
            )
        )

    # Apply the summed per-item totals once each (not once per input
    # line), now that every line for that item has been processed.
    for po_item_id, total_qty in received_qty_by_item.items():
        po_item = po_items_by_id[po_item_id]
        new_total = po_item.quantity_received + total_qty
        if new_total > po_item.quantity_ordered:
            # Allowed (docs/TECHNICAL_BLUEPRINT.md Section F: "over-receipt
            # allowed, flagged") — flagged via the audit event and the
            # response's is_over_receipt field, not blocked.
            over_receipt_lines.append(po_item_id)
        po_item.quantity_received = new_total

    _advance_purchase_order_status(purchase_order)

    audit_service.log_event(
        db,
        user_id=received_by,
        action="GOODS_RECEIPT_COMPLETED",
        entity_type="goods_receipt",
        entity_id=receipt.id,
        after={
            "purchase_order_id": purchase_order_id,
            "line_count": len(line_details),
            "over_receipt_line_item_ids": over_receipt_lines,
        },
    )

    # Accounting posting shares this same uncommitted transaction — see
    # app.modules.accounting.service.post_goods_receipt_journal's
    # docstring (docs/M4_ACCOUNTING_CORE.md Section 19).
    accounting_service.post_goods_receipt_journal(
        db,
        goods_receipt=receipt,
        received_lines=received_value_lines,
        created_by=received_by,
    )

    db.flush()
    return receipt


def _advance_purchase_order_status(purchase_order: PurchaseOrder) -> None:
    items = purchase_order.items
    if items and all(item.quantity_received >= item.quantity_ordered for item in items):
        purchase_order.status = "RECEIVED"
    elif any(item.quantity_received > 0 for item in items):
        purchase_order.status = "PARTIALLY_RECEIVED"


def get_goods_receipt(db: Session, goods_receipt_id: int) -> GoodsReceipt:
    receipt = db.get(GoodsReceipt, goods_receipt_id)
    if receipt is None:
        raise NotFoundError(f"Goods receipt {goods_receipt_id} not found")
    return receipt


def list_goods_receipts(
    db: Session, *, purchase_order_id: int | None = None, store_id: int | None = None
) -> list[GoodsReceipt]:
    query = select(GoodsReceipt).order_by(GoodsReceipt.created_at.desc())
    if purchase_order_id is not None:
        query = query.where(GoodsReceipt.purchase_order_id == purchase_order_id)
    if store_id is not None:
        query = query.where(GoodsReceipt.store_id == store_id)
    return list(db.execute(query).scalars().all())


# --- Purchase returns --------------------------------------------------


@dataclass(frozen=True)
class PurchaseReturnLineInput:
    product_id: int
    quantity: Decimal


def create_purchase_return(
    db: Session,
    *,
    purchase_order_id: int,
    store_id: int,
    return_date: date,
    lines: list[PurchaseReturnLineInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    reason: str | None = None,
    created_by: int | None = None,
) -> PurchaseReturn:
    """A return only ever REMOVES stock, so it reuses record_movement()
    exactly as a sale does — including never passing `new_product_cost`,
    which is what keeps WAC unchanged by a removal (WAC only moves on a
    receipt, by design; see compute_new_wac's docstring). Cost basis
    limitation: see PurchaseReturn's model docstring — this uses the
    product's CURRENT WAC as the returned unit's cost, since there is no
    per-receipt-lot tracking in this schema.
    """
    _enforce_store_access(caller_store_id, store_id, "purchase returns")

    existing = db.execute(
        select(PurchaseReturn).where(PurchaseReturn.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if not lines:
        raise ValidationAppError(
            "A purchase return must have at least one line", error_code="EMPTY_RETURN"
        )
    for line in lines:
        if line.quantity <= 0:
            raise ValidationAppError(
                "Return quantity must be positive", error_code="INVALID_QUANTITY"
            )

    purchase_order = db.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    if purchase_order.store_id != store_id:
        raise ConflictError(
            f"Purchase order {purchase_order_id} does not belong to store {store_id}",
            error_code="STORE_MISMATCH",
        )

    requested_qty: dict[int, Decimal] = {}
    for line in lines:
        requested_qty[line.product_id] = (
            requested_qty.get(line.product_id, Decimal("0")) + line.quantity
        )

    distinct_product_ids = sorted(requested_qty)
    locked_products: dict[int, Product] = {}
    for product_id in distinct_product_ids:
        product = inventory_service.lock_product_for_update(db, product_id)
        if product.store_id != store_id:
            raise ConflictError(
                f"Product {product_id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        locked_products[product_id] = product

    for product_id, qty in requested_qty.items():
        product = locked_products[product_id]
        if qty > product.current_qty_on_hand:
            raise ConflictError(
                f"Cannot return {qty} of product {product_id}: only "
                f"{product.current_qty_on_hand} on hand",
                error_code="INSUFFICIENT_STOCK",
            )

    purchase_return = PurchaseReturn(
        purchase_order_id=purchase_order_id,
        store_id=store_id,
        client_transaction_id=client_transaction_id,
        return_date=return_date,
        reason=reason,
        created_by=created_by,
    )
    db.add(purchase_return)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(PurchaseReturn).where(
                PurchaseReturn.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    returned_value_lines: list[tuple[Decimal, Decimal]] = []
    for line in lines:
        product = locked_products[line.product_id]
        return_unit_cost = product.current_cost
        returned_value_lines.append((line.quantity, return_unit_cost))
        inventory_service.record_movement(
            db,
            product=product,
            store_id=store_id,
            movement_type="PURCHASE_RETURN",
            quantity_delta=-line.quantity,
            unit_cost_at_movement=return_unit_cost,
            reference_type="purchase_return",
            reference_id=purchase_return.id,
            created_by=created_by,
        )
        db.add(
            PurchaseReturnItem(
                purchase_return_id=purchase_return.id,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_cost=return_unit_cost,
            )
        )

    audit_service.log_event(
        db,
        user_id=created_by,
        action="PURCHASE_RETURN_COMPLETED",
        entity_type="purchase_return",
        entity_id=purchase_return.id,
        after={"purchase_order_id": purchase_order_id, "line_count": len(lines)},
    )

    accounting_service.post_purchase_return_journal(
        db,
        purchase_return=purchase_return,
        returned_lines=returned_value_lines,
        created_by=created_by,
    )

    db.flush()
    return purchase_return


def get_purchase_return(db: Session, purchase_return_id: int) -> PurchaseReturn:
    purchase_return = db.get(PurchaseReturn, purchase_return_id)
    if purchase_return is None:
        raise NotFoundError(f"Purchase return {purchase_return_id} not found")
    return purchase_return
