"""Replenishment and supply-chain planning.

`supply_chain.read` gates every GET (suggestions report, supplier-product
catalog, plans, metrics, exceptions). `supply_chain.plan` gates generating
recommendations, the supplier-product catalog write, and cancelling a
plan. `supply_chain.approve` gates approval. `supply_chain.execute` gates
turning an approved plan into a real purchase order or transfer — see
docs/M9_SUPPLY_CHAIN_DESIGN.md "design answer #19" for why these are four
separate permissions rather than one.

This module never exposes its own purchase-order/transfer read or mutate
endpoints: a plan's generated document is fetched via the EXISTING
/purchasing/purchase-orders/{id} or /transfers/{id} endpoints
(`generated_purchase_order_id`/`generated_transfer_id` on the plan is the
link), per the task's explicit "never duplicate existing PO/transfer
endpoints" instruction."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import (
    SUPPLY_CHAIN_APPROVE,
    SUPPLY_CHAIN_EXECUTE,
    SUPPLY_CHAIN_PLAN,
    SUPPLY_CHAIN_READ,
)
from app.modules.auth.service import CurrentUser, require_permission, scoped_store_filter
from app.modules.replenishment import service
from app.modules.replenishment.models import ReplenishmentPlan
from app.modules.replenishment.schemas import (
    CancelPlanRequest,
    ExecutePlanRequest,
    GeneratePlansRequest,
    ReplenishmentPlanDetailRead,
    ReplenishmentPlanRead,
    ReplenishmentSuggestionRead,
    SupplierProductCreate,
    SupplierProductRead,
    SupplyChainExceptionRead,
    SupplyChainMetricsRead,
)

router = APIRouter(prefix="/replenishment", tags=["replenishment"])

_read_permission = require_permission(SUPPLY_CHAIN_READ)
_plan_permission = require_permission(SUPPLY_CHAIN_PLAN)
_approve_permission = require_permission(SUPPLY_CHAIN_APPROVE)
_execute_permission = require_permission(SUPPLY_CHAIN_EXECUTE)


# --- M8: read-only suggested-reorder report ---------------------------------


@router.get("/suggestions", response_model=list[ReplenishmentSuggestionRead])
def get_replenishment_suggestions(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ReplenishmentSuggestionRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    suggestions = service.get_replenishment_suggestions(db, store_id=effective_store_id)
    return [ReplenishmentSuggestionRead(**s.__dict__) for s in suggestions]


# --- M9: supplier-product catalog / pricing --------------------------------


@router.post(
    "/supplier-products", response_model=SupplierProductRead, status_code=status.HTTP_201_CREATED
)
def create_supplier_product(
    payload: SupplierProductCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_plan_permission),
) -> SupplierProductRead:
    row = service.create_supplier_product(
        db,
        supplier_id=payload.supplier_id,
        product_id=payload.product_id,
        supplier_sku=payload.supplier_sku,
        pack_size=payload.pack_size,
        unit_cost=payload.unit_cost,
        minimum_order_quantity=payload.minimum_order_quantity,
        lead_time_days=payload.lead_time_days,
        effective_date=payload.effective_date,
        actor_id=current_user.id,
    )
    return SupplierProductRead.model_validate(row)


@router.get("/supplier-products", response_model=list[SupplierProductRead])
def list_supplier_products(
    supplier_id: int | None = None,
    product_id: int | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierProductRead]:
    rows = service.list_supplier_products(
        db,
        supplier_id=supplier_id,
        product_id=product_id,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )
    return [SupplierProductRead.model_validate(r) for r in rows]


# --- M9: replenishment plans ---------------------------------------------


def _to_plan_detail(db: Session, plan: ReplenishmentPlan) -> ReplenishmentPlanDetailRead:
    base = ReplenishmentPlanRead.model_validate(plan)
    return ReplenishmentPlanDetailRead(
        **base.model_dump(),
        remaining_need=service.get_remaining_need(db, plan),
        fulfilled=service.is_plan_fulfilled(db, plan),
    )


@router.post("/plans/generate", response_model=list[ReplenishmentPlanRead])
def generate_replenishment_plans(
    payload: GeneratePlansRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_plan_permission),
) -> list[ReplenishmentPlanRead]:
    plans = service.generate_replenishment_plans(
        db,
        store_id=payload.store_id,
        product_ids=payload.product_ids,
        created_by=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return [ReplenishmentPlanRead.model_validate(p) for p in plans]


@router.get("/plans", response_model=list[ReplenishmentPlanRead])
def list_replenishment_plans(
    store_id: int | None = None,
    status_filter: str | None = None,
    generation_batch_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ReplenishmentPlanRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    plans = service.list_plans(
        db,
        store_id=effective_store_id,
        status=status_filter,
        generation_batch_id=generation_batch_id,
        limit=limit,
        offset=offset,
    )
    return [ReplenishmentPlanRead.model_validate(p) for p in plans]


def _get_plan_with_store_check(
    db: Session, plan_id: int, current_user: CurrentUser
) -> ReplenishmentPlan:
    plan = service.get_plan(db, plan_id)
    if current_user.store_id is not None:
        allowed = current_user.store_id == plan.destination_store_id or (
            plan.source_type == "TRANSFER" and current_user.store_id == plan.source_store_id
        )
        if not allowed:
            raise NotFoundError(f"Replenishment plan {plan_id} not found")
    return plan


@router.get("/plans/{plan_id}", response_model=ReplenishmentPlanDetailRead)
def get_replenishment_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ReplenishmentPlanDetailRead:
    plan = _get_plan_with_store_check(db, plan_id, current_user)
    return _to_plan_detail(db, plan)


@router.post("/plans/{plan_id}/approve", response_model=ReplenishmentPlanRead)
def approve_replenishment_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_approve_permission),
) -> ReplenishmentPlanRead:
    plan = service.approve_plan(
        db, plan_id, actor_id=current_user.id, caller_store_id=current_user.store_id
    )
    return ReplenishmentPlanRead.model_validate(plan)


@router.post("/plans/{plan_id}/execute", response_model=ReplenishmentPlanRead)
def execute_replenishment_plan(
    plan_id: int,
    payload: ExecutePlanRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_execute_permission),
) -> ReplenishmentPlanRead:
    plan = service.execute_plan(
        db,
        plan_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        client_transaction_id=payload.client_transaction_id,
    )
    return ReplenishmentPlanRead.model_validate(plan)


@router.post("/plans/{plan_id}/cancel", response_model=ReplenishmentPlanRead)
def cancel_replenishment_plan(
    plan_id: int,
    payload: CancelPlanRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_plan_permission),
) -> ReplenishmentPlanRead:
    plan = service.cancel_plan(
        db,
        plan_id,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
        reason=payload.reason,
    )
    return ReplenishmentPlanRead.model_validate(plan)


# --- M9: metrics and exceptions -----------------------------------------


@router.get("/metrics", response_model=SupplyChainMetricsRead)
def get_supply_chain_metrics(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplyChainMetricsRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    metrics = service.get_supply_chain_metrics(db, store_id=effective_store_id)
    return SupplyChainMetricsRead(**metrics.__dict__)


@router.get("/exceptions", response_model=list[SupplyChainExceptionRead])
def get_supply_chain_exceptions(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplyChainExceptionRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    exceptions = service.get_exceptions(db, store_id=effective_store_id)
    return [SupplyChainExceptionRead(**e.__dict__) for e in exceptions]
