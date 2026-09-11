"""POS / sale-finalization endpoints, plus M5 sale returns and voids.

`pos.use` gates sale finalization; `sales.read` gates history/receipt
lookups; `sales.return.read`/`sales.return.write`/`sales.void` gate the
M5 return/void workflow (see app.modules.auth.permissions and
docs/M5_RETURNS_VOIDS_REFUNDS.md "RBAC").

Route ordering note: `/returns` and `/returns/{return_id}` are declared
BEFORE `/{sale_id}` so FastAPI/Starlette never tries to match "returns"
as an integer `sale_id` — a static path segment must be registered ahead
of a same-prefix parameterized one to win the match.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import (
    POS_USE,
    SALES_READ,
    SALES_RETURN_READ,
    SALES_RETURN_WRITE,
    SALES_VOID,
)
from app.modules.auth.service import (
    CurrentUser,
    enforce_store_access,
    require_permission,
    scoped_store_filter,
)
from app.modules.products.models import Product
from app.modules.sales import service
from app.modules.sales.models import Sale, SaleItem, SaleReturn
from app.modules.sales.schemas import (
    SaleCreate,
    SaleItemRead,
    SaleItemReturnEligibilityRead,
    SaleRead,
    SaleReturnCreate,
    SaleReturnEligibilityRead,
    SaleReturnItemRead,
    SaleReturnRead,
    VoidSaleCreate,
)
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput

router = APIRouter(prefix="/sales", tags=["sales"])

_read_permission = require_permission(SALES_READ)
_pos_use = require_permission(POS_USE)
_return_read_permission = require_permission(SALES_RETURN_READ)
_return_write_permission = require_permission(SALES_RETURN_WRITE)
_void_permission = require_permission(SALES_VOID)


def _to_sale_read(db: Session, sale: Sale) -> SaleRead:
    product_ids = {item.product_id for item in sale.items}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    items = []
    for item in sale.items:
        product = products.get(item.product_id)
        item_read = SaleItemRead.model_validate(item)
        item_read.product_name = product.name if product else None
        item_read.product_sku = product.sku if product else None
        items.append(item_read)
    sale_read = SaleRead.model_validate(sale)
    sale_read.items = items
    return sale_read


def _to_sale_return_read(db: Session, sale_return: SaleReturn) -> SaleReturnRead:
    # Batch-fetched, not traversed via a cross-module ORM relationship —
    # same established pattern as purchasing's _to_po_read /
    # sales' own _to_sale_read.
    sale_item_ids = {item.sale_item_id for item in sale_return.items}
    sale_items = {
        i.id: i
        for i in db.execute(select(SaleItem).where(SaleItem.id.in_(sale_item_ids))).scalars()
    }
    product_ids = {item.product_id for item in sale_items.values()}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    items = []
    for item in sale_return.items:
        sale_item = sale_items.get(item.sale_item_id)
        product = products.get(sale_item.product_id) if sale_item else None
        item_read = SaleReturnItemRead.model_validate(item)
        item_read.product_id = sale_item.product_id if sale_item else None
        item_read.product_name = product.name if product else None
        item_read.product_sku = product.sku if product else None
        items.append(item_read)
    read = SaleReturnRead.model_validate(sale_return)
    read.items = items
    return read


@router.post("", response_model=SaleRead, status_code=201)
def create_sale(
    payload: SaleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_pos_use),
) -> SaleRead:
    sale = service.finalize_sale(
        db,
        store_id=payload.store_id,
        cashier_id=current_user.id,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        lines=[
            SaleLineInput(
                product_id=line.product_id,
                quantity=line.quantity,
                discount_amount=line.discount_amount,
            )
            for line in payload.lines
        ],
        payments=[
            PaymentInput(
                payment_method=payment.payment_method,
                amount=payment.amount,
                reference=payment.reference,
            )
            for payment in payload.payments
        ],
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(sale)
    return _to_sale_read(db, service.get_sale(db, sale.id))


@router.get("", response_model=list[SaleRead])
def list_sales(
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SaleRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    sales = service.list_sales(db, store_id=effective_store_id, limit=limit, offset=offset)
    return [_to_sale_read(db, sale) for sale in sales]


# --- Sale returns (M5) — declared BEFORE /{sale_id} (see module docstring) --


@router.get("/returns", response_model=list[SaleReturnRead])
def list_sale_returns(
    sale_id: int | None = None,
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_return_read_permission),
) -> list[SaleReturnRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    returns = service.list_sale_returns(
        db, sale_id=sale_id, store_id=effective_store_id, limit=limit, offset=offset
    )
    return [_to_sale_return_read(db, r) for r in returns]


@router.get("/returns/{sale_return_id}", response_model=SaleReturnRead)
def get_sale_return(
    sale_return_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_return_read_permission),
) -> SaleReturnRead:
    sale_return = service.get_sale_return(db, sale_return_id)
    if current_user.store_id is not None and sale_return.store_id != current_user.store_id:
        raise NotFoundError(f"Sale return {sale_return_id} not found")
    return _to_sale_return_read(db, sale_return)


@router.get("/{sale_id}/return-eligibility", response_model=SaleReturnEligibilityRead)
def get_return_eligibility(
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_return_read_permission),
) -> SaleReturnEligibilityRead:
    sale = service.get_sale(db, sale_id)
    if current_user.store_id is not None and sale.store_id != current_user.store_id:
        raise NotFoundError(f"Sale {sale_id} not found")
    product_ids = {item.product_id for item in sale.items}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    eligibility = service.get_return_eligibility(db, sale_id)
    items = []
    for e in eligibility:
        product = products.get(e.product_id)
        items.append(
            SaleItemReturnEligibilityRead(
                sale_item_id=e.sale_item_id,
                product_id=e.product_id,
                quantity=e.quantity,
                quantity_returned=e.quantity_returned,
                quantity_returnable=e.quantity_returnable,
                product_name=product.name if product else None,
                product_sku=product.sku if product else None,
            )
        )
    return SaleReturnEligibilityRead(sale_id=sale.id, sale_status=sale.status, items=items)


@router.post("/{sale_id}/returns", response_model=SaleReturnRead, status_code=201)
def create_sale_return(
    sale_id: int,
    payload: SaleReturnCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_return_write_permission),
) -> SaleReturnRead:
    enforce_store_access(current_user, payload.store_id)
    sale_return = service.create_sale_return(
        db,
        sale_id=sale_id,
        store_id=payload.store_id,
        return_date=payload.return_date,
        lines=[
            SaleReturnLineInput(
                sale_item_id=line.sale_item_id, quantity=line.quantity, restock=line.restock
            )
            for line in payload.lines
        ],
        refund_method=payload.refund_method,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        reason=payload.reason,
        created_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(sale_return)
    return _to_sale_return_read(db, service.get_sale_return(db, sale_return.id))


@router.post("/{sale_id}/void", response_model=SaleReturnRead, status_code=201)
def void_sale(
    sale_id: int,
    payload: VoidSaleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_void_permission),
) -> SaleReturnRead:
    enforce_store_access(current_user, payload.store_id)
    sale_return = service.void_sale(
        db,
        sale_id=sale_id,
        store_id=payload.store_id,
        return_date=payload.return_date,
        refund_method=payload.refund_method,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        reason=payload.reason,
        created_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(sale_return)
    return _to_sale_return_read(db, service.get_sale_return(db, sale_return.id))


@router.get("/{sale_id}", response_model=SaleRead)
def get_sale(
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SaleRead:
    sale = service.get_sale(db, sale_id)
    # A store-scoped user gets 404 (not 403) for another store's sale, so
    # the response doesn't confirm that sale even exists elsewhere (M2
    # hardening audit Section 12).
    if current_user.store_id is not None and sale.store_id != current_user.store_id:
        raise NotFoundError(f"Sale {sale_id} not found")
    return _to_sale_read(db, sale)
