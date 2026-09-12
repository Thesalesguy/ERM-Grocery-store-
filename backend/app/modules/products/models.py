"""ORM models for product categories, products, and product barcodes.

See docs/TECHNICAL_BLUEPRINT.md Section C.2 and docs/M1_DATABASE_DESIGN.md
for the authoritative design and the decisions behind it (in particular:
`current_qty_on_hand`/`current_cost` are a maintained *cache* reconciled
against the inventory_movements ledger, never the source of truth — see
the inventory module).
"""

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin


class ProductCategory(TimestampMixin, Base):
    __tablename__ = "product_categories"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_product_categories_parent_name"),
        # Postgres treats NULLs as distinct in a regular UNIQUE constraint,
        # so two top-level categories (parent_id IS NULL) could otherwise
        # share a name. Close that gap with a partial unique index.
        Index(
            "uq_product_categories_top_level_name",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("product_categories.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "sku", name="uq_products_store_sku"),
        CheckConstraint("current_price >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("current_cost >= 0", name="ck_products_cost_non_negative"),
        CheckConstraint(
            "allow_negative_stock OR current_qty_on_hand >= 0",
            name="ck_products_stock_non_negative_unless_allowed",
        ),
        CheckConstraint(
            "unit_of_measure IN ('each', 'kg', 'g', 'l', 'ml')",
            name="ck_products_unit_of_measure",
        ),
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_default_supplier_id", "default_supplier_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("product_categories.id"))
    default_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    unit_of_measure: Mapped[str] = mapped_column(String(16), nullable=False, default="each")
    is_weighed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Reference/catalog values. Historical sale and purchase lines freeze
    # their own copies at transaction time (BR-2) — these columns are only
    # "what would apply right now."
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Weighted Average Cost — maintained exclusively by the inventory
    # module (app/modules/inventory/service.py). Never edited directly by
    # a route handler or by catalog-editing code.
    current_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)

    tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    reorder_point: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    # M9 (docs/M9_SUPPLY_CHAIN_DESIGN.md "Design Decision 10"): these live
    # on Product, not a separate (store, product)-keyed policy table —
    # Product already IS that pair. NULL target_stock_quantity means "aim
    # for reorder_point itself" (no separate buffer configured);
    # minimum_stock_quantity is the urgent-priority floor (distinct from
    # reorder_point, which is the normal-priority trigger) and folds in
    # what would otherwise be a separate, synonymous "safety stock" field.
    target_stock_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    minimum_stock_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))

    # Inventory cache (docs/TECHNICAL_BLUEPRINT.md Section G): the
    # authoritative source of truth is the inventory_movements ledger.
    # This column exists only so reads (POS lookup, catalog listing)
    # don't have to SUM the ledger every time. It must always be
    # reconcilable to SUM(inventory_movements.quantity_delta) for this
    # product — see test_inventory.py::test_qty_on_hand_matches_ledger_sum.
    current_qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    # Per-product override of the default negative-stock policy
    # (docs/TECHNICAL_BLUEPRINT.md assumption #12 / BR-7). Default is the
    # safe option: negative stock is blocked.
    allow_negative_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    category: Mapped[ProductCategory | None] = relationship(back_populates="products")
    barcodes: Mapped[list["ProductBarcode"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductBarcode(TimestampMixin, Base):
    """A product may have more than one barcode (unit pack, case pack,
    weight-embedded PLU prefix, etc. — docs/TECHNICAL_BLUEPRINT.md
    assumption #6). A barcode is NOT assumed to equal the SKU."""

    __tablename__ = "product_barcodes"
    __table_args__ = (
        CheckConstraint("pack_quantity > 0", name="ck_product_barcodes_pack_quantity_positive"),
        CheckConstraint(
            "barcode_type IN ('EAN13', 'UPC_A', 'CODE128', 'WEIGHT_EMBEDDED', 'INTERNAL')",
            name="ck_product_barcodes_type",
        ),
        Index(
            "uq_product_barcodes_one_primary_per_product",
            "product_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_product_barcodes_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    barcode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    barcode_type: Mapped[str] = mapped_column(String(20), nullable=False, default="EAN13")
    pack_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=1)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    product: Mapped[Product] = relationship(back_populates="barcodes")
