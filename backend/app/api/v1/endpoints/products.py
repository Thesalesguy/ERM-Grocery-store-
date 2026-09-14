"""Product catalog endpoints.

Every route is authenticated; write routes additionally require
`products.write` and read routes require `products.read` (see
app.modules.auth.permissions for the full matrix and
docs/M2_AUTH_AND_POS.md for the documented rationale).
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import PRODUCTS_READ, PRODUCTS_WRITE
from app.modules.auth.service import (
    CurrentUser,
    enforce_store_access,
    require_permission,
    scoped_store_filter,
)
from app.modules.products import service
from app.modules.products.schemas import (
    ProductBarcodeCreate,
    ProductBarcodeRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])

_read_permission = require_permission(PRODUCTS_READ)
_write_permission = require_permission(PRODUCTS_WRITE)


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> ProductRead:
    # A store-scoped user (e.g. an Inventory Clerk assigned to one store)
    # can only create products for their own store — otherwise the
    # store_id field in the request body would let them populate another
    # store's catalog (M2 hardening audit Section 12).
    enforce_store_access(current_user, payload.store_id)
    product = service.create_product(db, payload, actor_id=current_user.id)
    return ProductRead.model_validate(product)


@router.get("", response_model=list[ProductRead])
def list_products(
    store_id: int | None = None,
    category_id: int | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ProductRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    products = service.list_products(
        db,
        store_id=effective_store_id,
        category_id=category_id,
        is_active=is_active,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ProductRead.model_validate(p) for p in products]


def _check_product_store_access(current_user: CurrentUser, product_store_id: int) -> None:
    # 404, not 403: a store-scoped user shouldn't be able to tell "this
    # product exists in another store" from "this product doesn't exist
    # at all" just by trying an ID/barcode (M2 hardening audit Section 12).
    if current_user.store_id is not None and current_user.store_id != product_store_id:
        raise NotFoundError("Product not found")


@router.get("/barcode/{barcode}", response_model=ProductRead)
def get_product_by_barcode(
    barcode: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ProductRead:
    """The POS scan-to-lookup endpoint. Backed by product_barcodes'
    UNIQUE(barcode) index — a single indexed equality lookup, no scan."""
    product = service.get_product_by_barcode(db, barcode)
    _check_product_store_access(current_user, product.store_id)
    return ProductRead.model_validate(product)


@router.get("/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ProductRead:
    product = service.get_product(db, product_id)
    _check_product_store_access(current_user, product.store_id)
    return ProductRead.model_validate(product)


@router.put("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> ProductRead:
    existing = service.get_product(db, product_id)
    _check_product_store_access(current_user, existing.store_id)
    product = service.update_product(db, product_id, payload, actor_id=current_user.id)
    return ProductRead.model_validate(product)


@router.post("/{product_id}/activate", response_model=ProductRead)
def activate_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> ProductRead:
    existing = service.get_product(db, product_id)
    _check_product_store_access(current_user, existing.store_id)
    product = service.set_product_active(db, product_id, True, actor_id=current_user.id)
    return ProductRead.model_validate(product)


@router.post("/{product_id}/deactivate", response_model=ProductRead)
def deactivate_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> ProductRead:
    existing = service.get_product(db, product_id)
    _check_product_store_access(current_user, existing.store_id)
    product = service.set_product_active(db, product_id, False, actor_id=current_user.id)
    return ProductRead.model_validate(product)


@router.post(
    "/{product_id}/barcodes",
    response_model=ProductBarcodeRead,
    status_code=status.HTTP_201_CREATED,
)
def add_barcode(
    product_id: int,
    payload: ProductBarcodeCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> ProductBarcodeRead:
    existing = service.get_product(db, product_id)
    _check_product_store_access(current_user, existing.store_id)
    barcode = service.add_barcode(db, product_id, payload)
    return ProductBarcodeRead.model_validate(barcode)
