"""Purchasing endpoints: suppliers (global reference data), purchase
orders, goods receiving, and purchase returns.

`purchasing.read` gates every GET; `purchasing.write` gates supplier and
purchase-order create/edit/submit/cancel; `purchasing.receive` gates the
two operations that actually move inventory — receiving and returns (a
return is, like a receipt, a real inventory-affecting event, not a
catalog edit — see docs/M3_PURCHASING_RECEIVING_WAC.md "Permissions").
Suppliers are NOT store-scoped (shared reference data — see
app.modules.purchasing.models.Supplier's docstring); purchase orders,
goods receipts, and purchase returns are, enforced the same way
app.modules.sales/products/inventory already do (M2 hardening audit
Section 12).
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import PURCHASING_READ, PURCHASING_RECEIVE, PURCHASING_WRITE
from app.modules.auth.service import (
    CurrentUser,
    enforce_store_access,
    require_permission,
    scoped_store_filter,
)
from app.modules.products.models import Product
from app.modules.purchasing import service
from app.modules.purchasing.models import PurchaseOrder, Supplier
from app.modules.purchasing.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptRead,
    PurchaseOrderCancelRequest,
    PurchaseOrderCreate,
    PurchaseOrderItemRead,
    PurchaseOrderRead,
    PurchaseReturnCreate,
    PurchaseReturnRead,
    SupplierCreate,
    SupplierRead,
    SupplierUpdate,
)
from app.modules.purchasing.service import (
    GoodsReceiptLineInput,
    PurchaseOrderItemInput,
    PurchaseReturnLineInput,
    SupplierInput,
)

router = APIRouter(prefix="/purchasing", tags=["purchasing"])

_read_permission = require_permission(PURCHASING_READ)
_write_permission = require_permission(PURCHASING_WRITE)
_receive_permission = require_permission(PURCHASING_RECEIVE)


# --- Suppliers (global — no store scoping, see module docstring) ----------


@router.post("/suppliers", response_model=SupplierRead, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> SupplierRead:
    supplier = service.create_supplier(
        db, SupplierInput(**payload.model_dump()), actor_id=current_user.id
    )
    return SupplierRead.model_validate(supplier)


@router.get("/suppliers", response_model=list[SupplierRead])
def list_suppliers(
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierRead]:
    suppliers = service.list_suppliers(
        db, is_active=is_active, search=search, limit=limit, offset=offset
    )
    return [SupplierRead.model_validate(s) for s in suppliers]


@router.get("/suppliers/{supplier_id}", response_model=SupplierRead)
def get_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierRead:
    return SupplierRead.model_validate(service.get_supplier(db, supplier_id))


@router.put("/suppliers/{supplier_id}", response_model=SupplierRead)
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> SupplierRead:
    changes = payload.model_dump(exclude_unset=True)
    supplier = service.update_supplier(db, supplier_id, changes, actor_id=current_user.id)
    return SupplierRead.model_validate(supplier)


@router.post("/suppliers/{supplier_id}/activate", response_model=SupplierRead)
def activate_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> SupplierRead:
    return SupplierRead.model_validate(
        service.set_supplier_active(db, supplier_id, True, actor_id=current_user.id)
    )


@router.post("/suppliers/{supplier_id}/deactivate", response_model=SupplierRead)
def deactivate_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> SupplierRead:
    return SupplierRead.model_validate(
        service.set_supplier_active(db, supplier_id, False, actor_id=current_user.id)
    )


# --- Purchase orders (store-scoped) ----------------------------------------


def _to_po_read(db: Session, po: PurchaseOrder) -> PurchaseOrderRead:
    # Batch-fetched, not traversed via an ORM relationship — matching
    # app.api.v1.endpoints.sales._to_sale_read's established pattern of
    # keeping cross-module reads as one explicit query rather than a
    # relationship spanning module boundaries (and avoiding N+1 across a
    # list of purchase orders).
    product_ids = {item.product_id for item in po.items}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    items = []
    for item in po.items:
        quantity_remaining = item.quantity_ordered - item.quantity_received
        product = products.get(item.product_id)
        items.append(
            PurchaseOrderItemRead(
                id=item.id,
                product_id=item.product_id,
                quantity_ordered=item.quantity_ordered,
                quantity_received=item.quantity_received,
                unit_cost=item.unit_cost,
                quantity_remaining=quantity_remaining if quantity_remaining > 0 else Decimal("0"),
                is_over_received=item.quantity_received > item.quantity_ordered,
                product_name=product.name if product else None,
                product_sku=product.sku if product else None,
            )
        )
    read = PurchaseOrderRead.model_validate(po)
    read.items = items
    supplier = db.get(Supplier, po.supplier_id)
    read.supplier_name = supplier.name if supplier else None
    return read


@router.post(
    "/purchase-orders", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED
)
def create_purchase_order(
    payload: PurchaseOrderCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> PurchaseOrderRead:
    po = service.create_purchase_order(
        db,
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        order_date=payload.order_date,
        expected_date=payload.expected_date,
        notes=payload.notes,
        lines=[
            PurchaseOrderItemInput(
                product_id=line.product_id,
                quantity_ordered=line.quantity_ordered,
                unit_cost=line.unit_cost,
            )
            for line in payload.lines
        ],
        created_by=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return _to_po_read(db, service.get_purchase_order(db, po.id))


@router.get("/purchase-orders", response_model=list[PurchaseOrderRead])
def list_purchase_orders(
    store_id: int | None = None,
    supplier_id: int | None = None,
    status_filter: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[PurchaseOrderRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    orders = service.list_purchase_orders(
        db,
        store_id=effective_store_id,
        supplier_id=supplier_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return [_to_po_read(db, po) for po in orders]


def _get_po_with_store_check(
    db: Session, purchase_order_id: int, current_user: CurrentUser
) -> PurchaseOrder:
    po = service.get_purchase_order(db, purchase_order_id)
    if current_user.store_id is not None and current_user.store_id != po.store_id:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    return po


@router.get("/purchase-orders/{purchase_order_id}", response_model=PurchaseOrderRead)
def get_purchase_order(
    purchase_order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseOrderRead:
    po = _get_po_with_store_check(db, purchase_order_id, current_user)
    return _to_po_read(db, po)


@router.post("/purchase-orders/{purchase_order_id}/submit", response_model=PurchaseOrderRead)
def submit_purchase_order(
    purchase_order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> PurchaseOrderRead:
    service.submit_purchase_order(
        db, purchase_order_id, actor_id=current_user.id, caller_store_id=current_user.store_id
    )
    return _to_po_read(db, service.get_purchase_order(db, purchase_order_id))


@router.post("/purchase-orders/{purchase_order_id}/cancel", response_model=PurchaseOrderRead)
def cancel_purchase_order(
    purchase_order_id: int,
    payload: PurchaseOrderCancelRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> PurchaseOrderRead:
    service.cancel_purchase_order(
        db,
        purchase_order_id,
        actor_id=current_user.id,
        reason=payload.reason,
        caller_store_id=current_user.store_id,
    )
    return _to_po_read(db, service.get_purchase_order(db, purchase_order_id))


# --- Goods receiving --------------------------------------------------------


@router.post(
    "/purchase-orders/{purchase_order_id}/receive",
    response_model=GoodsReceiptRead,
    status_code=status.HTTP_201_CREATED,
)
def receive_goods(
    purchase_order_id: int,
    payload: GoodsReceiptCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_receive_permission),
) -> GoodsReceiptRead:
    receipt = service.receive_goods(
        db,
        purchase_order_id=purchase_order_id,
        received_date=payload.received_date,
        notes=payload.notes,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        received_by=current_user.id,
        lines=[
            GoodsReceiptLineInput(
                purchase_order_item_id=line.purchase_order_item_id,
                quantity_received=line.quantity_received,
                unit_cost=line.unit_cost,
                condition_notes=line.condition_notes,
            )
            for line in payload.lines
        ],
    )
    db.commit()
    db.refresh(receipt)
    return GoodsReceiptRead.model_validate(service.get_goods_receipt(db, receipt.id))


@router.get("/goods-receipts/{goods_receipt_id}", response_model=GoodsReceiptRead)
def get_goods_receipt(
    goods_receipt_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> GoodsReceiptRead:
    receipt = service.get_goods_receipt(db, goods_receipt_id)
    if current_user.store_id is not None and current_user.store_id != receipt.store_id:
        raise NotFoundError(f"Goods receipt {goods_receipt_id} not found")
    return GoodsReceiptRead.model_validate(receipt)


@router.get("/goods-receipts", response_model=list[GoodsReceiptRead])
def list_goods_receipts(
    purchase_order_id: int | None = None,
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[GoodsReceiptRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    receipts = service.list_goods_receipts(
        db, purchase_order_id=purchase_order_id, store_id=effective_store_id
    )
    return [GoodsReceiptRead.model_validate(r) for r in receipts]


# --- Purchase returns --------------------------------------------------------


@router.post(
    "/purchase-orders/{purchase_order_id}/returns",
    response_model=PurchaseReturnRead,
    status_code=status.HTTP_201_CREATED,
)
def create_purchase_return(
    purchase_order_id: int,
    payload: PurchaseReturnCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_receive_permission),
) -> PurchaseReturnRead:
    enforce_store_access(current_user, payload.store_id)
    purchase_return = service.create_purchase_return(
        db,
        purchase_order_id=purchase_order_id,
        store_id=payload.store_id,
        return_date=payload.return_date,
        reason=payload.reason,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        created_by=current_user.id,
        lines=[
            PurchaseReturnLineInput(product_id=line.product_id, quantity=line.quantity)
            for line in payload.lines
        ],
    )
    db.commit()
    db.refresh(purchase_return)
    return PurchaseReturnRead.model_validate(service.get_purchase_return(db, purchase_return.id))


@router.get("/purchase-returns/{purchase_return_id}", response_model=PurchaseReturnRead)
def get_purchase_return(
    purchase_return_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseReturnRead:
    purchase_return = service.get_purchase_return(db, purchase_return_id)
    if current_user.store_id is not None and current_user.store_id != purchase_return.store_id:
        raise NotFoundError(f"Purchase return {purchase_return_id} not found")
    return PurchaseReturnRead.model_validate(purchase_return)
