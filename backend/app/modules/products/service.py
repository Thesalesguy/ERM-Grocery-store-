"""Product catalog business logic.

Route handlers call these functions and translate their results/errors to
HTTP — they never talk to SQLAlchemy or touch the database directly (M1
task Section 17 / M2 task Section 19: keep business logic out of route
handlers).

Price-change policy (M2 task Section 5): `update_product` changes only
`products.current_price` (and other catalog fields) — the *reference*
value used for *future* sales. It never touches any `sale_items` row.
Historical completed sales are already immune to this by construction:
`sale_items.unit_price_at_sale` is frozen at finalization time (see
app.modules.sales.service.finalize_sale) and is never recomputed from the
product's current price. A regression test
(tests/test_products.py::test_price_change_does_not_affect_historical_sale)
proves this end to end.
"""

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.modules.auth.models import Store
from app.modules.products.models import Product, ProductBarcode, ProductCategory
from app.modules.products.schemas import ProductBarcodeCreate, ProductCreate, ProductUpdate
from app.modules.purchasing.models import Supplier
from app.modules.tax.models import TaxRate


def _validate_references(
    db: Session,
    *,
    store_id: int | None = None,
    category_id: int | None = None,
    supplier_id: int | None = None,
    tax_rate_id: int | None = None,
) -> None:
    """Checked explicitly, up front, rather than left to a generic
    IntegrityError catch — so a bad category/tax reference is reported as
    exactly that, not misdiagnosed as a duplicate-SKU conflict."""
    if store_id is not None and db.get(Store, store_id) is None:
        raise ValidationAppError(f"Store {store_id} does not exist", error_code="INVALID_STORE")
    if category_id is not None and db.get(ProductCategory, category_id) is None:
        raise ValidationAppError(
            f"Category {category_id} does not exist", error_code="INVALID_CATEGORY"
        )
    if supplier_id is not None and db.get(Supplier, supplier_id) is None:
        raise ValidationAppError(
            f"Supplier {supplier_id} does not exist", error_code="INVALID_SUPPLIER"
        )
    if tax_rate_id is not None and db.get(TaxRate, tax_rate_id) is None:
        raise ValidationAppError(
            f"Tax rate {tax_rate_id} does not exist", error_code="INVALID_TAX_RATE"
        )


def create_product(db: Session, data: ProductCreate) -> Product:
    _validate_references(
        db,
        store_id=data.store_id,
        category_id=data.category_id,
        supplier_id=data.default_supplier_id,
        tax_rate_id=data.tax_rate_id,
    )
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


def get_product_by_barcode(db: Session, barcode: str) -> Product:
    """The POS scan-to-lookup path — must stay index-backed (product_barcodes.barcode
    has a UNIQUE constraint, which Postgres backs with an index automatically)."""
    row = db.execute(
        select(ProductBarcode).where(ProductBarcode.barcode == barcode)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            f"No product found for barcode {barcode!r}", error_code="UNKNOWN_BARCODE"
        )
    return get_product(db, row.product_id)


def list_products(
    db: Session,
    *,
    store_id: int | None = None,
    category_id: int | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Product]:
    query = select(Product).order_by(Product.id).limit(limit).offset(offset)
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    if category_id is not None:
        query = query.where(Product.category_id == category_id)
    if is_active is not None:
        query = query.where(Product.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(Product.name.ilike(pattern), Product.sku.ilike(pattern)))
    return list(db.execute(query).scalars().all())


def update_product(db: Session, product_id: int, data: ProductUpdate) -> Product:
    product = get_product(db, product_id)
    changes = data.model_dump(exclude_unset=True)
    _validate_references(
        db,
        category_id=changes.get("category_id"),
        supplier_id=changes.get("default_supplier_id"),
        tax_rate_id=changes.get("tax_rate_id"),
    )
    for field, value in changes.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return product


def set_product_active(db: Session, product_id: int, is_active: bool) -> Product:
    product = get_product(db, product_id)
    product.is_active = is_active
    db.commit()
    db.refresh(product)
    return product


def add_barcode(db: Session, product_id: int, data: ProductBarcodeCreate) -> ProductBarcode:
    get_product(db, product_id)  # 404 if the product doesn't exist
    barcode = ProductBarcode(
        product_id=product_id,
        barcode=data.barcode,
        barcode_type=data.barcode_type,
        pack_quantity=data.pack_quantity,
        is_primary=data.is_primary,
    )
    db.add(barcode)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Barcode {data.barcode!r} is already assigned to a product",
            error_code="DUPLICATE_BARCODE",
        ) from exc
    db.refresh(barcode)
    return barcode
