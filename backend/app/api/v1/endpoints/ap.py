"""Accounts Payable endpoints: purchase invoices, three-way matching,
multi-invoice supplier payment allocation, supplier credit notes, and AP
subledger reporting.

`ap.read` gates every GET. `ap.write` gates creating a DRAFT invoice.
`ap.post` gates posting (financial commitment) and voiding (financial
reversal) an invoice. `ap.pay` gates recording a supplier payment.
`ap.credit` gates creating a supplier credit note (also a financial
commitment — single-step, immediate). See app.modules.auth.permissions and
docs/M7_ADVANCED_AP_SETTLEMENT.md "RBAC".

Store isolation mirrors sales.py/purchasing.py exactly: a store-scoped
user's list/report requests are filtered via scoped_store_filter, a
direct-by-ID read of another store's invoice/payment/credit-note 404s
(never 403 — doesn't confirm existence elsewhere), and every mutating
endpoint calls enforce_store_access at the route layer IN ADDITION to the
service layer's own independent check (defense-in-depth, proven
independent by a mutation test in tests/test_ap_api.py).
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.ap import service
from app.modules.ap.models import PurchaseInvoice, SupplierCreditNote
from app.modules.ap.schemas import (
    ApAgingRowRead,
    ApReconciliationRead,
    PurchaseClearingReconciliationRead,
    PurchaseInvoiceCreate,
    PurchaseInvoiceLineRead,
    PurchaseInvoiceRead,
    PurchaseInvoiceReceiptMatchRead,
    PurchaseOrderItemMatchStatusRead,
    PurchaseOrderMatchingStatusRead,
    SupplierApSummaryRead,
    SupplierCreditNoteCreate,
    SupplierCreditNoteRead,
    SupplierPaymentCreate,
    SupplierPaymentRead,
    SupplierReversalRequest,
    SupplierStatementLineRead,
    SupplierStatementRead,
    SupplierTransactionRead,
    VoidPurchaseInvoiceRequest,
)
from app.modules.ap.service import (
    PaymentAllocationInput,
    PurchaseInvoiceLineInput,
    SupplierCreditNoteLineInput,
)
from app.modules.auth.permissions import AP_CREDIT, AP_PAY, AP_POST, AP_READ, AP_REVERSE, AP_WRITE
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
_credit_permission = require_permission(AP_CREDIT)
_reverse_permission = require_permission(AP_REVERSE)


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
        line_read.matches = [
            PurchaseInvoiceReceiptMatchRead.model_validate(m) for m in line.matches
        ]
        lines.append(line_read)
    read = PurchaseInvoiceRead.model_validate(invoice)
    read.balance_due = invoice.grand_total - invoice.amount_paid - invoice.amount_credited
    read.supplier_name = supplier.name if supplier else None
    read.lines = lines
    return read


def _to_credit_note_read(db: Session, credit_note: SupplierCreditNote) -> SupplierCreditNoteRead:
    # Nested `lines`/`allocations` are populated automatically from the
    # ORM relationships of the same name (from_attributes=True) — only
    # supplier_name needs manual enrichment.
    supplier = db.get(Supplier, credit_note.supplier_id)
    read = SupplierCreditNoteRead.model_validate(credit_note)
    read.supplier_name = supplier.name if supplier else None
    return read


@router.get("/invoices", response_model=list[PurchaseInvoiceRead])
def list_invoices(
    store_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
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
    rows = service.get_invoice_matching_status(
        db, purchase_order_id, caller_store_id=current_user.store_id
    )
    return _matching_status_response(db, [purchase_order_id], rows)


@router.get(
    "/purchase-orders/matching-status",
    response_model=PurchaseOrderMatchingStatusRead,
)
def get_matching_status_multi(
    purchase_order_ids: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> PurchaseOrderMatchingStatusRead:
    """M7: the multi-PO matching preview for the "one invoice across
    several purchase orders" workflow. `purchase_order_ids` is a
    comma-separated list, e.g. `?purchase_order_ids=12,13`."""
    ids = [int(part) for part in purchase_order_ids.split(",") if part.strip()]
    rows = service.get_invoice_matching_status_multi(db, ids, caller_store_id=current_user.store_id)
    return _matching_status_response(db, ids, rows)


def _matching_status_response(
    db: Session, purchase_order_ids: list[int], rows: list
) -> PurchaseOrderMatchingStatusRead:
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
                purchase_order_id=row.purchase_order_id,
                product_id=row.product_id,
                quantity_ordered=row.quantity_ordered,
                quantity_received=row.quantity_received,
                quantity_invoiced=row.quantity_invoiced,
                quantity_invoiceable=row.quantity_invoiceable,
                product_name=product.name if product else None,
                product_sku=product.sku if product else None,
            )
        )
    return PurchaseOrderMatchingStatusRead(purchase_order_ids=purchase_order_ids, items=items)


@router.post("/payments", response_model=SupplierPaymentRead, status_code=201)
def create_payment(
    payload: SupplierPaymentCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_pay_permission),
) -> SupplierPaymentRead:
    enforce_store_access(current_user, payload.store_id)
    payment = service.record_supplier_payment(
        db,
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        payment_date=payload.payment_date,
        payment_method=payload.payment_method,
        amount=payload.amount,
        allocations=[
            PaymentAllocationInput(
                purchase_invoice_id=a.purchase_invoice_id,
                amount=a.amount,
            )
            for a in payload.allocations
        ],
        reference=payload.reference,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        created_by=current_user.id,
    )
    db.commit()
    db.refresh(payment)
    return _to_payment_read(service.get_supplier_payment(db, payment.id))


def _to_payment_read(payment) -> SupplierPaymentRead:  # type: ignore[no-untyped-def]
    # Nested `allocations` is populated automatically from the ORM
    # relationship of the same name (from_attributes=True).
    return SupplierPaymentRead.model_validate(payment)


@router.get("/payments", response_model=list[SupplierPaymentRead])
def list_payments(
    store_id: int | None = None,
    supplier_id: int | None = None,
    purchase_invoice_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
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
    return [_to_payment_read(p) for p in payments]


@router.get("/payments/{supplier_payment_id}", response_model=SupplierPaymentRead)
def get_payment(
    supplier_payment_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierPaymentRead:
    payment = service.get_supplier_payment(db, supplier_payment_id)
    if current_user.store_id is not None and payment.store_id != current_user.store_id:
        raise NotFoundError(f"Supplier payment {supplier_payment_id} not found")
    return _to_payment_read(payment)


@router.post("/payments/{supplier_payment_id}/reverse", response_model=SupplierPaymentRead)
def reverse_payment(
    supplier_payment_id: int,
    payload: SupplierReversalRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reverse_permission),
) -> SupplierPaymentRead:
    payment = service.reverse_supplier_payment(
        db,
        supplier_payment_id=supplier_payment_id,
        reason=payload.reason,
        caller_store_id=current_user.store_id,
        reversed_by=current_user.id,
    )
    db.commit()
    db.refresh(payment)
    return _to_payment_read(service.get_supplier_payment(db, payment.id))


@router.post("/credit-notes", response_model=SupplierCreditNoteRead, status_code=201)
def create_credit_note(
    payload: SupplierCreditNoteCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_credit_permission),
) -> SupplierCreditNoteRead:
    enforce_store_access(current_user, payload.store_id)
    credit_note = service.create_supplier_credit_note(
        db,
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        credit_number=payload.credit_number,
        credit_date=payload.credit_date,
        reason=payload.reason,
        purchase_return_id=payload.purchase_return_id,
        lines=[
            SupplierCreditNoteLineInput(
                description=line.description,
                amount=line.amount,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_cost=line.unit_cost,
            )
            for line in payload.lines
        ],
        allocations=[
            PaymentAllocationInput(purchase_invoice_id=a.purchase_invoice_id, amount=a.amount)
            for a in payload.allocations
        ],
        notes=payload.notes,
        client_transaction_id=payload.client_transaction_id,
        caller_store_id=current_user.store_id,
        created_by=current_user.id,
    )
    db.commit()
    db.refresh(credit_note)
    return _to_credit_note_read(db, service.get_supplier_credit_note(db, credit_note.id))


@router.get("/credit-notes", response_model=list[SupplierCreditNoteRead])
def list_credit_notes(
    store_id: int | None = None,
    supplier_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierCreditNoteRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    credit_notes = service.list_supplier_credit_notes(
        db,
        store_id=effective_store_id,
        supplier_id=supplier_id,
        limit=limit,
        offset=offset,
    )
    return [_to_credit_note_read(db, cn) for cn in credit_notes]


@router.get("/credit-notes/{supplier_credit_note_id}", response_model=SupplierCreditNoteRead)
def get_credit_note(
    supplier_credit_note_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierCreditNoteRead:
    credit_note = service.get_supplier_credit_note(db, supplier_credit_note_id)
    if current_user.store_id is not None and credit_note.store_id != current_user.store_id:
        raise NotFoundError(f"Supplier credit note {supplier_credit_note_id} not found")
    return _to_credit_note_read(db, credit_note)


@router.post(
    "/credit-notes/{supplier_credit_note_id}/reverse", response_model=SupplierCreditNoteRead
)
def reverse_credit_note(
    supplier_credit_note_id: int,
    payload: SupplierReversalRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reverse_permission),
) -> SupplierCreditNoteRead:
    credit_note = service.reverse_supplier_credit_note(
        db,
        supplier_credit_note_id=supplier_credit_note_id,
        reason=payload.reason,
        caller_store_id=current_user.store_id,
        reversed_by=current_user.id,
    )
    db.commit()
    db.refresh(credit_note)
    return _to_credit_note_read(db, service.get_supplier_credit_note(db, credit_note.id))


@router.get("/suppliers/{supplier_id}/summary", response_model=SupplierApSummaryRead)
def get_supplier_summary(
    supplier_id: int,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierApSummaryRead:
    # M21 F5 fix: Supplier is company-wide reference data (no store_id
    # query param here to begin with), but its invoices/payments/credit
    # notes are store-scoped operational rows -- scoped_store_filter(...,
    # None) silently narrows a store-scoped caller to their own store's
    # rows, exactly like list_invoices/list_payments already do for the
    # omitted-filter case, while an unrestricted caller still gets the
    # true company-wide total.
    effective_store_id = scoped_store_filter(current_user, None)
    summary = service.get_supplier_ap_summary(
        db, supplier_id, as_of=as_of, store_id=effective_store_id
    )
    return SupplierApSummaryRead(**summary.__dict__)


@router.get("/suppliers/{supplier_id}/transactions", response_model=list[SupplierTransactionRead])
def get_supplier_transactions(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[SupplierTransactionRead]:
    effective_store_id = scoped_store_filter(current_user, None)
    transactions = service.get_supplier_transaction_history(
        db, supplier_id, store_id=effective_store_id
    )
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


@router.get("/suppliers/{supplier_id}/statement", response_model=SupplierStatementRead)
def get_supplier_statement(
    supplier_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> SupplierStatementRead:
    effective_store_id = scoped_store_filter(current_user, None)
    statement = service.get_supplier_statement(
        db, supplier_id, date_from=date_from, date_to=date_to, store_id=effective_store_id
    )
    return SupplierStatementRead(
        supplier_id=statement.supplier_id,
        date_from=statement.date_from,
        date_to=statement.date_to,
        opening_balance=statement.opening_balance,
        closing_balance=statement.closing_balance,
        lines=[
            SupplierStatementLineRead(
                date=line.date_,
                transaction_type=line.transaction_type,
                reference=line.reference,
                amount=line.amount,
                running_balance=line.running_balance,
            )
            for line in statement.lines
        ],
    )


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
