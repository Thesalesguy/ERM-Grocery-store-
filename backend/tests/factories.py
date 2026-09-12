"""Small helpers to build valid rows for domain tests without repeating
FK/unique-constraint boilerplate in every test. Every unique value
(SKU, barcode, sale_number, ...) is suffixed with a fresh uuid fragment so
tests can run repeatedly against the same database without colliding.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.auth.models import Role, Store, User, UserRole
from app.modules.products.models import Product, ProductCategory
from app.modules.purchasing.models import PurchaseOrder, Supplier
from app.modules.replenishment.models import SupplierProduct
from app.modules.tax.models import TaxRate

DEFAULT_TEST_PASSWORD = "Test-Password-123!"


def unique_suffix() -> str:
    return uuid.uuid4().hex[:8]


def make_store(db: Session, **overrides) -> Store:
    defaults = dict(name=f"Store {unique_suffix()}", timezone="UTC", is_active=True)
    defaults.update(overrides)
    store = Store(**defaults)
    db.add(store)
    db.flush()
    return store


def make_user(db: Session, store: Store | None = None, **overrides) -> User:
    suffix = unique_suffix()
    defaults = dict(
        store_id=store.id if store else None,
        username=f"user_{suffix}",
        email=f"user_{suffix}@example.com",
        password_hash="not-a-real-hash",
        full_name="Test User",
        is_active=True,
    )
    defaults.update(overrides)
    user = User(**defaults)
    db.add(user)
    db.flush()
    return user


def make_user_with_role(
    db: Session,
    store: Store | None,
    role_name: str,
    *,
    password: str = DEFAULT_TEST_PASSWORD,
    **overrides,
) -> User:
    """A user with a known plaintext password (for exercising the real
    HTTP login endpoint in API-level tests) and one assigned role."""
    user = make_user(db, store, password_hash=hash_password(password), **overrides)
    role = db.execute(select(Role).where(Role.name == role_name)).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def make_supplier(db: Session, **overrides) -> Supplier:
    defaults = dict(name=f"Supplier {unique_suffix()}", is_active=True)
    defaults.update(overrides)
    supplier = Supplier(**defaults)
    db.add(supplier)
    db.flush()
    return supplier


def make_category(
    db: Session, parent: ProductCategory | None = None, **overrides
) -> ProductCategory:
    defaults = dict(
        name=f"Category {unique_suffix()}",
        parent_id=parent.id if parent else None,
        is_active=True,
    )
    defaults.update(overrides)
    category = ProductCategory(**defaults)
    db.add(category)
    db.flush()
    return category


def make_tax_rate(db: Session, rate_percent: Decimal = Decimal("18.000"), **overrides) -> TaxRate:
    defaults = dict(
        name=f"Tax {unique_suffix()}",
        rate_percent=rate_percent,
        is_active=True,
        effective_from=date(2020, 1, 1),
    )
    defaults.update(overrides)
    tax_rate = TaxRate(**defaults)
    db.add(tax_rate)
    db.flush()
    return tax_rate


def make_product(db: Session, store: Store, **overrides) -> Product:
    defaults = dict(
        store_id=store.id,
        sku=f"SKU-{unique_suffix()}",
        name=f"Product {unique_suffix()}",
        unit_of_measure="each",
        current_price=Decimal("0.00"),
        current_cost=Decimal("0"),
        current_qty_on_hand=Decimal("0"),
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db.add(product)
    db.flush()
    return product


def make_purchase_order(
    db: Session, store: Store, supplier: Supplier, created_by: User | None = None, **overrides
) -> PurchaseOrder:
    defaults = dict(
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_number=f"PO-{unique_suffix()}",
        status="ORDERED",
        order_date=date(2024, 1, 1),
        created_by=created_by.id if created_by else None,
    )
    defaults.update(overrides)
    purchase_order = PurchaseOrder(**defaults)
    db.add(purchase_order)
    db.flush()
    return purchase_order


def make_supplier_product(
    db: Session, supplier: Supplier, product: Product, **overrides
) -> SupplierProduct:
    defaults = dict(
        supplier_id=supplier.id,
        product_id=product.id,
        pack_size=Decimal("1"),
        unit_cost=Decimal("1.00"),
        effective_date=date(2024, 1, 1),
        is_active=True,
    )
    defaults.update(overrides)
    supplier_product = SupplierProduct(**defaults)
    db.add(supplier_product)
    db.flush()
    return supplier_product
