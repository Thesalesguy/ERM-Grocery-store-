"""Product catalog: creation, uniqueness, and DB-level constraints."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.modules.products import service
from app.modules.products.models import Product, ProductBarcode, ProductCategory
from app.modules.products.schemas import ProductCreate
from tests.factories import make_category, make_product, make_store, unique_suffix


def test_create_product(db: Session) -> None:
    store = make_store(db)
    db.commit()

    product = service.create_product(
        db,
        ProductCreate(
            store_id=store.id, sku="SKU-CREATE", name="Widget", current_price=Decimal("9.99")
        ),
    )

    assert product.id is not None
    assert product.current_price == Decimal("9.99")
    # System-managed fields are never client-settable, and start at zero.
    assert product.current_cost == 0
    assert product.current_qty_on_hand == 0


def test_duplicate_sku_within_store_rejected(db: Session) -> None:
    store = make_store(db)
    db.commit()

    service.create_product(
        db, ProductCreate(store_id=store.id, sku="DUP-SKU", name="A", current_price=Decimal("1.00"))
    )

    with pytest.raises(ConflictError):
        service.create_product(
            db,
            ProductCreate(
                store_id=store.id, sku="DUP-SKU", name="B", current_price=Decimal("2.00")
            ),
        )


def test_same_sku_allowed_in_different_stores(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    db.commit()

    service.create_product(
        db,
        ProductCreate(store_id=store_a.id, sku="SHARED-SKU", name="A", current_price=Decimal("1")),
    )
    # No exception: uniqueness is scoped per store, not global.
    service.create_product(
        db,
        ProductCreate(store_id=store_b.id, sku="SHARED-SKU", name="B", current_price=Decimal("2")),
    )


def test_duplicate_barcode_across_products_rejected(db: Session) -> None:
    store = make_store(db)
    product_a = make_product(db, store)
    product_b = make_product(db, store)
    db.commit()

    db.add(ProductBarcode(product_id=product_a.id, barcode="1234567890123", barcode_type="EAN13"))
    db.commit()

    db.add(ProductBarcode(product_id=product_b.id, barcode="1234567890123", barcode_type="EAN13"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_negative_price_rejected_by_check_constraint(db: Session) -> None:
    store = make_store(db)
    db.commit()

    db.add(
        Product(
            store_id=store.id,
            sku=f"NEG-{unique_suffix()}",
            name="Bad price",
            current_price=Decimal("-1.00"),
            current_cost=0,
            current_qty_on_hand=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_negative_stock_blocked_by_default(db: Session) -> None:
    store = make_store(db)
    db.commit()

    db.add(
        Product(
            store_id=store.id,
            sku=f"NEGSTOCK-{unique_suffix()}",
            name="Bad stock",
            current_price=0,
            current_cost=0,
            current_qty_on_hand=Decimal("-5"),
            allow_negative_stock=False,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_negative_stock_allowed_when_flag_set(db: Session) -> None:
    store = make_store(db)
    db.commit()

    product = Product(
        store_id=store.id,
        sku=f"NEGOK-{unique_suffix()}",
        name="Backorder-friendly",
        current_price=0,
        current_cost=0,
        current_qty_on_hand=Decimal("-5"),
        allow_negative_stock=True,
    )
    db.add(product)
    db.commit()  # does not raise
    assert product.current_qty_on_hand == Decimal("-5")


def test_top_level_category_names_must_be_unique(db: Session) -> None:

    make_category(db, name="Groceries")
    db.commit()

    db.add(ProductCategory(name="Groceries", parent_id=None, is_active=True))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_name_allowed_under_different_parents(db: Session) -> None:
    parent_a = make_category(db)
    parent_b = make_category(db)
    db.commit()

    db.add(ProductCategory(name="Snacks", parent_id=parent_a.id, is_active=True))
    db.add(ProductCategory(name="Snacks", parent_id=parent_b.id, is_active=True))
    db.commit()  # does not raise: uniqueness is scoped per parent
