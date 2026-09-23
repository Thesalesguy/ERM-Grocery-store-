"""Accounts Payable business logic: purchase-invoice lifecycle, three-way
matching against purchase orders/goods receipts, Purchase Clearing
clearing, multi-invoice supplier-payment allocation, and supplier credit
notes.

See docs/M7_ADVANCED_AP_SETTLEMENT.md for the full M7 design and
docs/M6_AP_VENDOR_ACCOUNTING.md for the original M6 design this extends.

The single most important technique in this file is still FIFO receipt-lot
matching (`_fifo_match_slices`): a PurchaseOrderItem can be received across
several GoodsReceipts at different costs, so "the cost of the units being
invoiced" is computed by walking that item's GoodsReceiptItem rows in
receipt order, skipping whatever earlier invoices already consumed. M7's
change is that the per-lot slices this walk produces are now PERSISTED
(`PurchaseInvoiceReceiptMatch`) instead of only being summed into a total —
this is what lets an invoice legitimately span multiple receipts (already
true in M6) AND multiple purchase orders (new in M7) while still answering
"why does this Purchase Clearing/AP amount exist" from real rows, and lets
`void_purchase_invoice` undo an exact posting without ever recomputing
FIFO (unsafe once other invoices have posted against the same items).

Precision note: a match row's own `variance_amount` is rounded to money
(2dp) for readability as a documentary/audit record — the actual posted
journal amounts are computed independently from the RAW (unrounded)
per-slice arithmetic, summed once, and ledger-quantized (6dp) exactly once
at the end. The two are not required to sub-cent-agree; a per-row
rounding artifact must never leak into what gets posted.

Two commit conventions coexist here, matching every other service module:
`create_purchase_invoice` (a DRAFT has no accounting/quantity effect) on
one hand, and `post_purchase_invoice`, `void_purchase_invoice` on a POSTED
invoice, `record_supplier_payment`, and `create_supplier_credit_note` —
each a genuinely atomic multi-effect transaction — on the other, which
never call commit()/rollback() themselves; the caller commits once.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import (
    ACCOUNT_ACCOUNTS_PAYABLE,
    ACCOUNT_PURCHASE_CLEARING,
    ACCOUNT_PURCHASE_PRICE_VARIANCE,
)
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.ap.models import (
    SUPPLIER_CREDIT_NOTE_REASONS,
    SUPPLIER_PAYMENT_METHODS,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseInvoiceReceiptMatch,
    SupplierCreditAllocation,
    SupplierCreditNote,
    SupplierCreditNoteLine,
    SupplierCreditNoteReversal,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from app.modules.audit import service as audit_service
from app.modules.auth.models import Store
from app.modules.products.models import Product
from app.modules.purchasing.models import (
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    Supplier,
)

_MONEY_QUANTUM = Decimal("0.01")
# Matches accounting/service.py's own _LEDGER_QUANTUM exactly — clearing
# and price-variance amounts must be quantized identically to how
# post_goods_receipt_journal originally quantized the Purchase Clearing
# credit they are now clearing, or a fresh, fabricated rounding mismatch
# would appear in inventory/AP reconciliation for no real reason.
_LEDGER_QUANTUM = Decimal("0.000001")

_OUTSTANDING_INVOICE_STATUSES = ("POSTED", "PARTIALLY_PAID")
_NON_ACCOUNTING_INVOICE_STATUSES = ("DRAFT", "VOIDED")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _ledger_quantize(value: Decimal) -> Decimal:
    return value.quantize(_LEDGER_QUANTUM, rounding=ROUND_HALF_UP)


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Mirrors app.modules.purchasing.service._enforce_store_access /
    app.modules.sales.service._enforce_store_access exactly — duplicated,
    not imported, so this module stays independently testable by calling
    its functions directly (M2 hardening audit Section 12's rule)."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access "
            f"{noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


def _outstanding_balance(invoice: PurchaseInvoice) -> Decimal:
    """The single definition of "what is still owed on this invoice" used
    everywhere in this module — grand_total minus BOTH settlement paths
    (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 16)."""
    return invoice.grand_total - invoice.amount_paid - invoice.amount_credited


def _recompute_invoice_status(invoice: PurchaseInvoice) -> None:
    settled = invoice.amount_paid + invoice.amount_credited
    if settled >= invoice.grand_total:
        invoice.status = "PAID"
    elif settled > 0:
        invoice.status = "PARTIALLY_PAID"
    else:
        invoice.status = "POSTED"


# --- Purchase invoice creation (DRAFT) --------------------------------------


@dataclass(frozen=True)
class PurchaseInvoiceLineInput:
    purchase_order_item_id: int
    quantity_invoiced: Decimal
    unit_price: Decimal
    discount_amount: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    description: str | None = None


def _generate_invoice_client_signature(
    supplier_id: int,
    invoice_number: str,
    lines: list[PurchaseInvoiceLineInput],
) -> tuple:
    return (
        supplier_id,
        invoice_number,
        tuple(
            sorted(
                (
                    line.purchase_order_item_id,
                    line.quantity_invoiced,
                    line.unit_price,
                    line.discount_amount,
                    line.tax_amount,
                )
                for line in lines
            )
        ),
    )


def _match_or_reject_idempotent_invoice(
    db: Session,
    *,
    client_transaction_id: str,
    supplier_id: int,
    invoice_number: str,
    lines: list[PurchaseInvoiceLineInput],
) -> PurchaseInvoice | None:
    """Mirrors app.modules.sales.service._match_or_reject_idempotent_return
    exactly, including WHY it matters under concurrency: see this module's
    create_purchase_invoice docstring.

    M7: no longer includes purchase_order_id in the signature —
    purchase_order_id is now a non-authoritative display field (an
    invoice's lines, not its header, define what it actually matches), so
    two requests differing only in that field would otherwise be treated
    as genuinely different when they are not."""
    existing = db.execute(
        select(PurchaseInvoice).where(
            PurchaseInvoice.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing_lines = (
        db.execute(
            select(PurchaseInvoiceLine).where(
                PurchaseInvoiceLine.purchase_invoice_id == existing.id
            )
        )
        .scalars()
        .all()
    )
    existing_signature = (
        existing.supplier_id,
        existing.invoice_number,
        tuple(
            sorted(
                (
                    line.purchase_order_item_id,
                    line.quantity_invoiced,
                    line.unit_price,
                    line.discount_amount,
                    line.tax_amount,
                )
                for line in existing_lines
            )
        ),
    )
    requested_signature = _generate_invoice_client_signature(supplier_id, invoice_number, lines)
    if existing_signature != requested_signature:
        raise ConflictError(
            f"client_transaction_id {client_transaction_id!r} was already used for a "
            "different invoice request",
            error_code="IDEMPOTENCY_KEY_CONFLICT",
        )
    return existing


def create_purchase_invoice(
    db: Session,
    *,
    store_id: int,
    supplier_id: int,
    invoice_number: str,
    invoice_date: date,
    lines: list[PurchaseInvoiceLineInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    purchase_order_id: int | None = None,
    due_date: date | None = None,
    notes: str | None = None,
    created_by: int | None = None,
) -> PurchaseInvoice:
    """Records a supplier invoice as a DRAFT — no accounting effect, no
    quantity_invoiced change, freely re-creatable-if-wrong (delete via
    void_purchase_invoice) up until post_purchase_invoice commits it
    financially (docs/M7_ADVANCED_AP_SETTLEMENT.md "Invoice lifecycle").

    M7: `purchase_order_id` is now OPTIONAL and purely informational — an
    invoice's lines may reference purchase_order_items belonging to
    DIFFERENT purchase orders, as long as every one of those purchase
    orders belongs to the SAME `store_id`/`supplier_id` given here (each
    line is validated independently; there is no requirement that they
    share one PO). If `purchase_order_id` is not given and every line
    happens to reference the same single PO, it is filled in automatically
    for display/filtering convenience — never used to validate anything.

    Two INDEPENDENT duplicate-detection mechanisms, for two different
    real failure modes (both required by M6 task Section 27's "duplicate
    invoice" mutation target, unchanged in M7):
    1. `client_transaction_id` idempotency.
    2. `(supplier_id, invoice_number)` uniqueness.
    """
    existing = _match_or_reject_idempotent_invoice(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        invoice_number=invoice_number,
        lines=lines,
    )
    if existing is not None:
        return existing

    _enforce_store_access(caller_store_id, store_id, "purchase invoices")

    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise ValidationAppError(
            f"Supplier {supplier_id} does not exist or is inactive", error_code="INVALID_SUPPLIER"
        )
    if purchase_order_id is not None:
        primary_po = db.get(PurchaseOrder, purchase_order_id)
        if primary_po is None:
            raise NotFoundError(f"Purchase order {purchase_order_id} not found")
        if primary_po.store_id != store_id:
            raise ConflictError(
                f"Purchase order {purchase_order_id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        if primary_po.supplier_id != supplier_id:
            raise ConflictError(
                f"Purchase order {purchase_order_id} does not belong to supplier {supplier_id}",
                error_code="SUPPLIER_MISMATCH",
            )
    if not invoice_number.strip():
        raise ValidationAppError("Invoice number is required", error_code="INVALID_INVOICE_NUMBER")

    if not lines:
        raise ValidationAppError(
            "An invoice must have at least one line", error_code="EMPTY_INVOICE"
        )
    for line in lines:
        if line.quantity_invoiced <= 0:
            raise ValidationAppError(
                "Invoiced quantity must be positive", error_code="INVALID_QUANTITY"
            )
        if line.unit_price < 0:
            raise ValidationAppError(
                "Unit price cannot be negative", error_code="INVALID_UNIT_PRICE"
            )
        if line.discount_amount < 0 or line.tax_amount < 0:
            raise ValidationAppError(
                "Discount/tax amounts cannot be negative", error_code="INVALID_AMOUNT"
            )

    # Batched into two-then-two IN(...) queries rather than one db.get()
    # per line per table (mirrors M6/M5's identical N+1 fix) — and, new in
    # M7, used to validate EACH line's own purchase order against
    # store_id/supplier_id, since the header purchase_order_id no longer
    # does that job.
    po_item_ids = {line.purchase_order_item_id for line in lines}
    po_items_by_id = {
        item.id: item
        for item in db.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.id.in_(po_item_ids))
        ).scalars()
    }
    missing_item_ids = po_item_ids - set(po_items_by_id)
    if missing_item_ids:
        raise NotFoundError(f"Purchase order item(s) {sorted(missing_item_ids)} not found")

    po_ids_needed = {item.purchase_order_id for item in po_items_by_id.values()}
    pos_by_id = {
        po.id: po
        for po in db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id.in_(po_ids_needed))
        ).scalars()
    }
    for po_item in po_items_by_id.values():
        po = pos_by_id.get(po_item.purchase_order_id)
        if po is None or po.store_id != store_id or po.supplier_id != supplier_id:
            raise ConflictError(
                f"Purchase order item {po_item.id} belongs to a purchase order that is not "
                f"in store {store_id} for supplier {supplier_id}",
                error_code="PO_ITEM_STORE_SUPPLIER_MISMATCH",
            )

    resolved_purchase_order_id = purchase_order_id
    if resolved_purchase_order_id is None:
        distinct_pos = {item.purchase_order_id for item in po_items_by_id.values()}
        if len(distinct_pos) == 1:
            resolved_purchase_order_id = next(iter(distinct_pos))

    resolved_due_date = due_date
    if resolved_due_date is None:
        term_days = supplier.default_payment_terms_days or 0
        resolved_due_date = invoice_date + timedelta(days=term_days)

    invoice = PurchaseInvoice(
        store_id=store_id,
        supplier_id=supplier_id,
        purchase_order_id=resolved_purchase_order_id,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=resolved_due_date,
        status="DRAFT",
        subtotal=Decimal("0"),
        discount_total=Decimal("0"),
        tax_total=Decimal("0"),
        grand_total=Decimal("0"),
        amount_paid=Decimal("0"),
        amount_credited=Decimal("0"),
        client_transaction_id=client_transaction_id,
        notes=notes,
        created_by=created_by,
    )
    db.add(invoice)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_purchase_invoices_supplier_invoice_number":
            raise ConflictError(
                f"Supplier {supplier_id} already has an invoice numbered {invoice_number!r}",
                error_code="DUPLICATE_SUPPLIER_INVOICE_NUMBER",
            ) from exc
        # Otherwise assume a genuinely concurrent duplicate client_transaction_id
        # submission — see finalize_sale's identical recovery block.
        winner = db.execute(
            select(PurchaseInvoice).where(
                PurchaseInvoice.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    product_ids = {item.product_id for item in po_items_by_id.values()}
    products_by_id = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }

    subtotal = Decimal("0")
    discount_total = Decimal("0")
    tax_total = Decimal("0")
    for line in lines:
        po_item = po_items_by_id[line.purchase_order_item_id]
        product = products_by_id.get(po_item.product_id)
        description = line.description or (
            product.name if product else f"Product {po_item.product_id}"
        )
        line_subtotal = _round_money(line.quantity_invoiced * line.unit_price)
        line_total = line_subtotal - line.discount_amount + line.tax_amount
        subtotal += line_subtotal
        discount_total += line.discount_amount
        tax_total += line.tax_amount
        db.add(
            PurchaseInvoiceLine(
                purchase_invoice_id=invoice.id,
                purchase_order_item_id=po_item.id,
                product_id=po_item.product_id,
                description=description,
                quantity_invoiced=line.quantity_invoiced,
                unit_price=line.unit_price,
                discount_amount=line.discount_amount,
                tax_amount=line.tax_amount,
                line_total=line_total,
            )
        )

    invoice.subtotal = subtotal
    invoice.discount_total = discount_total
    invoice.tax_total = tax_total
    invoice.grand_total = subtotal - discount_total + tax_total

    audit_service.log_event(
        db,
        user_id=created_by,
        action="PURCHASE_INVOICE_CREATED",
        entity_type="purchase_invoice",
        entity_id=invoice.id,
        after={
            "supplier_id": supplier_id,
            "purchase_order_ids": sorted(po_ids_needed),
            "invoice_number": invoice_number,
            "grand_total": str(invoice.grand_total),
            "line_count": len(lines),
        },
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def get_purchase_invoice(db: Session, purchase_invoice_id: int) -> PurchaseInvoice:
    invoice = db.get(PurchaseInvoice, purchase_invoice_id)
    if invoice is None:
        raise NotFoundError(f"Purchase invoice {purchase_invoice_id} not found")
    return invoice


def list_purchase_invoices(
    db: Session,
    *,
    store_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PurchaseInvoice]:
    query = (
        select(PurchaseInvoice)
        .order_by(PurchaseInvoice.invoice_date.desc(), PurchaseInvoice.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(PurchaseInvoice.store_id == store_id)
    if supplier_id is not None:
        query = query.where(PurchaseInvoice.supplier_id == supplier_id)
    if status is not None:
        query = query.where(PurchaseInvoice.status == status)
    return list(db.execute(query).scalars().all())


# --- Three-way matching ------------------------------------------------------


@dataclass(frozen=True)
class PurchaseOrderItemMatchStatus:
    purchase_order_item_id: int
    purchase_order_id: int
    product_id: int
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_invoiced: Decimal
    quantity_invoiceable: Decimal  # quantity_received - quantity_invoiced


def get_invoice_matching_status(
    db: Session, purchase_order_id: int
) -> list[PurchaseOrderItemMatchStatus]:
    """Read-only three-way-match preview for ONE PO — mirrors
    app.modules.sales.service.get_return_eligibility's read-only-helper
    shape. `quantity_invoiceable` is the exact ceiling
    post_purchase_invoice enforces: never received, ordered."""
    purchase_order = db.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    return [
        PurchaseOrderItemMatchStatus(
            purchase_order_item_id=item.id,
            purchase_order_id=purchase_order_id,
            product_id=item.product_id,
            quantity_ordered=item.quantity_ordered,
            quantity_received=item.quantity_received,
            quantity_invoiced=item.quantity_invoiced,
            quantity_invoiceable=item.quantity_received - item.quantity_invoiced,
        )
        for item in purchase_order.items
    ]


def get_invoice_matching_status_multi(
    db: Session, purchase_order_ids: list[int]
) -> list[PurchaseOrderItemMatchStatus]:
    """M7: the multi-PO equivalent of get_invoice_matching_status, for the
    "one invoice across multiple POs" workflow (docs/M7_ADVANCED_AP_SETTLEMENT.md
    Section 1) — the frontend calls this once instead of stitching together
    several single-PO calls itself."""
    rows: list[PurchaseOrderItemMatchStatus] = []
    for po_id in purchase_order_ids:
        rows.extend(get_invoice_matching_status(db, po_id))
    return rows


def _fifo_match_slices(
    db: Session,
    *,
    purchase_order_item_id: int,
    already_invoiced_qty: Decimal,
    additional_qty: Decimal,
) -> list[tuple[int, Decimal, Decimal]]:
    """Walks `purchase_order_item_id`'s GoodsReceiptItem lots in receipt
    order (oldest first), skipping `already_invoiced_qty` units already
    consumed by earlier invoices/lines against this same item, and returns
    the list of (goods_receipt_item_id, matched_quantity, matched_unit_cost)
    slices that together make up `additional_qty` more units being
    invoiced. This is the persisted-match version of what M6 only summed
    on the fly — see the module docstring.

    Callers must have already validated `already_invoiced_qty +
    additional_qty <= <that item's quantity_received>` — this function
    trusts that invariant and raises a defensive (should-be-unreachable)
    ConflictError if the receipt data doesn't actually cover the
    requested range, rather than silently returning a wrong number.
    """
    receipt_items = (
        db.execute(
            select(GoodsReceiptItem)
            .where(GoodsReceiptItem.purchase_order_item_id == purchase_order_item_id)
            .order_by(GoodsReceiptItem.id)
        )
        .scalars()
        .all()
    )
    skip = already_invoiced_qty
    remaining = additional_qty
    slices: list[tuple[int, Decimal, Decimal]] = []
    for receipt_item in receipt_items:
        lot_qty = receipt_item.quantity_received
        if skip >= lot_qty:
            skip -= lot_qty
            continue
        available = lot_qty - skip
        skip = Decimal("0")
        take = min(available, remaining)
        if take > 0:
            slices.append((receipt_item.id, take, receipt_item.unit_cost))
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        raise ConflictError(
            f"Purchase order item {purchase_order_item_id}'s receipt data does not cover "
            f"the requested invoiced range — this indicates a data inconsistency, not a "
            "normal validation failure",
            error_code="MATCH_DATA_INCONSISTENT",
        )
    return slices


def _fifo_clearing_amount(
    db: Session,
    *,
    purchase_order_item_id: int,
    already_invoiced_qty: Decimal,
    additional_qty: Decimal,
) -> Decimal:
    """Aggregate-only convenience over _fifo_match_slices, used by the
    read-only reporting functions below (get_supplier_ap_summary,
    purchase_clearing_reconciliation) that only need a total value for
    STILL-UNINVOICED quantity, never a persisted per-lot record."""
    slices = _fifo_match_slices(
        db,
        purchase_order_item_id=purchase_order_item_id,
        already_invoiced_qty=already_invoiced_qty,
        additional_qty=additional_qty,
    )
    total = sum((qty * cost for _, qty, cost in slices), Decimal("0"))
    return _ledger_quantize(total)


# --- Posting (the financial commitment point) -------------------------------


def post_purchase_invoice(
    db: Session,
    *,
    purchase_invoice_id: int,
    caller_store_id: int | None,
    posted_by: int | None = None,
) -> PurchaseInvoice:
    """DRAFT -> POSTED: atomically validates three-way matching (no line
    may push cumulative quantity_invoiced past that PO item's
    quantity_received), computes the FIFO-matched clearing amount and
    price variance per LINE (persisting each receipt-lot slice consumed as
    a PurchaseInvoiceReceiptMatch row), updates quantity_invoiced, and
    posts the Purchase Clearing/AP/variance/tax/discount journal — all in
    this one transaction.

    Idempotent by state, not by a separate key (mirrors
    submit_purchase_order's status-transition precedent): calling this on
    an already-POSTED/PARTIALLY_PAID/PAID invoice is a no-op that returns
    the current state.

    Locking: an invoice's lines may now reference items across MULTIPLE
    purchase orders (M7). Every one of those PurchaseOrder rows is locked
    — in ascending id order, a fixed, deterministic order every caller
    (this function, receive_goods, and void_purchase_invoice) follows, so
    two transactions racing over overlapping PO sets can never deadlock —
    before touching any of their items. The invoice's own status is
    re-checked immediately after acquiring those locks (the M5-discovered
    pattern: a racing caller that already posted this exact invoice may
    have committed while this caller waited)."""
    invoice = get_purchase_invoice(db, purchase_invoice_id)
    _enforce_store_access(caller_store_id, invoice.store_id, "this purchase invoice")
    if invoice.status != "DRAFT":
        if invoice.status == "VOIDED":
            raise ConflictError(
                f"Purchase invoice {purchase_invoice_id} is VOIDED and cannot be posted",
                error_code="INVALID_INVOICE_STATE",
            )
        return invoice

    lines = list(invoice.lines)
    if not lines:
        raise ConflictError(
            f"Purchase invoice {purchase_invoice_id} has no lines", error_code="EMPTY_INVOICE"
        )

    po_item_ids = sorted({line.purchase_order_item_id for line in lines})
    # Selecting only the scalar column (not full ORM entities) is
    # deliberate: a full-entity SELECT here would populate the session's
    # identity map with PurchaseOrderItem objects BEFORE the lock below is
    # acquired, and SQLAlchemy does not refresh already-tracked entities'
    # attributes from a later query by default — the "fresh" read after
    # the lock would silently return the same stale (pre-lock)
    # quantity_invoiced values. A real concurrency test caught exactly
    # this: two concurrent postings both succeeded because each saw the
    # other's stale, pre-commit quantity_invoiced.
    po_ids = sorted(
        {
            row[0]
            for row in db.execute(
                select(PurchaseOrderItem.purchase_order_id).where(
                    PurchaseOrderItem.id.in_(po_item_ids)
                )
            )
        }
    )
    for po_id in po_ids:
        db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == po_id).with_for_update()
        ).scalar_one_or_none()

    db.refresh(invoice)
    if invoice.status != "DRAFT":
        if invoice.status == "VOIDED":
            raise ConflictError(
                f"Purchase invoice {purchase_invoice_id} is VOIDED and cannot be posted",
                error_code="INVALID_INVOICE_STATE",
            )
        return invoice

    po_items = {
        item.id: item
        for item in db.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.id.in_(po_item_ids))
        ).scalars()
    }

    requested_by_item: dict[int, Decimal] = {}
    for line in lines:
        requested_by_item[line.purchase_order_item_id] = (
            requested_by_item.get(line.purchase_order_item_id, Decimal("0"))
            + line.quantity_invoiced
        )

    for po_item_id, requested_qty in requested_by_item.items():
        po_item = po_items.get(po_item_id)
        if po_item is None:
            # Unreachable in practice: create_purchase_invoice already
            # validated every line's purchase_order_item_id. Guarded
            # anyway rather than trusting that forever.
            raise NotFoundError(f"Purchase order item {po_item_id} not found")
        new_total = po_item.quantity_invoiced + requested_qty
        if new_total > po_item.quantity_received:
            raise ConflictError(
                f"Cannot invoice {requested_qty} of purchase order item {po_item_id}: only "
                f"{po_item.quantity_received - po_item.quantity_invoiced} remains invoiceable "
                f"(of {po_item.quantity_received} received)",
                error_code="OVER_INVOICING",
            )

    # Second pass: FIFO-consume per LINE (stable id order, so two lines
    # referencing the same po_item on this invoice consume adjacent,
    # non-overlapping lots), persisting each slice consumed.
    consumed_this_posting: dict[int, Decimal] = {}
    total_clearing_raw = Decimal("0")
    total_variance_raw = Decimal("0")
    for line in sorted(lines, key=lambda item: item.id):
        po_item = po_items[line.purchase_order_item_id]
        already = po_item.quantity_invoiced + consumed_this_posting.get(po_item.id, Decimal("0"))
        slices = _fifo_match_slices(
            db,
            purchase_order_item_id=po_item.id,
            already_invoiced_qty=already,
            additional_qty=line.quantity_invoiced,
        )
        consumed_this_posting[po_item.id] = (
            consumed_this_posting.get(po_item.id, Decimal("0")) + line.quantity_invoiced
        )
        for receipt_item_id, slice_qty, slice_cost in slices:
            raw_variance = slice_qty * (line.unit_price - slice_cost)
            total_clearing_raw += slice_qty * slice_cost
            total_variance_raw += raw_variance
            db.add(
                PurchaseInvoiceReceiptMatch(
                    purchase_invoice_line_id=line.id,
                    goods_receipt_item_id=receipt_item_id,
                    matched_quantity=slice_qty,
                    matched_unit_cost=slice_cost,
                    variance_amount=_round_money(raw_variance),
                )
            )

    for po_item_id, requested_qty in requested_by_item.items():
        po_item = po_items[po_item_id]
        po_item.quantity_invoiced = po_item.quantity_invoiced + requested_qty

    total_clearing = _ledger_quantize(total_clearing_raw)
    total_variance = _ledger_quantize(total_variance_raw)

    invoice.status = "POSTED"
    invoice.posted_at = datetime.now(UTC)

    audit_service.log_event(
        db,
        user_id=posted_by,
        action="PURCHASE_INVOICE_POSTED",
        entity_type="purchase_invoice",
        entity_id=invoice.id,
        after={
            "grand_total": str(invoice.grand_total),
            "clearing_amount": str(total_clearing),
            "price_variance_amount": str(total_variance),
            "purchase_order_ids": po_ids,
        },
    )

    accounting_service.post_purchase_invoice_journal(
        db,
        purchase_invoice=invoice,
        clearing_amount=total_clearing,
        price_variance_amount=total_variance,
        created_by=posted_by,
    )

    db.flush()
    return invoice


def _clearing_and_variance_from_matches(
    db: Session, purchase_invoice_id: int
) -> tuple[Decimal, Decimal]:
    """Reads the EXACT clearing_amount/price_variance_amount posted by a
    prior post_purchase_invoice call, straight off its own persisted
    PurchaseInvoiceReceiptMatch rows — used by void_purchase_invoice so a
    void's reversal is provably the mirror of what was actually posted,
    never a fresh recomputation (unsafe once OTHER invoices against the
    same PO items have posted in between)."""
    rows = db.execute(
        select(
            PurchaseInvoiceReceiptMatch.matched_quantity,
            PurchaseInvoiceReceiptMatch.matched_unit_cost,
            PurchaseInvoiceLine.unit_price,
        )
        .join(
            PurchaseInvoiceLine,
            PurchaseInvoiceLine.id == PurchaseInvoiceReceiptMatch.purchase_invoice_line_id,
        )
        .where(PurchaseInvoiceLine.purchase_invoice_id == purchase_invoice_id)
    ).all()
    total_clearing_raw = Decimal("0")
    total_variance_raw = Decimal("0")
    for qty, cost, unit_price in rows:
        qty = Decimal(qty)
        cost = Decimal(cost)
        unit_price = Decimal(unit_price)
        total_clearing_raw += qty * cost
        total_variance_raw += qty * (unit_price - cost)
    return _ledger_quantize(total_clearing_raw), _ledger_quantize(total_variance_raw)


def _extract_clearing_and_variance_from_journal(
    db: Session, journal_entry_id: int
) -> tuple[Decimal, Decimal]:
    """Legacy fallback for an invoice POSTED before M7 (no persisted
    PurchaseInvoiceReceiptMatch rows exist for it) — reads the original
    posting's own journal lines back instead. See
    _clearing_and_variance_from_matches for the preferred, M7 path."""
    rows = db.execute(
        select(Account.code, JournalLine.debit, JournalLine.credit)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.journal_entry_id == journal_entry_id,
            Account.code.in_([ACCOUNT_PURCHASE_CLEARING, ACCOUNT_PURCHASE_PRICE_VARIANCE]),
        )
    ).all()
    clearing_amount = Decimal("0")
    variance_amount = Decimal("0")
    for code, debit, credit in rows:
        if code == ACCOUNT_PURCHASE_CLEARING:
            clearing_amount += Decimal(debit) - Decimal(credit)
        else:
            variance_amount += Decimal(debit) - Decimal(credit)
    return clearing_amount, variance_amount


def void_purchase_invoice(
    db: Session,
    *,
    purchase_invoice_id: int,
    caller_store_id: int | None,
    reason: str | None = None,
    voided_by: int | None = None,
) -> PurchaseInvoice:
    """Voids a DRAFT (a pure status flip) or a POSTED-but-unsettled invoice
    (amount_paid == 0 AND amount_credited == 0) — reverses
    quantity_invoiced and posts an exact compensating
    PURCHASE_INVOICE_VOID journal entry.

    Deliberately scoped to invoices with no payments or credits applied
    (PARTIALLY_PAID/PAID are rejected with INVOICE_HAS_PAYMENTS) — M7 does
    not implement unwinding a supplier payment or credit note as part of a
    void (docs/M7_ADVANCED_AP_SETTLEMENT.md "Deferred").

    Locking mirrors post_purchase_invoice: every PurchaseOrder this
    invoice's lines touch is locked, in the same ascending-id order, before
    touching any of their quantity_invoiced."""
    invoice = db.execute(
        select(PurchaseInvoice).where(PurchaseInvoice.id == purchase_invoice_id).with_for_update()
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"Purchase invoice {purchase_invoice_id} not found")
    _enforce_store_access(caller_store_id, invoice.store_id, "this purchase invoice")

    if invoice.status == "VOIDED":
        return invoice
    if invoice.status in ("PARTIALLY_PAID", "PAID"):
        raise ConflictError(
            f"Purchase invoice {purchase_invoice_id} has payments or credits recorded against "
            "it and cannot be voided (a future refund workflow would handle this)",
            error_code="INVOICE_HAS_PAYMENTS",
        )

    was_posted = invoice.status == "POSTED"
    if was_posted:
        po_item_ids = sorted({line.purchase_order_item_id for line in invoice.lines})
        # Scalar-column select, not a full-entity SELECT — see the
        # identical comment in post_purchase_invoice for why: avoids
        # pre-populating the identity map with PurchaseOrderItem objects
        # before the lock below, which would make the later "fresh" read
        # silently stale.
        po_ids = sorted(
            {
                row[0]
                for row in db.execute(
                    select(PurchaseOrderItem.purchase_order_id).where(
                        PurchaseOrderItem.id.in_(po_item_ids)
                    )
                )
            }
        )
        for po_id in po_ids:
            db.execute(
                select(PurchaseOrder).where(PurchaseOrder.id == po_id).with_for_update()
            ).scalar_one_or_none()

        has_matches = (
            db.execute(
                select(PurchaseInvoiceReceiptMatch.id)
                .join(
                    PurchaseInvoiceLine,
                    PurchaseInvoiceLine.id == PurchaseInvoiceReceiptMatch.purchase_invoice_line_id,
                )
                .where(PurchaseInvoiceLine.purchase_invoice_id == invoice.id)
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        if has_matches:
            clearing_amount, variance_amount = _clearing_and_variance_from_matches(db, invoice.id)
        else:
            original_journal = db.execute(
                select(JournalEntry).where(
                    JournalEntry.source_type == "PURCHASE_INVOICE",
                    JournalEntry.source_id == invoice.id,
                )
            ).scalar_one_or_none()
            clearing_amount = Decimal("0")
            variance_amount = Decimal("0")
            if original_journal is not None:
                clearing_amount, variance_amount = _extract_clearing_and_variance_from_journal(
                    db, original_journal.id
                )

        po_items = {
            item.id: item
            for item in db.execute(
                select(PurchaseOrderItem).where(PurchaseOrderItem.id.in_(po_item_ids))
            ).scalars()
        }
        for line in invoice.lines:
            po_items[line.purchase_order_item_id].quantity_invoiced = (
                po_items[line.purchase_order_item_id].quantity_invoiced - line.quantity_invoiced
            )

        accounting_service.post_purchase_invoice_void_journal(
            db,
            purchase_invoice=invoice,
            clearing_amount=clearing_amount,
            price_variance_amount=variance_amount,
            created_by=voided_by,
        )

    invoice.status = "VOIDED"
    invoice.voided_at = datetime.now(UTC)

    audit_service.log_event(
        db,
        user_id=voided_by,
        action="PURCHASE_INVOICE_VOIDED",
        entity_type="purchase_invoice",
        entity_id=invoice.id,
        before={"status": "POSTED" if was_posted else "DRAFT"},
        after={"status": "VOIDED", "reason": reason},
    )

    if was_posted:
        db.flush()
    else:
        db.commit()
        db.refresh(invoice)
    return invoice


# --- Supplier payments (M7: header + allocations) ---------------------------


@dataclass(frozen=True)
class PaymentAllocationInput:
    purchase_invoice_id: int
    amount: Decimal


def _generate_payment_signature(
    supplier_id: int,
    amount: Decimal,
    payment_method: str,
    allocations: list[PaymentAllocationInput],
) -> tuple:
    return (
        supplier_id,
        amount,
        payment_method,
        tuple(sorted((a.purchase_invoice_id, a.amount) for a in allocations)),
    )


def _match_or_reject_idempotent_payment(
    db: Session,
    *,
    client_transaction_id: str,
    supplier_id: int,
    amount: Decimal,
    payment_method: str,
    allocations: list[PaymentAllocationInput],
) -> SupplierPayment | None:
    existing = db.execute(
        select(SupplierPayment).where(
            SupplierPayment.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing_allocations = (
        db.execute(
            select(SupplierPaymentAllocation).where(
                SupplierPaymentAllocation.supplier_payment_id == existing.id
            )
        )
        .scalars()
        .all()
    )
    existing_signature = (
        existing.supplier_id,
        existing.amount,
        existing.payment_method,
        tuple(sorted((a.purchase_invoice_id, a.amount) for a in existing_allocations)),
    )
    requested_signature = _generate_payment_signature(
        supplier_id, amount, payment_method, allocations
    )
    if existing_signature != requested_signature:
        raise ConflictError(
            f"client_transaction_id {client_transaction_id!r} was already used for a "
            "different payment request",
            error_code="IDEMPOTENCY_KEY_CONFLICT",
        )
    return existing


def record_supplier_payment(
    db: Session,
    *,
    store_id: int,
    supplier_id: int,
    payment_date: date,
    payment_method: str,
    amount: Decimal,
    allocations: list[PaymentAllocationInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    reference: str | None = None,
    created_by: int | None = None,
) -> SupplierPayment:
    """Atomically: idempotency fast path -> validate -> lock every
    allocated PurchaseInvoice (ascending id order — the same deterministic
    ordering post_purchase_invoice/void_purchase_invoice use for
    PurchaseOrder locks, for the same deadlock-avoidance reason) ->
    idempotency re-check (post-lock) -> validate invoice states/amounts ->
    apply -> audit -> post ONE accounting journal for the payment's whole
    amount -> return (caller commits).

    M7 (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 8/9/13): `allocations`
    may name several invoices (one payment settling multiple invoices) and
    an invoice may already carry other payments (the M6
    one-payment-per-invoice limit is gone). `allocations` must sum to
    EXACTLY `amount` — an unapplied remainder is rejected outright
    (ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT), not modeled as an advance (see
    the design doc's "Deferred" section for why). Any single allocation
    exceeding its invoice's own outstanding balance is still rejected as
    OVERPAYMENT, exactly as M6 did at the whole-payment level."""
    if payment_method not in SUPPLIER_PAYMENT_METHODS:
        raise ValidationAppError(
            f"Invalid payment method {payment_method!r}", error_code="INVALID_PAYMENT_METHOD"
        )
    if amount <= 0:
        raise ValidationAppError("Payment amount must be positive", error_code="INVALID_AMOUNT")
    if not allocations:
        raise ValidationAppError(
            "A payment must allocate to at least one invoice", error_code="EMPTY_ALLOCATIONS"
        )
    invoice_ids = [a.purchase_invoice_id for a in allocations]
    if len(set(invoice_ids)) != len(invoice_ids):
        raise ValidationAppError(
            "A payment cannot allocate to the same invoice twice",
            error_code="DUPLICATE_ALLOCATION_TARGET",
        )
    for allocation in allocations:
        if allocation.amount <= 0:
            raise ValidationAppError(
                "Each allocation amount must be positive", error_code="INVALID_AMOUNT"
            )
    allocation_total = sum((a.amount for a in allocations), Decimal("0"))
    if allocation_total != amount:
        raise ValidationAppError(
            f"Allocations sum to {allocation_total} but the payment amount is {amount} — "
            "an unapplied remainder is not supported",
            error_code="ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT",
        )

    existing = _match_or_reject_idempotent_payment(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        amount=amount,
        payment_method=payment_method,
        allocations=allocations,
    )
    if existing is not None:
        return existing

    _enforce_store_access(caller_store_id, store_id, "supplier payments")

    sorted_invoice_ids = sorted(set(invoice_ids))
    invoices = (
        db.execute(
            select(PurchaseInvoice)
            .where(PurchaseInvoice.id.in_(sorted_invoice_ids))
            .order_by(PurchaseInvoice.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    invoices_by_id = {inv.id: inv for inv in invoices}

    existing = _match_or_reject_idempotent_payment(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        amount=amount,
        payment_method=payment_method,
        allocations=allocations,
    )
    if existing is not None:
        return existing

    missing_ids = set(sorted_invoice_ids) - set(invoices_by_id)
    if missing_ids:
        raise NotFoundError(f"Purchase invoice(s) {sorted(missing_ids)} not found")

    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        if invoice.store_id != store_id:
            raise ConflictError(
                f"Purchase invoice {invoice.id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        if invoice.supplier_id != supplier_id:
            raise ConflictError(
                f"Purchase invoice {invoice.id} does not belong to supplier {supplier_id}",
                error_code="SUPPLIER_MISMATCH",
            )
        if invoice.status not in _OUTSTANDING_INVOICE_STATUSES:
            raise ConflictError(
                f"Purchase invoice {invoice.id} is {invoice.status} and cannot accept a "
                "payment (must be POSTED or PARTIALLY_PAID)",
                error_code="INVALID_INVOICE_STATE",
            )
        remaining = _outstanding_balance(invoice)
        if allocation.amount > remaining:
            raise ConflictError(
                f"Allocation of {allocation.amount} to purchase invoice {invoice.id} exceeds "
                f"its outstanding balance of {remaining}",
                error_code="OVERPAYMENT",
            )

    payment = SupplierPayment(
        store_id=store_id,
        supplier_id=supplier_id,
        payment_date=payment_date,
        payment_method=payment_method,
        amount=amount,
        reference=reference,
        client_transaction_id=client_transaction_id,
        created_by=created_by,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(SupplierPayment).where(
                SupplierPayment.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        db.add(
            SupplierPaymentAllocation(
                supplier_payment_id=payment.id,
                purchase_invoice_id=invoice.id,
                amount=allocation.amount,
            )
        )
        invoice.amount_paid = invoice.amount_paid + allocation.amount
        _recompute_invoice_status(invoice)

    audit_service.log_event(
        db,
        user_id=created_by,
        action="SUPPLIER_PAYMENT_CREATED",
        entity_type="supplier_payment",
        entity_id=payment.id,
        after={
            "supplier_id": supplier_id,
            "amount": str(amount),
            "payment_method": payment_method,
            "allocations": [
                {"purchase_invoice_id": a.purchase_invoice_id, "amount": str(a.amount)}
                for a in allocations
            ],
        },
    )

    accounting_service.post_supplier_payment_journal(
        db,
        supplier_payment=payment,
        allocated_invoice_ids=sorted_invoice_ids,
        created_by=created_by,
    )

    db.flush()
    return payment


def get_supplier_payment(db: Session, supplier_payment_id: int) -> SupplierPayment:
    payment = db.get(SupplierPayment, supplier_payment_id)
    if payment is None:
        raise NotFoundError(f"Supplier payment {supplier_payment_id} not found")
    return payment


def get_payment_allocations(
    db: Session, supplier_payment_id: int
) -> list[SupplierPaymentAllocation]:
    return list(
        db.execute(
            select(SupplierPaymentAllocation)
            .where(SupplierPaymentAllocation.supplier_payment_id == supplier_payment_id)
            .order_by(SupplierPaymentAllocation.id)
        )
        .scalars()
        .all()
    )


def reverse_supplier_payment(
    db: Session,
    *,
    supplier_payment_id: int,
    reason: str,
    caller_store_id: int | None,
    reversed_by: int | None,
) -> SupplierPayment:
    """Corrects a mis-recorded supplier payment (docs/M16_DESIGN.md "AP
    payment/credit-note correction path") — mirrors
    app.modules.payroll.service.reverse_payroll_period's exact structure:
    idempotent-by-existence (a SupplierPaymentReversal row referencing
    this payment is a domain fact, never created twice — the DB's own
    UNIQUE(supplier_payment_id) constraint is the race-safety backstop),
    no separate client_transaction_id (there is nothing to retry against
    — a genuinely new reversal ATTEMPT against an already-reversed
    payment is itself the idempotent case, exactly as payroll's own
    reversal treats it).

    Every invoice this payment allocated to has its `amount_paid`
    decremented by the reversed allocation and its status recomputed via
    `_recompute_invoice_status` — the SAME function `record_supplier_payment`
    itself uses, so every derived figure that reads amount_paid/status
    (outstanding balance, supplier AP summary, aging, statement) reflects
    the correction automatically with no separate update needed anywhere.

    Locking mirrors record_supplier_payment: the payment row itself is
    locked first (serializes two concurrent reversal attempts for the
    SAME payment), then every allocated PurchaseInvoice in ascending id
    order (serializes against a concurrent new payment/credit-note
    touching the same invoices).

    Does not commit — the caller commits once, matching this module's
    OWN dominant convention (record_supplier_payment/
    create_supplier_credit_note never self-commit either); this
    deliberately does NOT mirror payroll's post_payroll_period/
    reverse_payroll_period self-commit, which the M16 discovery audit
    flagged as an inconsistency with the rest of the codebase — this
    function does not propagate that inconsistency into AP."""
    if not reason or not reason.strip():
        raise ValidationAppError(
            "A reversal reason is required", error_code="REVERSAL_REASON_REQUIRED"
        )
    payment = db.execute(
        select(SupplierPayment).where(SupplierPayment.id == supplier_payment_id).with_for_update()
    ).scalar_one_or_none()
    if payment is None:
        raise NotFoundError(f"Supplier payment {supplier_payment_id} not found")
    _enforce_store_access(caller_store_id, payment.store_id, "this supplier payment")

    existing_reversal = db.execute(
        select(SupplierPaymentReversal).where(
            SupplierPaymentReversal.supplier_payment_id == payment.id
        )
    ).scalar_one_or_none()
    if existing_reversal is not None:
        return payment
    if reversed_by is None:
        raise ValidationAppError(
            "Reversal requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    allocations = (
        db.execute(
            select(SupplierPaymentAllocation)
            .where(SupplierPaymentAllocation.supplier_payment_id == payment.id)
            .order_by(SupplierPaymentAllocation.purchase_invoice_id)
        )
        .scalars()
        .all()
    )
    invoice_ids = sorted({a.purchase_invoice_id for a in allocations})
    invoices = (
        db.execute(
            select(PurchaseInvoice)
            .where(PurchaseInvoice.id.in_(invoice_ids))
            .order_by(PurchaseInvoice.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    invoices_by_id = {inv.id: inv for inv in invoices}
    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        invoice.amount_paid = invoice.amount_paid - allocation.amount
        _recompute_invoice_status(invoice)

    reversal_entry = accounting_service.post_supplier_payment_reversal_journal(
        db, supplier_payment=payment, created_by=reversed_by
    )

    db.add(
        SupplierPaymentReversal(
            supplier_payment_id=payment.id,
            reversal_journal_entry_id=reversal_entry.id,
            reason=reason,
            reversed_by=reversed_by,
            reversed_at=datetime.now(UTC),
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Supplier payment {supplier_payment_id} has already been reversed",
            error_code="ALREADY_REVERSED",
        ) from exc

    audit_service.log_event(
        db,
        user_id=reversed_by,
        action="SUPPLIER_PAYMENT_REVERSED",
        entity_type="supplier_payment",
        entity_id=payment.id,
        after={
            "reversal_journal_entry_id": reversal_entry.id,
            "reason": reason,
            "amount": str(payment.amount),
        },
    )
    return payment


def list_supplier_payments(
    db: Session,
    *,
    store_id: int | None = None,
    supplier_id: int | None = None,
    purchase_invoice_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SupplierPayment]:
    query = (
        select(SupplierPayment)
        .order_by(SupplierPayment.payment_date.desc(), SupplierPayment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(SupplierPayment.store_id == store_id)
    if supplier_id is not None:
        query = query.where(SupplierPayment.supplier_id == supplier_id)
    if purchase_invoice_id is not None:
        query = query.join(
            SupplierPaymentAllocation,
            SupplierPaymentAllocation.supplier_payment_id == SupplierPayment.id,
        ).where(SupplierPaymentAllocation.purchase_invoice_id == purchase_invoice_id)
    return list(db.execute(query).scalars().all())


# --- Supplier credit notes (M7) ----------------------------------------------


@dataclass(frozen=True)
class SupplierCreditNoteLineInput:
    description: str
    amount: Decimal
    product_id: int | None = None
    quantity: Decimal | None = None
    unit_cost: Decimal | None = None


def _generate_credit_note_signature(
    supplier_id: int,
    credit_number: str,
    reason: str,
    lines: list[SupplierCreditNoteLineInput],
    allocations: list[PaymentAllocationInput],
) -> tuple:
    return (
        supplier_id,
        credit_number,
        reason,
        tuple(sorted((line.description, line.amount) for line in lines)),
        tuple(sorted((a.purchase_invoice_id, a.amount) for a in allocations)),
    )


def _match_or_reject_idempotent_credit_note(
    db: Session,
    *,
    client_transaction_id: str,
    supplier_id: int,
    credit_number: str,
    reason: str,
    lines: list[SupplierCreditNoteLineInput],
    allocations: list[PaymentAllocationInput],
) -> SupplierCreditNote | None:
    existing = db.execute(
        select(SupplierCreditNote).where(
            SupplierCreditNote.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing_lines = (
        db.execute(
            select(SupplierCreditNoteLine).where(
                SupplierCreditNoteLine.supplier_credit_note_id == existing.id
            )
        )
        .scalars()
        .all()
    )
    existing_allocations = (
        db.execute(
            select(SupplierCreditAllocation).where(
                SupplierCreditAllocation.supplier_credit_note_id == existing.id
            )
        )
        .scalars()
        .all()
    )
    existing_signature = (
        existing.supplier_id,
        existing.credit_number,
        existing.reason,
        tuple(sorted((line.description, line.amount) for line in existing_lines)),
        tuple(sorted((a.purchase_invoice_id, a.amount) for a in existing_allocations)),
    )
    requested_signature = _generate_credit_note_signature(
        supplier_id, credit_number, reason, lines, allocations
    )
    if existing_signature != requested_signature:
        raise ConflictError(
            f"client_transaction_id {client_transaction_id!r} was already used for a "
            "different credit note request",
            error_code="IDEMPOTENCY_KEY_CONFLICT",
        )
    return existing


def create_supplier_credit_note(
    db: Session,
    *,
    store_id: int,
    supplier_id: int,
    credit_number: str,
    credit_date: date,
    reason: str,
    lines: list[SupplierCreditNoteLineInput],
    allocations: list[PaymentAllocationInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    purchase_return_id: int | None = None,
    notes: str | None = None,
    created_by: int | None = None,
) -> SupplierCreditNote:
    """Creates AND fully applies a supplier credit note in one atomic,
    single-step transaction — there is no DRAFT state (unlike an invoice,
    a credit note requires no matching decision) and no unapplied-credit
    balance (`allocations` must sum to exactly the credit's total —
    docs/M7_ADVANCED_AP_SETTLEMENT.md Section 14). Once created, a credit
    note's financial values are immutable; M7 implements no void for it
    (see the design doc's "Deferred" section).

    `reason` determines the accounting treatment
    (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 13):
    - GOODS_RETURN: requires `purchase_return_id` (the physical return
      this credit corresponds to — the quantity movement itself was
      already recorded by that PurchaseReturn; this only accounts for the
      AP-side financial consequence). Posts Dr AP / Cr Inventory.
    - COMMERCIAL_DISCOUNT: requires NO purchase_return_id (nothing
      physically moved). Posts Dr AP / Cr Purchase Discounts.

    Locking mirrors record_supplier_payment exactly: every allocated
    PurchaseInvoice is locked in ascending id order."""
    if reason not in SUPPLIER_CREDIT_NOTE_REASONS:
        raise ValidationAppError(
            f"Invalid credit note reason {reason!r}", error_code="INVALID_CREDIT_REASON"
        )
    if reason == "GOODS_RETURN" and purchase_return_id is None:
        raise ValidationAppError(
            "A GOODS_RETURN credit note must reference the purchase_return_id it corresponds to",
            error_code="MISSING_PURCHASE_RETURN_REFERENCE",
        )
    if reason == "COMMERCIAL_DISCOUNT" and purchase_return_id is not None:
        raise ValidationAppError(
            "A COMMERCIAL_DISCOUNT credit note must not reference a purchase return — nothing "
            "physically moved",
            error_code="UNEXPECTED_PURCHASE_RETURN_REFERENCE",
        )
    if not credit_number.strip():
        raise ValidationAppError("Credit number is required", error_code="INVALID_CREDIT_NUMBER")
    if not lines:
        raise ValidationAppError(
            "A credit note must have at least one line", error_code="EMPTY_CREDIT_NOTE"
        )
    for line in lines:
        if line.amount <= 0:
            raise ValidationAppError(
                "Each credit note line amount must be positive", error_code="INVALID_AMOUNT"
            )
    if not allocations:
        raise ValidationAppError(
            "A credit note must allocate to at least one invoice", error_code="EMPTY_ALLOCATIONS"
        )
    invoice_ids = [a.purchase_invoice_id for a in allocations]
    if len(set(invoice_ids)) != len(invoice_ids):
        raise ValidationAppError(
            "A credit note cannot allocate to the same invoice twice",
            error_code="DUPLICATE_ALLOCATION_TARGET",
        )
    for allocation in allocations:
        if allocation.amount <= 0:
            raise ValidationAppError(
                "Each allocation amount must be positive", error_code="INVALID_AMOUNT"
            )

    existing = _match_or_reject_idempotent_credit_note(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        credit_number=credit_number,
        reason=reason,
        lines=lines,
        allocations=allocations,
    )
    if existing is not None:
        return existing

    _enforce_store_access(caller_store_id, store_id, "supplier credit notes")

    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise ValidationAppError(
            f"Supplier {supplier_id} does not exist or is inactive", error_code="INVALID_SUPPLIER"
        )

    grand_total = sum((line.amount for line in lines), Decimal("0"))
    allocation_total = sum((a.amount for a in allocations), Decimal("0"))
    if allocation_total != grand_total:
        raise ValidationAppError(
            f"Allocations sum to {allocation_total} but the credit note total is {grand_total} "
            "— an unapplied remainder is not supported",
            error_code="ALLOCATION_MUST_EQUAL_CREDIT_AMOUNT",
        )

    if purchase_return_id is not None:
        purchase_return = db.get(PurchaseReturn, purchase_return_id)
        if purchase_return is None:
            raise NotFoundError(f"Purchase return {purchase_return_id} not found")
        if purchase_return.store_id != store_id:
            raise ConflictError(
                f"Purchase return {purchase_return_id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        po = db.get(PurchaseOrder, purchase_return.purchase_order_id)
        if po is None or po.supplier_id != supplier_id:
            raise ConflictError(
                f"Purchase return {purchase_return_id} does not belong to supplier {supplier_id}",
                error_code="SUPPLIER_MISMATCH",
            )
        return_value = sum(
            (item.quantity * item.unit_cost for item in purchase_return.items), Decimal("0")
        )
        if grand_total > return_value:
            raise ConflictError(
                f"Credit note total {grand_total} exceeds purchase return {purchase_return_id}'s "
                f"own value of {return_value}",
                error_code="CREDIT_EXCEEDS_RETURN_VALUE",
            )

    sorted_invoice_ids = sorted(set(invoice_ids))
    invoices = (
        db.execute(
            select(PurchaseInvoice)
            .where(PurchaseInvoice.id.in_(sorted_invoice_ids))
            .order_by(PurchaseInvoice.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    invoices_by_id = {inv.id: inv for inv in invoices}

    existing = _match_or_reject_idempotent_credit_note(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        credit_number=credit_number,
        reason=reason,
        lines=lines,
        allocations=allocations,
    )
    if existing is not None:
        return existing

    missing_ids = set(sorted_invoice_ids) - set(invoices_by_id)
    if missing_ids:
        raise NotFoundError(f"Purchase invoice(s) {sorted(missing_ids)} not found")

    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        if invoice.store_id != store_id:
            raise ConflictError(
                f"Purchase invoice {invoice.id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        if invoice.supplier_id != supplier_id:
            raise ConflictError(
                f"Purchase invoice {invoice.id} does not belong to supplier {supplier_id}",
                error_code="SUPPLIER_MISMATCH",
            )
        if invoice.status not in _OUTSTANDING_INVOICE_STATUSES:
            raise ConflictError(
                f"Purchase invoice {invoice.id} is {invoice.status} and cannot accept a "
                "credit (must be POSTED or PARTIALLY_PAID)",
                error_code="INVALID_INVOICE_STATE",
            )
        remaining = _outstanding_balance(invoice)
        if allocation.amount > remaining:
            raise ConflictError(
                f"Allocation of {allocation.amount} to purchase invoice {invoice.id} exceeds "
                f"its outstanding balance of {remaining}",
                error_code="OVER_ALLOCATION",
            )

    credit_note = SupplierCreditNote(
        store_id=store_id,
        supplier_id=supplier_id,
        credit_number=credit_number,
        credit_date=credit_date,
        reason=reason,
        purchase_return_id=purchase_return_id,
        grand_total=grand_total,
        amount_allocated=Decimal("0"),
        client_transaction_id=client_transaction_id,
        notes=notes,
        created_by=created_by,
    )
    db.add(credit_note)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_supplier_credit_notes_supplier_credit_number":
            raise ConflictError(
                f"Supplier {supplier_id} already has a credit note numbered {credit_number!r}",
                error_code="DUPLICATE_SUPPLIER_CREDIT_NUMBER",
            ) from exc
        winner = db.execute(
            select(SupplierCreditNote).where(
                SupplierCreditNote.client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    for line in lines:
        db.add(
            SupplierCreditNoteLine(
                supplier_credit_note_id=credit_note.id,
                product_id=line.product_id,
                description=line.description,
                quantity=line.quantity,
                unit_cost=line.unit_cost,
                amount=line.amount,
            )
        )

    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        db.add(
            SupplierCreditAllocation(
                supplier_credit_note_id=credit_note.id,
                purchase_invoice_id=invoice.id,
                amount=allocation.amount,
            )
        )
        invoice.amount_credited = invoice.amount_credited + allocation.amount
        _recompute_invoice_status(invoice)

    credit_note.amount_allocated = grand_total

    audit_service.log_event(
        db,
        user_id=created_by,
        action="SUPPLIER_CREDIT_NOTE_CREATED",
        entity_type="supplier_credit_note",
        entity_id=credit_note.id,
        after={
            "supplier_id": supplier_id,
            "reason": reason,
            "grand_total": str(grand_total),
            "purchase_return_id": purchase_return_id,
            "allocations": [
                {"purchase_invoice_id": a.purchase_invoice_id, "amount": str(a.amount)}
                for a in allocations
            ],
        },
    )

    accounting_service.post_supplier_credit_note_journal(
        db, credit_note=credit_note, created_by=created_by
    )

    db.flush()
    return credit_note


def reverse_supplier_credit_note(
    db: Session,
    *,
    supplier_credit_note_id: int,
    reason: str,
    caller_store_id: int | None,
    reversed_by: int | None,
) -> SupplierCreditNote:
    """Corrects a mis-recorded supplier credit note — mirrors
    reverse_supplier_payment exactly (see that function's docstring for
    the full structure/locking/idempotency rationale). The one genuine
    difference from a payment reversal (docs/M16_DESIGN.md "AP payment/
    credit-note correction path"): a GOODS_RETURN credit note's original
    posting credits Inventory, not a cash/bank account, because the
    physical goods movement was already recorded by the referenced
    PurchaseReturn — reversing the AP-side journal here does NOT
    resurrect that inventory (no second movement is created or undone;
    PurchaseReturn itself has no reversal in this codebase, a
    pre-existing M3/M7 scope boundary this milestone does not remove).
    This reversal is the AP financial correction only, exactly mirroring
    what the original posting itself already scoped to."""
    if not reason or not reason.strip():
        raise ValidationAppError(
            "A reversal reason is required", error_code="REVERSAL_REASON_REQUIRED"
        )
    credit_note = db.execute(
        select(SupplierCreditNote)
        .where(SupplierCreditNote.id == supplier_credit_note_id)
        .with_for_update()
    ).scalar_one_or_none()
    if credit_note is None:
        raise NotFoundError(f"Supplier credit note {supplier_credit_note_id} not found")
    _enforce_store_access(caller_store_id, credit_note.store_id, "this supplier credit note")

    existing_reversal = db.execute(
        select(SupplierCreditNoteReversal).where(
            SupplierCreditNoteReversal.supplier_credit_note_id == credit_note.id
        )
    ).scalar_one_or_none()
    if existing_reversal is not None:
        return credit_note
    if reversed_by is None:
        raise ValidationAppError(
            "Reversal requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    allocations = (
        db.execute(
            select(SupplierCreditAllocation)
            .where(SupplierCreditAllocation.supplier_credit_note_id == credit_note.id)
            .order_by(SupplierCreditAllocation.purchase_invoice_id)
        )
        .scalars()
        .all()
    )
    invoice_ids = sorted({a.purchase_invoice_id for a in allocations})
    invoices = (
        db.execute(
            select(PurchaseInvoice)
            .where(PurchaseInvoice.id.in_(invoice_ids))
            .order_by(PurchaseInvoice.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    invoices_by_id = {inv.id: inv for inv in invoices}
    total_reversed = Decimal("0")
    for allocation in allocations:
        invoice = invoices_by_id[allocation.purchase_invoice_id]
        invoice.amount_credited = invoice.amount_credited - allocation.amount
        _recompute_invoice_status(invoice)
        total_reversed += allocation.amount
    credit_note.amount_allocated = credit_note.amount_allocated - total_reversed

    reversal_entry = accounting_service.post_supplier_credit_note_reversal_journal(
        db, credit_note=credit_note, created_by=reversed_by
    )

    db.add(
        SupplierCreditNoteReversal(
            supplier_credit_note_id=credit_note.id,
            reversal_journal_entry_id=reversal_entry.id,
            reason=reason,
            reversed_by=reversed_by,
            reversed_at=datetime.now(UTC),
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Supplier credit note {supplier_credit_note_id} has already been reversed",
            error_code="ALREADY_REVERSED",
        ) from exc

    audit_service.log_event(
        db,
        user_id=reversed_by,
        action="SUPPLIER_CREDIT_NOTE_REVERSED",
        entity_type="supplier_credit_note",
        entity_id=credit_note.id,
        after={
            "reversal_journal_entry_id": reversal_entry.id,
            "reason": reason,
            "grand_total": str(credit_note.grand_total),
        },
    )
    return credit_note


def get_supplier_credit_note(db: Session, supplier_credit_note_id: int) -> SupplierCreditNote:
    credit_note = db.get(SupplierCreditNote, supplier_credit_note_id)
    if credit_note is None:
        raise NotFoundError(f"Supplier credit note {supplier_credit_note_id} not found")
    return credit_note


def list_supplier_credit_notes(
    db: Session,
    *,
    store_id: int | None = None,
    supplier_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SupplierCreditNote]:
    query = (
        select(SupplierCreditNote)
        .order_by(SupplierCreditNote.credit_date.desc(), SupplierCreditNote.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(SupplierCreditNote.store_id == store_id)
    if supplier_id is not None:
        query = query.where(SupplierCreditNote.supplier_id == supplier_id)
    return list(db.execute(query).scalars().all())


# --- AP subledger / reporting ------------------------------------------------


@dataclass(frozen=True)
class SupplierApSummary:
    supplier_id: int
    total_owed: Decimal  # Σ outstanding balance across POSTED/PARTIALLY_PAID invoices
    total_overdue: Decimal  # the portion of total_owed whose due_date has passed
    total_current: Decimal  # total_owed - total_overdue
    total_paid: Decimal  # Σ amount_paid across every invoice ever (lifetime)
    total_credited: Decimal  # Σ amount_credited across every invoice ever (lifetime)
    outstanding_purchase_clearing: Decimal  # received, not yet invoiced, across this supplier's POs


def get_supplier_ap_summary(
    db: Session, supplier_id: int, *, as_of: date | None = None
) -> SupplierApSummary:
    """Answers the M6/M7 task checklist for one supplier. Every figure
    here is derived directly from PurchaseInvoice/SupplierPayment/
    SupplierCreditNote rows (the AP subledger) — never from a separately-
    maintained running total, so it always reconciles to what the rows
    actually say."""
    as_of = as_of or date.today()
    invoices = list(
        db.execute(select(PurchaseInvoice).where(PurchaseInvoice.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    total_owed = Decimal("0")
    total_overdue = Decimal("0")
    total_paid = Decimal("0")
    total_credited = Decimal("0")
    for invoice in invoices:
        total_paid += invoice.amount_paid
        total_credited += invoice.amount_credited
        if invoice.status in _OUTSTANDING_INVOICE_STATUSES:
            balance = _outstanding_balance(invoice)
            total_owed += balance
            if invoice.due_date < as_of:
                total_overdue += balance
    total_current = total_owed - total_overdue

    purchase_orders = list(
        db.execute(select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    outstanding_clearing = Decimal("0")
    for po in purchase_orders:
        for item in po.items:
            remaining = item.quantity_received - item.quantity_invoiced
            if remaining > 0:
                outstanding_clearing += _fifo_clearing_amount(
                    db,
                    purchase_order_item_id=item.id,
                    already_invoiced_qty=item.quantity_invoiced,
                    additional_qty=remaining,
                )

    return SupplierApSummary(
        supplier_id=supplier_id,
        total_owed=total_owed,
        total_overdue=total_overdue,
        total_current=total_current,
        total_paid=total_paid,
        total_credited=total_credited,
        outstanding_purchase_clearing=outstanding_clearing,
    )


@dataclass(frozen=True)
class SupplierTransaction:
    transaction_type: str  # "INVOICE", "PAYMENT", or "CREDIT_NOTE"
    id: int
    date_: date
    reference: str
    amount: Decimal
    status: str


def get_supplier_transaction_history(db: Session, supplier_id: int) -> list[SupplierTransaction]:
    invoices = (
        db.execute(select(PurchaseInvoice).where(PurchaseInvoice.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    payments = (
        db.execute(select(SupplierPayment).where(SupplierPayment.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    credit_notes = (
        db.execute(select(SupplierCreditNote).where(SupplierCreditNote.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    transactions = (
        [
            SupplierTransaction(
                transaction_type="INVOICE",
                id=inv.id,
                date_=inv.invoice_date,
                reference=inv.invoice_number,
                amount=inv.grand_total,
                status=inv.status,
            )
            for inv in invoices
        ]
        + [
            SupplierTransaction(
                transaction_type="PAYMENT",
                id=pay.id,
                date_=pay.payment_date,
                reference=pay.reference or f"Payment {pay.id}",
                amount=pay.amount,
                status="RECORDED",
            )
            for pay in payments
        ]
        + [
            SupplierTransaction(
                transaction_type="CREDIT_NOTE",
                id=cn.id,
                date_=cn.credit_date,
                reference=cn.credit_number,
                amount=cn.grand_total,
                status="APPLIED",
            )
            for cn in credit_notes
        ]
    )
    transactions.sort(key=lambda t: (t.date_, t.id))
    return transactions


@dataclass(frozen=True)
class SupplierStatementLine:
    date_: date
    # INVOICE | PAYMENT | CREDIT_NOTE | PAYMENT_REVERSAL | CREDIT_NOTE_REVERSAL (M16)
    transaction_type: str
    reference: str
    amount: Decimal  # signed effect on the balance owed to the supplier
    running_balance: Decimal


@dataclass(frozen=True)
class SupplierStatement:
    supplier_id: int
    date_from: date | None
    date_to: date | None
    opening_balance: Decimal
    lines: list[SupplierStatementLine]
    closing_balance: Decimal


def get_supplier_statement(
    db: Session,
    supplier_id: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SupplierStatement:
    """A chronological reconstruction of the supplier balance from
    invoices/payments/credit-notes — never from a cached total
    (docs/M7_ADVANCED_AP_SETTLEMENT.md Section 16). With no date filters,
    `closing_balance` equals `get_supplier_ap_summary(...).total_owed`
    exactly (proven in tests): every invoice's amount_paid/amount_credited
    is itself the sum of allocations from these SAME payments/credit
    notes, so summing (Σ non-draft/voided invoice grand_totals) - (Σ
    payments) - (Σ credit notes) telescopes to (Σ outstanding invoice
    balances)."""
    invoices = (
        db.execute(
            select(PurchaseInvoice).where(
                PurchaseInvoice.supplier_id == supplier_id,
                PurchaseInvoice.status.notin_(_NON_ACCOUNTING_INVOICE_STATUSES),
            )
        )
        .scalars()
        .all()
    )
    payments = (
        db.execute(select(SupplierPayment).where(SupplierPayment.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    credit_notes = (
        db.execute(select(SupplierCreditNote).where(SupplierCreditNote.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    # M16 (docs/M16_DESIGN.md "AP payment/credit-note correction path"):
    # a reversed payment/credit note's ORIGINAL event above is
    # deliberately left unchanged (it really happened, historically) --
    # its reversal is a new, separate, dated event that reverses the
    # ORIGINAL's balance effect, exactly mirroring how the reversal
    # journal itself is a new entry, never a mutation of the original.
    # Without this, the statement's closing_balance would silently
    # diverge from get_supplier_ap_summary/ap_aging (both of which read
    # the live, already-corrected PurchaseInvoice.amount_paid/
    # amount_credited) the moment any reversal exists.
    payment_reversals = db.execute(
        select(SupplierPaymentReversal, SupplierPayment)
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentReversal.supplier_payment_id)
        .where(SupplierPayment.supplier_id == supplier_id)
    ).all()
    credit_note_reversals = db.execute(
        select(SupplierCreditNoteReversal, SupplierCreditNote)
        .join(
            SupplierCreditNote,
            SupplierCreditNote.id == SupplierCreditNoteReversal.supplier_credit_note_id,
        )
        .where(SupplierCreditNote.supplier_id == supplier_id)
    ).all()

    # The tiebreaker for same-day events CANNOT be each row's own primary
    # key: PurchaseInvoice/SupplierPayment/SupplierCreditNote are three
    # independent auto-increment sequences, so comparing their raw `id`
    # values across tables is meaningless (a live smoke test caught this
    # producing a nonsensical statement order — credit note before the
    # payment before the invoice it was created against, purely because
    # that table's sequence happened to start lower). `created_at` (real
    # insertion order, from TimestampMixin on every one of these models)
    # is the only value that actually orders same-day events correctly.
    events: list[tuple[date, str, str, Decimal, datetime]] = (
        [
            (inv.invoice_date, "INVOICE", inv.invoice_number, inv.grand_total, inv.created_at)
            for inv in invoices
        ]
        + [
            (pay.payment_date, "PAYMENT", f"Payment {pay.id}", -pay.amount, pay.created_at)
            for pay in payments
        ]
        + [
            (cn.credit_date, "CREDIT_NOTE", cn.credit_number, -cn.grand_total, cn.created_at)
            for cn in credit_notes
        ]
        + [
            (
                reversal.reversed_at.date(),
                "PAYMENT_REVERSAL",
                f"Reversal of payment {payment.id}",
                payment.amount,
                reversal.reversed_at,
            )
            for reversal, payment in payment_reversals
        ]
        + [
            (
                reversal.reversed_at.date(),
                "CREDIT_NOTE_REVERSAL",
                f"Reversal of credit note {credit_note.credit_number}",
                credit_note.grand_total,
                reversal.reversed_at,
            )
            for reversal, credit_note in credit_note_reversals
        ]
    )
    events.sort(key=lambda e: (e[0], e[4]))

    opening_balance = Decimal("0")
    in_range: list[tuple[date, str, str, Decimal, datetime]] = []
    for event_date, ttype, reference, amount, created_at in events:
        if date_from is not None and event_date < date_from:
            opening_balance += amount
            continue
        if date_to is not None and event_date > date_to:
            continue
        in_range.append((event_date, ttype, reference, amount, created_at))

    running = opening_balance
    lines: list[SupplierStatementLine] = []
    for event_date, ttype, reference, amount, _created_at in in_range:
        running += amount
        lines.append(
            SupplierStatementLine(
                date_=event_date,
                transaction_type=ttype,
                reference=reference,
                amount=amount,
                running_balance=running,
            )
        )

    return SupplierStatement(
        supplier_id=supplier_id,
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening_balance,
        lines=lines,
        closing_balance=running,
    )


@dataclass(frozen=True)
class ApAgingRow:
    supplier_id: int
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_over_90: Decimal
    total: Decimal


def ap_aging(
    db: Session,
    *,
    store_id: int | None = None,
    store_ids: Sequence[int] | None = None,
    as_of: date | None = None,
) -> list[ApAgingRow]:
    """Standard aging buckets (current / 1-30 / 31-60 / 61-90 / 90+ days
    past due_date) for every outstanding (POSTED/PARTIALLY_PAID) invoice,
    grouped by supplier. The bucketed balance is `_outstanding_balance`
    (grand_total - amount_paid - amount_credited) — a credit note reduces
    an invoice's aged balance exactly like a payment does.

    `store_ids` (M11 addition, docs/M11_DESIGN.md Section 2): an
    explicit store subset, additive to the existing `store_id`
    single-store/all-stores filter — every existing caller is
    unaffected."""
    as_of = as_of or date.today()
    query = select(PurchaseInvoice).where(PurchaseInvoice.status.in_(_OUTSTANDING_INVOICE_STATUSES))
    if store_ids is not None:
        query = query.where(PurchaseInvoice.store_id.in_(store_ids))
    elif store_id is not None:
        query = query.where(PurchaseInvoice.store_id == store_id)
    invoices = list(db.execute(query).scalars().all())

    buckets: dict[int, dict[str, Decimal]] = {}
    for invoice in invoices:
        balance = _outstanding_balance(invoice)
        if balance <= 0:
            continue
        row = buckets.setdefault(
            invoice.supplier_id,
            {
                "current": Decimal("0"),
                "1_30": Decimal("0"),
                "31_60": Decimal("0"),
                "61_90": Decimal("0"),
                "90+": Decimal("0"),
            },
        )
        days_overdue = (as_of - invoice.due_date).days
        if days_overdue <= 0:
            row["current"] += balance
        elif days_overdue <= 30:
            row["1_30"] += balance
        elif days_overdue <= 60:
            row["31_60"] += balance
        elif days_overdue <= 90:
            row["61_90"] += balance
        else:
            row["90+"] += balance

    return [
        ApAgingRow(
            supplier_id=supplier_id,
            current=b["current"],
            days_1_30=b["1_30"],
            days_31_60=b["31_60"],
            days_61_90=b["61_90"],
            days_over_90=b["90+"],
            total=b["current"] + b["1_30"] + b["31_60"] + b["61_90"] + b["90+"],
        )
        for supplier_id, b in buckets.items()
    ]


@dataclass(frozen=True)
class ApReconciliationRow:
    store_id: int | None
    gl_accounts_payable_balance: Decimal
    ap_subledger_total: Decimal
    discrepancy: Decimal


def ap_reconciliation(
    db: Session, *, store_id: int | None = None, store_ids: Sequence[int] | None = None
) -> ApReconciliationRow:
    """Compares the Accounts Payable GL control-account balance against
    the AP subledger total (Σ outstanding invoice balances, now net of
    both payments AND credit notes) — unchanged mechanism from M6, same
    shape as accounting.service.inventory_reconciliation.

    `store_ids` (M11 addition): see `ap_aging`'s own docstring."""
    rows = accounting_service.trial_balance(db, store_id=store_id, store_ids=store_ids)
    ap_row = next((r for r in rows if r.account_code == ACCOUNT_ACCOUNTS_PAYABLE), None)
    gl_balance = (ap_row.total_credit - ap_row.total_debit) if ap_row else Decimal("0")

    query = select(PurchaseInvoice).where(PurchaseInvoice.status.in_(_OUTSTANDING_INVOICE_STATUSES))
    if store_ids is not None:
        query = query.where(PurchaseInvoice.store_id.in_(store_ids))
    elif store_id is not None:
        query = query.where(PurchaseInvoice.store_id == store_id)
    subledger_total = sum(
        (_outstanding_balance(invoice) for invoice in db.execute(query).scalars()),
        Decimal("0"),
    )
    return ApReconciliationRow(
        store_id=store_id,
        gl_accounts_payable_balance=gl_balance,
        ap_subledger_total=subledger_total,
        discrepancy=gl_balance - subledger_total,
    )


@dataclass(frozen=True)
class PurchaseClearingReconciliationRow:
    store_id: int | None
    gl_purchase_clearing_balance: Decimal
    outstanding_clearing_total: Decimal
    discrepancy: Decimal


def purchase_clearing_reconciliation(
    db: Session, *, store_id: int | None = None, store_ids: Sequence[int] | None = None
) -> PurchaseClearingReconciliationRow:
    """Compares the Purchase Clearing GL balance against the sum, across
    every PurchaseOrderItem (optionally scoped to one store), of the
    FIFO-priced value still received-but-not-invoiced — unchanged from M6
    (credit notes never touch Purchase Clearing; see
    docs/M7_ADVANCED_AP_SETTLEMENT.md Section 13).

    `store_ids` (M11 addition): see `ap_aging`'s own docstring."""
    rows = accounting_service.trial_balance(db, store_id=store_id, store_ids=store_ids)
    clearing_row = next((r for r in rows if r.account_code == ACCOUNT_PURCHASE_CLEARING), None)
    gl_balance = (
        (clearing_row.total_credit - clearing_row.total_debit) if clearing_row else Decimal("0")
    )

    query = select(PurchaseOrder)
    if store_ids is not None:
        query = query.where(PurchaseOrder.store_id.in_(store_ids))
    elif store_id is not None:
        query = query.where(PurchaseOrder.store_id == store_id)
    purchase_orders = list(db.execute(query).scalars().all())
    outstanding_total = Decimal("0")
    for po in purchase_orders:
        for item in po.items:
            remaining = item.quantity_received - item.quantity_invoiced
            if remaining > 0:
                outstanding_total += _fifo_clearing_amount(
                    db,
                    purchase_order_item_id=item.id,
                    already_invoiced_qty=item.quantity_invoiced,
                    additional_qty=remaining,
                )
    return PurchaseClearingReconciliationRow(
        store_id=store_id,
        gl_purchase_clearing_balance=gl_balance,
        outstanding_clearing_total=outstanding_total,
        discrepancy=gl_balance - outstanding_total,
    )
