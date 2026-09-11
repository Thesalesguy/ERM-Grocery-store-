"""Product catalog business logic.

Route handlers call these functions and translate their results/errors to
HTTP — they never talk to SQLAlchemy or touch the database directly (M1
task Section 17: keep business logic out of route handlers).
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.products.models import Product
from app.modules.products.schemas import ProductCreate


def create_product(db: Session, data: ProductCreate) -> Product:
    product = Product(
        store_id=data.store_id,
        sku=data.sku,
        name=data.name,
        description=data.description,
        category_id=data.category_id,
        default_supplier_id=data.default_supplier_id,
        unit_of_measure=data.unit_of_measure,
        is_weighed=data.is_weighed,
        current_price=data.current_price,
        tax_rate_id=data.tax_rate_id,
        reorder_point=data.reorder_point,
        allow_negative_stock=data.allow_negative_stock,
        # Deliberately not from `data`: cost and on-hand quantity are
        # system-managed and only ever move through inventory movements.
        current_cost=0,
        current_qty_on_hand=0,
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Product with SKU {data.sku!r} already exists for store {data.store_id}",
            error_code="DUPLICATE_SKU",
        ) from exc
    db.refresh(product)
    return product


def get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")
    return product


def list_products(
    db: Session, *, store_id: int | None = None, limit: int = 50, offset: int = 0
) -> list[Product]:
    query = select(Product).order_by(Product.id).limit(limit).offset(offset)
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    return list(db.execute(query).scalars().all())
