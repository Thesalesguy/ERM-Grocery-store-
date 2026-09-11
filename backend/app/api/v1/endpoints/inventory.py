"""Inventory endpoints: stock levels, the movement ledger, and manual
stock adjustments.

`inventory.read` gates every GET; `inventory.adjust` additionally gates
the adjustment endpoint (see app.modules.auth.permissions).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import INVENTORY_ADJUST, INVENTORY_READ
from app.modules.auth.service import (
    CurrentUser,
    enforce_store_access,
    require_permission,
    scoped_store_filter,
)
from app.modules.inventory import service
from app.modules.inventory.schemas import (
    InventoryMovementRead,
    StockAdjustmentCreate,
    StockAdjustmentRead,
    StockLevelRead,
)
from app.modules.products.models import Product

router = APIRouter(prefix="/inventory", tags=["inventory"])

_read_permission = require_permission(INVENTORY_READ)
_adjust_permission = require_permission(INVENTORY_ADJUST)


def _to_stock_level(product: Product) -> StockLevelRead:
    is_low_stock = (
        product.reorder_point is not None and product.current_qty_on_hand <= product.reorder_point
    )
    return StockLevelRead(
        id=product.id,
        store_id=product.store_id,
        sku=product.sku,
        name=product.name,
        current_qty_on_hand=product.current_qty_on_hand,
        current_cost=product.current_cost,
        reorder_point=product.reorder_point,
        is_low_stock=is_low_stock,
        is_active=product.is_active,
    )


@router.get("/stock", response_model=list[StockLevelRead])
def list_stock(
    store_id: int | None = None,
    search: str | None = None,
    low_stock_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[StockLevelRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    products = service.list_stock_levels(
        db,
        store_id=effective_store_id,
        search=search,
        low_stock_only=low_stock_only,
        limit=limit,
        offset=offset,
    )
    return [_to_stock_level(p) for p in products]


@router.get("/stock/{product_id}", response_model=StockLevelRead)
def get_stock(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> StockLevelRead:
    product = service.get_stock_level(db, product_id)
    if current_user.store_id is not None and current_user.store_id != product.store_id:
        raise NotFoundError(f"Product {product_id} not found")
    return _to_stock_level(product)


@router.get("/movements", response_model=list[InventoryMovementRead])
def list_movements(
    product_id: int | None = None,
    movement_type: str | None = None,
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[InventoryMovementRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    movements = service.list_movements(
        db,
        product_id=product_id,
        movement_type=movement_type,
        store_id=effective_store_id,
        limit=limit,
        offset=offset,
    )
    return [InventoryMovementRead.model_validate(m) for m in movements]


@router.post("/adjustments", response_model=StockAdjustmentRead, status_code=201)
def create_adjustment(
    payload: StockAdjustmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_adjust_permission),
) -> StockAdjustmentRead:
    product = service.get_stock_level(db, payload.product_id)
    # A store-scoped user cannot adjust another store's stock just by
    # naming its product_id (M2 hardening audit Section 12).
    enforce_store_access(current_user, product.store_id)
    adjustment = service.create_stock_adjustment(
        db,
        store_id=product.store_id,
        product_id=payload.product_id,
        quantity_delta=payload.quantity_delta,
        reason_code=payload.reason_code,
        notes=payload.notes,
        created_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(adjustment)
    return StockAdjustmentRead.model_validate(adjustment)
