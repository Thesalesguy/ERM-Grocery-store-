"""Replenishment: a read-only suggested-reorder report.

See docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 11". This
module NEVER creates a PurchaseOrder or InterStoreTransfer itself — a
human always initiates the resulting document through the existing
app.modules.purchasing.service.create_purchase_order /
app.modules.transfers.service.create_transfer, using these numbers as
input. This keeps replenishment a pure decision-support read path with
zero risk of an automated process placing an order or moving stock
nobody asked for.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.products.models import Product
from app.modules.purchasing.models import PurchaseOrder, PurchaseOrderItem
from app.modules.transfers.models import InterStoreTransfer, InterStoreTransferLine

_OPEN_PO_STATUSES = ("DRAFT", "ORDERED", "PARTIALLY_RECEIVED")


@dataclass(frozen=True)
class ReplenishmentSuggestion:
    product_id: int
    store_id: int
    sku: str
    name: str
    reorder_point: Decimal
    current_qty_on_hand: Decimal
    inbound_transfer_qty: Decimal  # shipped-but-not-yet-received, inbound to this store
    open_purchase_order_qty: Decimal  # ordered-but-not-yet-received, for this store
    inventory_position: Decimal  # on_hand + inbound_transfer_qty + open_purchase_order_qty
    shortfall: Decimal  # max(0, reorder_point - inventory_position)
    # A shortfall is split, never left as one ambiguous number: however
    # much of it a sister store's surplus (above THAT store's own reorder
    # point) can cover is suggested as a transfer; the rest as a purchase.
    suggested_transfer_quantity: Decimal
    suggested_purchase_quantity: Decimal
    sister_store_surplus_source_store_id: int | None


def get_replenishment_suggestions(
    db: Session, *, store_id: int | None = None
) -> list[ReplenishmentSuggestion]:
    """One row per product whose inventory_position has fallen at or
    below its reorder_point. `inventory_position` (not raw on-hand) is
    the correct basis — counting only physical on-hand would suggest
    re-ordering stock that is already inbound, producing duplicate
    purchase orders/transfers for the same shortfall."""
    query = select(Product).where(Product.reorder_point.is_not(None), Product.is_active.is_(True))
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    products = list(db.execute(query).scalars().all())
    if not products:
        return []

    transfer_rows = db.execute(
        select(
            InterStoreTransferLine.destination_product_id,
            InterStoreTransferLine.shipped_quantity,
            InterStoreTransferLine.received_quantity,
        )
        .join(InterStoreTransfer, InterStoreTransfer.id == InterStoreTransferLine.transfer_id)
        .where(InterStoreTransfer.status == "SHIPPED")
    ).all()
    inbound_by_product: dict[int, Decimal] = {}
    for destination_product_id, shipped, received in transfer_rows:
        remaining = shipped - received
        if remaining > 0:
            inbound_by_product[destination_product_id] = (
                inbound_by_product.get(destination_product_id, Decimal("0")) + remaining
            )

    po_rows = db.execute(
        select(
            PurchaseOrderItem.product_id,
            PurchaseOrderItem.quantity_ordered,
            PurchaseOrderItem.quantity_received,
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(PurchaseOrder.status.in_(_OPEN_PO_STATUSES))
    ).all()
    open_po_by_product: dict[int, Decimal] = {}
    for product_id, ordered, received in po_rows:
        remaining = ordered - received
        if remaining > 0:
            open_po_by_product[product_id] = (
                open_po_by_product.get(product_id, Decimal("0")) + remaining
            )

    suggestions: list[ReplenishmentSuggestion] = []
    for product in products:
        inbound = inbound_by_product.get(product.id, Decimal("0"))
        open_po = open_po_by_product.get(product.id, Decimal("0"))
        position = product.current_qty_on_hand + inbound + open_po
        assert product.reorder_point is not None  # guaranteed by the query filter above
        shortfall = max(Decimal("0"), product.reorder_point - position)
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
                current_qty_on_hand=product.current_qty_on_hand,
                inbound_transfer_qty=inbound,
                open_purchase_order_qty=open_po,
                inventory_position=position,
                shortfall=shortfall,
                suggested_transfer_quantity=suggested_transfer,
                suggested_purchase_quantity=suggested_purchase,
                sister_store_surplus_source_store_id=surplus_source_store_id,
            )
        )
    return suggestions
