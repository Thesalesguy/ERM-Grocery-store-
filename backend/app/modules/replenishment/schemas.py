"""API-contract schemas for M8's read-only suggestions report and M9's
persisted supplier-product catalog / replenishment-plan lifecycle. See
docs/M9_SUPPLY_CHAIN_DESIGN.md for the design behind every field here."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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


# --- M9: supplier-product catalog / pricing --------------------------------


class SupplierProductCreate(BaseModel):
    supplier_id: int
    product_id: int
    supplier_sku: str | None = Field(default=None, max_length=64)
    pack_size: Decimal = Field(default=Decimal("1"), gt=0)
    unit_cost: Decimal = Field(ge=0)
    minimum_order_quantity: Decimal | None = Field(default=None, gt=0)
    lead_time_days: int | None = Field(default=None, ge=0)
    effective_date: date = Field(default_factory=date.today)


class SupplierProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    product_id: int
    supplier_sku: str | None
    pack_size: Decimal
    unit_cost: Decimal
    minimum_order_quantity: Decimal | None
    lead_time_days: int | None
    effective_date: date
    is_active: bool


# --- M9: replenishment plans -------------------------------------------


class ReplenishmentPlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    generation_batch_id: str
    destination_store_id: int
    product_id: int
    needed_quantity: Decimal
    suggested_quantity: Decimal
    source_type: str
    supplier_id: int | None
    source_store_id: int | None
    suggested_unit_cost: Decimal | None
    urgency: str
    reason: str
    status: str

    approved_by: int | None
    approved_at: datetime | None

    executed_by: int | None
    executed_at: datetime | None
    executed_quantity: Decimal | None
    executed_unit_cost: Decimal | None
    generated_purchase_order_id: int | None
    generated_transfer_id: int | None

    stale_detected_at: datetime | None
    stale_reason: str | None

    cancelled_by: int | None
    cancelled_at: datetime | None
    cancellation_reason: str | None

    created_at: datetime


class ReplenishmentPlanDetailRead(ReplenishmentPlanRead):
    """Adds the two values that are always computed on read, never stored
    (Design Decisions 4/5) — never confuse these with a plan's own stored
    columns above."""

    remaining_need: Decimal
    fulfilled: bool | None


class GeneratePlansRequest(BaseModel):
    store_id: int | None = None
    product_ids: list[int] | None = None


class ExecutePlanRequest(BaseModel):
    client_transaction_id: str = Field(min_length=1, max_length=100)


class CancelPlanRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


# --- M9: metrics and exceptions -----------------------------------------


class SupplyChainMetricsRead(BaseModel):
    store_id: int | None
    open_purchase_order_qty: Decimal
    inbound_transfer_qty: Decimal
    products_below_reorder_point: int
    products_below_minimum: int
    overdue_purchase_order_count: int


class SupplyChainExceptionRead(BaseModel):
    code: str
    product_id: int | None
    store_id: int | None
    purchase_order_id: int | None
    plan_id: int | None
    message: str
