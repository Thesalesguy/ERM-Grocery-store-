"""POS / sale-finalization endpoints.

`pos.use` gates sale finalization; `sales.read` gates history/receipt
lookups (see app.modules.auth.permissions).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.auth.permissions import POS_USE, SALES_READ
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.products.models import Product
from app.modules.sales import service
from app.modules.sales.models import Sale
from app.modules.sales.schemas import SaleCreate, SaleItemRead, SaleRead
from app.modules.sales.service import PaymentInput, SaleLineInput

router = APIRouter(prefix="/sales", tags=["sales"])

_read = Depends(require_permission(SALES_READ))
_pos_use = require_permission(POS_USE)


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


@router.get("", response_model=list[SaleRead], dependencies=[_read])
def list_sales(
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[SaleRead]:
    sales = service.list_sales(db, store_id=store_id, limit=limit, offset=offset)
    return [_to_sale_read(db, sale) for sale in sales]


@router.get("/{sale_id}", response_model=SaleRead, dependencies=[_read])
def get_sale(sale_id: int, db: Session = Depends(get_db)) -> SaleRead:
    sale = service.get_sale(db, sale_id)
    return _to_sale_read(db, sale)
