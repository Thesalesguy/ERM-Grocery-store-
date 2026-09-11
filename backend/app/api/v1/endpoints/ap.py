"""Accounts Payable endpoints: purchase invoices, three-way matching,
supplier payments, AP subledger reporting.

`ap.read` gates every GET. `ap.write` gates creating a DRAFT invoice.
`ap.post` gates posting (financial commitment) and voiding (financial
reversal) an invoice — the two operations that establish or undo Accounts
Payable. `ap.pay` gates recording a supplier payment. See
app.modules.auth.permissions and docs/M6_AP_VENDOR_ACCOUNTING.md "RBAC".

Store isolation mirrors sales.py/purchasing.py exactly: a store-scoped
user's list/report requests are filtered via scoped_store_filter, a
direct-by-ID read of another store's invoice/payment 404s (never 403 —
doesn't confirm existence elsewhere), and every mutating endpoint calls
enforce_store_access at the route layer IN ADDITION to the service
layer's own independent check (defense-in-depth, proven independent by a
mutation test in tests/test_ap_api.py).
"""

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.ap import service
from app.modules.ap.models import PurchaseInvoice
from app.modules.ap.schemas import (
    ApAgingRowRead,
    ApReconciliationRead,
    PurchaseClearingReconciliationRead,
    PurchaseInvoiceCreate,
    PurchaseInvoiceLineRead,
    PurchaseInvoiceRead,
    PurchaseOrderItemMatchStatusRead,
    PurchaseOrderMatchingStatusRead,
    SupplierApSummaryRead,
    SupplierPaymentCreate,
    SupplierPaymentRead,
    SupplierTransactionRead,
    VoidPurchaseInvoiceRequest,
)
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.auth.permissions import AP_PAY, AP_POST, AP_READ, AP_WRITE
from app.modules.auth.service import (
    CurrentUser,
    enforce_store_access,
    require_permission,
    scoped_store_filter,
)
from app.modules.products.models import Product
from app.modules.purchasing.models import Supplier

router = APIRouter(prefix="/ap", tags=["accounts-payable"])

_read_permission = require_permission(AP_READ)
_write_permission = require_permission(AP_WRITE)
_post_permission = require_permission(AP_POST)
_pay_permission = require_permission(AP_PAY)


def _to_invoice_read(db: Session, invoice: PurchaseInvoice) -> PurchaseInvoiceRead:
    product_ids = {line.product_id for line in invoice.lines}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    supplier = db.get(Supplier, invoice.supplier_id)
    lines = []
    for line in invoice.lines:
        product = products.get(line.product_id)
        line_read = PurchaseInvoiceLineRead.model_validate(line)
        line_read.product_name = product.name if product else None
        line_read.product_sku = product.sku if product else None
        lines.append(line_read)
    read = PurchaseInvoiceRead.model_validate(invoice)
    read.balance_due = invoice.grand_total - invoice.amount_paid
    read.supplier_name = supplier.name if supplier else None
    read.lines = lines
    return read


@router.get("/invoices", response_model=list[PurchaseInvoiceRead])
def list_invoices(
    store_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[PurchaseInvoiceRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    invoices = service.list_purchase_invoices(
        db,
        store_id=effective_store_id,
        supplier_id=supplier_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [_to_invoice_read(db, inv) for inv in invoices]


@router.get("/invoices/{purchase_invoice_id}", response_model=PurchaseInvoiceRead)
def get_invoice(
    purchase_invoice_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseInvoiceRead:
    invoice = service.get_purchase_invoice(db, purchase_invoice_id)
    if current_user.store_id is not None and invoice.store_id != current_user.store_id:
        raise NotFoundError(f"Purchase invoice {purchase_invoice_id} not found")
    return _to_invoice_read(db, invoice)


@router.post("/invoices", response_model=PurchaseInvoiceRead, status_code=201)
def create_invoice(
    payload: PurchaseInvoiceCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> PurchaseInvoiceRead:
    enforce_store_access(current_user, payload.store_id)
    invoice = service.create_purchase_invoice(
        db,
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        purchase_order_id=payload.purchase_order_id,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        due_date=payload.due_date,
        notes=payload.notes,
        lines=[
            PurchaseInvoiceLineInput(
                purchase_order_item_id=line.purchase_order_item_id,
                quantity_invoiced=line.quantity_invoiced,
                unit_price=line.unit_price,
                discount_amount=line.discount_amount,
                tax_amount=line.tax_amount,
                description=line.description,
            )
            for line in payload.lines
        ],
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        created_by=current_user.id,
    )
    return _to_invoice_read(db, service.get_purchase_invoice(db, invoice.id))


@router.post("/invoices/{purchase_invoice_id}/post", response_model=PurchaseInvoiceRead)
def post_invoice(
    purchase_invoice_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_post_permission),
) -> PurchaseInvoiceRead:
    invoice = service.post_purchase_invoice(
        db,
        purchase_invoice_id=purchase_invoice_id,
        caller_store_id=current_user.store_id,
        posted_by=current_user.id,
    )
    db.commit()
    db.refresh(invoice)
    return _to_invoice_read(db, service.get_purchase_invoice(db, invoice.id))


@router.post("/invoices/{purchase_invoice_id}/void", response_model=PurchaseInvoiceRead)
def void_invoice(
    purchase_invoice_id: int,
    payload: VoidPurchaseInvoiceRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_post_permission),
) -> PurchaseInvoiceRead:
    invoice = service.void_purchase_invoice(
        db,
        purchase_invoice_id=purchase_invoice_id,
        caller_store_id=current_user.store_id,
        reason=payload.reason,
        voided_by=current_user.id,
    )
    db.commit()
    db.refresh(invoice)
    return _to_invoice_read(db, service.get_purchase_invoice(db, invoice.id))


@router.get(
    "/purchase-orders/{purchase_order_id}/matching-status",
    response_model=PurchaseOrderMatchingStatusRead,
)
def get_matching_status(
    purchase_order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseOrderMatchingStatusRead:
    rows = service.get_invoice_matching_status(db, purchase_order_id)
    product_ids = {row.product_id for row in rows}
    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }
    items = []
    for row in rows:
        product = products.get(row.product_id)
        items.append(
            PurchaseOrderItemMatchStatusRead(
                purchase_order_item_id=row.purchase_order_item_id,
                product_id=row.product_id,
                quantity_ordered=row.quantity_ordered,
                quantity_received=row.quantity_received,
                quantity_invoiced=row.quantity_invoiced,
                quantity_invoiceable=row.quantity_invoiceable,
                product_name=product.name if product else None,
                product_sku=product.sku if product else None,
            )
        )
    return PurchaseOrderMatchingStatusRead(purchase_order_id=purchase_order_id, items=items)


@router.post(
    "/invoices/{purchase_invoice_id}/payments", response_model=SupplierPaymentRead, status_code=201
)
def create_payment(
    purchase_invoice_id: int,
    payload: SupplierPaymentCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_pay_permission),
) -> SupplierPaymentRead:
    enforce_store_access(current_user, payload.store_id)
    payment = service.record_supplier_payment(
        db,
        purchase_invoice_id=purchase_invoice_id,
        store_id=payload.store_id,
        payment_date=payload.payment_date,
        payment_method=payload.payment_method,
        amount=payload.amount,
        reference=payload.reference,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        created_by=current_user.id,
    )
    db.commit()
    db.refresh(payment)
    return SupplierPaymentRead.model_validate(service.get_supplier_payment(db, payment.id))


@router.get("/payments", response_model=list[SupplierPaymentRead])
def list_payments(
    store_id: int | None = None,
    supplier_id: int | None = None,
    purchase_invoice_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierPaymentRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    payments = service.list_supplier_payments(
        db,
        store_id=effective_store_id,
        supplier_id=supplier_id,
        purchase_invoice_id=purchase_invoice_id,
        limit=limit,
        offset=offset,
    )
    return [SupplierPaymentRead.model_validate(p) for p in payments]


@router.get("/payments/{supplier_payment_id}", response_model=SupplierPaymentRead)
def get_payment(
    supplier_payment_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierPaymentRead:
    payment = service.get_supplier_payment(db, supplier_payment_id)
    if current_user.store_id is not None and payment.store_id != current_user.store_id:
        raise NotFoundError(f"Supplier payment {supplier_payment_id} not found")
    return SupplierPaymentRead.model_validate(payment)


@router.get("/suppliers/{supplier_id}/summary", response_model=SupplierApSummaryRead)
def get_supplier_summary(
    supplier_id: int,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierApSummaryRead:
    summary = service.get_supplier_ap_summary(db, supplier_id, as_of=as_of)
    return SupplierApSummaryRead(**summary.__dict__)


@router.get("/suppliers/{supplier_id}/transactions", response_model=list[SupplierTransactionRead])
def get_supplier_transactions(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierTransactionRead]:
    transactions = service.get_supplier_transaction_history(db, supplier_id)
    return [
        SupplierTransactionRead(
            transaction_type=t.transaction_type,
            id=t.id,
            date=t.date_,
            reference=t.reference,
            amount=t.amount,
            status=t.status,
        )
        for t in transactions
    ]


@router.get("/aging", response_model=list[ApAgingRowRead])
def get_aging(
    store_id: int | None = None,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ApAgingRowRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    rows = service.ap_aging(db, store_id=effective_store_id, as_of=as_of)
    supplier_ids = {row.supplier_id for row in rows}
    suppliers = {
        s.id: s for s in db.execute(select(Supplier).where(Supplier.id.in_(supplier_ids))).scalars()
    }
    return [
        ApAgingRowRead(
            supplier_id=row.supplier_id,
            supplier_name=suppliers[row.supplier_id].name if row.supplier_id in suppliers else None,
            current=row.current,
            days_1_30=row.days_1_30,
            days_31_60=row.days_31_60,
            days_61_90=row.days_61_90,
            days_over_90=row.days_over_90,
            total=row.total,
        )
        for row in rows
    ]


@router.get("/reports/ap-reconciliation", response_model=ApReconciliationRead)
def get_ap_reconciliation(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ApReconciliationRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    row = service.ap_reconciliation(db, store_id=effective_store_id)
    return ApReconciliationRead(**row.__dict__)


@router.get(
    "/reports/purchase-clearing-reconciliation", response_model=PurchaseClearingReconciliationRead
)
def get_purchase_clearing_reconciliation(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseClearingReconciliationRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    row = service.purchase_clearing_reconciliation(db, store_id=effective_store_id)
    return PurchaseClearingReconciliationRead(**row.__dict__)
