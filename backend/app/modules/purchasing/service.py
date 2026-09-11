"""Goods-receiving transactional slice.

This is the one real vertical slice of business logic built in M1 (per
the task's instruction to prepare, but not fully build, the transactional
architecture): receiving goods against a purchase order, atomically:

1. locks the affected product row(s),
2. recomputes Weighted Average Cost from the actual received cost,
3. posts a PURCHASE_RECEIPT inventory movement per line,
4. updates the purchase order item's received quantity, and
5. advances the purchase order's status —

all inside the caller's ambient DB transaction (this module never calls
commit()), so a failure on any line rolls back the entire receipt rather
than partially applying it (BR-3). A purchase order by itself never
touches inventory — only a GoodsReceiptItem does (docs/TECHNICAL_BLUEPRINT.md
Section F).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.inventory import service as inventory_service
from app.modules.purchasing.models import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
)


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
    received_by: int | None = None,
    notes: str | None = None,
) -> GoodsReceipt:
    if not lines:
        raise ConflictError("A goods receipt must have at least one line")

    purchase_order = db.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    if purchase_order.status == "CANCELLED":
        raise ConflictError("Cannot receive goods against a cancelled purchase order")

    receipt = GoodsReceipt(
        purchase_order_id=purchase_order_id,
        received_date=received_date,
        received_by=received_by,
        notes=notes,
    )
    db.add(receipt)
    db.flush()

    for line in lines:
        po_item = db.get(PurchaseOrderItem, line.purchase_order_item_id)
        if po_item is None or po_item.purchase_order_id != purchase_order_id:
            raise NotFoundError(
                f"Purchase order item {line.purchase_order_item_id} not found on "
                f"purchase order {purchase_order_id}"
            )

        product = inventory_service.lock_product_for_update(db, po_item.product_id)
        new_wac = inventory_service.compute_new_wac(
            existing_qty=product.current_qty_on_hand,
            existing_wac=product.current_cost,
            received_qty=line.quantity_received,
            received_unit_cost=line.unit_cost,
        )
        inventory_service.record_movement(
            db,
            product=product,
            store_id=purchase_order.store_id,
            movement_type="PURCHASE_RECEIPT",
            quantity_delta=line.quantity_received,
            unit_cost_at_movement=line.unit_cost,
            reference_type="purchase_order",
            reference_id=purchase_order.id,
            created_by=received_by,
            new_product_cost=new_wac,
        )

        db.add(
            GoodsReceiptItem(
                goods_receipt_id=receipt.id,
                purchase_order_item_id=po_item.id,
                quantity_received=line.quantity_received,
                unit_cost=line.unit_cost,
                condition_notes=line.condition_notes,
            )
        )
        po_item.quantity_received = po_item.quantity_received + line.quantity_received

    _advance_purchase_order_status(db, purchase_order)
    db.flush()
    return receipt


def _advance_purchase_order_status(db: Session, purchase_order: PurchaseOrder) -> None:
    items = (
        db.execute(
            select(PurchaseOrderItem).where(
                PurchaseOrderItem.purchase_order_id == purchase_order.id
            )
        )
        .scalars()
        .all()
    )
    if items and all(item.quantity_received >= item.quantity_ordered for item in items):
        purchase_order.status = "RECEIVED"
    elif any(item.quantity_received > 0 for item in items):
        purchase_order.status = "PARTIALLY_RECEIVED"
