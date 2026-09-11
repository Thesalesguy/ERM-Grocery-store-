"""Product catalog endpoints.

Minimal by design (M1 task Section 20: "M1 does not require complete CRUD
APIs... only create endpoints necessary for proving the foundation").
This proves the model/schema/service/route separation end to end; the
full Products screen's CRUD and barcode management remain for a later
milestone.

No authentication/authorization is enforced yet — see
docs/M1_DATABASE_DESIGN.md "Authentication timing" for why that's
deliberately still deferred.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.products import service
from app.modules.products.schemas import ProductCreate, ProductRead

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)) -> ProductRead:
    product = service.create_product(db, payload)
    return ProductRead.model_validate(product)


@router.get("", response_model=list[ProductRead])
def list_products(
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[ProductRead]:
    products = service.list_products(db, store_id=store_id, limit=limit, offset=offset)
    return [ProductRead.model_validate(p) for p in products]


@router.get("/{product_id}", response_model=ProductRead)
def get_product(product_id: int, db: Session = Depends(get_db)) -> ProductRead:
    product = service.get_product(db, product_id)
    return ProductRead.model_validate(product)
