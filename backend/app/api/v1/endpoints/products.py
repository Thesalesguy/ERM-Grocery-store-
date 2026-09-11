"""Product catalog endpoints.

Every route is authenticated; write routes additionally require
`products.write` and read routes require `products.read` (see
app.modules.auth.permissions for the full matrix and
docs/M2_AUTH_AND_POS.md for the documented rationale).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.auth.permissions import PRODUCTS_READ, PRODUCTS_WRITE
from app.modules.auth.service import require_permission
from app.modules.products import service
from app.modules.products.schemas import (
    ProductBarcodeCreate,
    ProductBarcodeRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])

_read = Depends(require_permission(PRODUCTS_READ))
_write = Depends(require_permission(PRODUCTS_WRITE))


@router.post(
    "", response_model=ProductRead, status_code=status.HTTP_201_CREATED, dependencies=[_write]
)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)) -> ProductRead:
    product = service.create_product(db, payload)
    return ProductRead.model_validate(product)


@router.get("", response_model=list[ProductRead], dependencies=[_read])
def list_products(
    store_id: int | None = None,
    category_id: int | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[ProductRead]:
    products = service.list_products(
        db,
        store_id=store_id,
        category_id=category_id,
        is_active=is_active,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ProductRead.model_validate(p) for p in products]


@router.get("/barcode/{barcode}", response_model=ProductRead, dependencies=[_read])
def get_product_by_barcode(barcode: str, db: Session = Depends(get_db)) -> ProductRead:
    """The POS scan-to-lookup endpoint. Backed by product_barcodes'
    UNIQUE(barcode) index — a single indexed equality lookup, no scan."""
    product = service.get_product_by_barcode(db, barcode)
    return ProductRead.model_validate(product)


@router.get("/{product_id}", response_model=ProductRead, dependencies=[_read])
def get_product(product_id: int, db: Session = Depends(get_db)) -> ProductRead:
    product = service.get_product(db, product_id)
    return ProductRead.model_validate(product)


@router.put("/{product_id}", response_model=ProductRead, dependencies=[_write])
def update_product(
    product_id: int, payload: ProductUpdate, db: Session = Depends(get_db)
) -> ProductRead:
    product = service.update_product(db, product_id, payload)
    return ProductRead.model_validate(product)


@router.post("/{product_id}/activate", response_model=ProductRead, dependencies=[_write])
def activate_product(product_id: int, db: Session = Depends(get_db)) -> ProductRead:
    product = service.set_product_active(db, product_id, True)
    return ProductRead.model_validate(product)


@router.post("/{product_id}/deactivate", response_model=ProductRead, dependencies=[_write])
def deactivate_product(product_id: int, db: Session = Depends(get_db)) -> ProductRead:
    product = service.set_product_active(db, product_id, False)
    return ProductRead.model_validate(product)


@router.post(
    "/{product_id}/barcodes",
    response_model=ProductBarcodeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_write],
)
def add_barcode(
    product_id: int, payload: ProductBarcodeCreate, db: Session = Depends(get_db)
) -> ProductBarcodeRead:
    barcode = service.add_barcode(db, product_id, payload)
    return ProductBarcodeRead.model_validate(barcode)
