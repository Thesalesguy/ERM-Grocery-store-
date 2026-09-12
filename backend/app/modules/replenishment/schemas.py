"""API-contract schema for the read-only replenishment suggestions report.
See app.modules.replenishment.service's module docstring: this never
creates a PurchaseOrder or InterStoreTransfer itself."""

from decimal import Decimal

from pydantic import BaseModel


class ReplenishmentSuggestionRead(BaseModel):
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
