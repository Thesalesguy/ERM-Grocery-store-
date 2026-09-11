"""Accounts Payable business logic: purchase-invoice lifecycle, three-way
matching against purchase orders/goods receipts, Purchase Clearing
clearing, and supplier payments.

See docs/M6_AP_VENDOR_ACCOUNTING.md for the full design. The single most
important technique in this file is FIFO receipt-lot matching
(`_fifo_clearing_amount`): a PurchaseOrderItem can be received across
several GoodsReceipts at different costs (the exact WAC-driving scenario
M3 already supports), so "the cost of the units being invoiced" is not a
single number — it is computed by walking that item's GoodsReceiptItem
rows in receipt order, skipping whatever an earlier invoice already
consumed (tracked via the same running `quantity_invoiced` cache
`quantity_received` already uses), and pricing the newly-invoiced
quantity at the ACTUAL recorded cost of whichever lot(s) it falls into.
This is what lets a Purchase Clearing balance always be traced back to
real receipt data (M6 task Section 5: "No orphan clearing balances") and
what makes the price-variance calculation (invoiced value minus this
FIFO-priced value) a real, derived fact rather than an invented number.

Two commit conventions coexist here, matching every other service module:
`create_purchase_invoice` (a DRAFT has no accounting/quantity effect) and
`void_purchase_invoice` on a DRAFT commit directly. `post_purchase_invoice`,
`void_purchase_invoice` on a POSTED invoice, and `record_supplier_payment`
— each a genuinely atomic multi-effect transaction — never call
commit()/rollback() themselves; the caller commits once.
"""

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
    SUPPLIER_PAYMENT_METHODS,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    SupplierPayment,
)
from app.modules.audit import service as audit_service
from app.modules.auth.models import Store
from app.modules.products.models import Product
from app.modules.purchasing.models import (
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
)

_MONEY_QUANTUM = Decimal("0.01")
# Matches accounting/service.py's own _LEDGER_QUANTUM exactly — clearing
# and price-variance amounts must be quantized identically to how
# post_goods_receipt_journal originally quantized the Purchase Clearing
# credit they are now clearing, or a fresh, fabricated rounding mismatch
# would appear in inventory/AP reconciliation for no real reason.
_LEDGER_QUANTUM = Decimal("0.000001")


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
    purchase_order_id: int,
    invoice_number: str,
    lines: list[PurchaseInvoiceLineInput],
) -> tuple:
    return (
        supplier_id,
        purchase_order_id,
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
    purchase_order_id: int,
    invoice_number: str,
    lines: list[PurchaseInvoiceLineInput],
) -> PurchaseInvoice | None:
    """Mirrors app.modules.sales.service._match_or_reject_idempotent_return
    exactly, including WHY it matters under concurrency: see this module's
    create_purchase_invoice docstring."""
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
        existing.purchase_order_id,
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
    requested_signature = _generate_invoice_client_signature(
        supplier_id, purchase_order_id, invoice_number, lines
    )
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
    purchase_order_id: int,
    invoice_number: str,
    invoice_date: date,
    lines: list[PurchaseInvoiceLineInput],
    client_transaction_id: str,
    caller_store_id: int | None,
    due_date: date | None = None,
    notes: str | None = None,
    created_by: int | None = None,
) -> PurchaseInvoice:
    """Records a supplier invoice as a DRAFT — no accounting effect, no
    quantity_invoiced change, freely re-creatable-if-wrong (delete via
    void_purchase_invoice) up until post_purchase_invoice commits it
    financially (docs/M6_AP_VENDOR_ACCOUNTING.md "Invoice lifecycle").

    Two INDEPENDENT duplicate-detection mechanisms, for two different
    real failure modes (both required by M6 task Section 27's "duplicate
    invoice" mutation target):
    1. `client_transaction_id` idempotency (checked here, before and
       after no lock is needed — DRAFT creation touches no shared running
       total, so unlike post_purchase_invoice/record_supplier_payment
       there is no second "after the lock" check to add: two concurrent
       creates race only on the flush's IntegrityError, recovered below
       exactly like every other module's create-with-idempotency-key
       function).
    2. `(supplier_id, invoice_number)` uniqueness — catches a genuinely
       different attempt (a fresh client_transaction_id) to record the
       SAME real-world supplier document twice.
    """
    existing = _match_or_reject_idempotent_invoice(
        db,
        client_transaction_id=client_transaction_id,
        supplier_id=supplier_id,
        purchase_order_id=purchase_order_id,
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
    purchase_order = db.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    if purchase_order.store_id != store_id:
        raise ConflictError(
            f"Purchase order {purchase_order_id} does not belong to store {store_id}",
            error_code="STORE_MISMATCH",
        )
    if purchase_order.supplier_id != supplier_id:
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

    resolved_due_date = due_date
    if resolved_due_date is None:
        term_days = supplier.default_payment_terms_days or 0
        resolved_due_date = invoice_date + timedelta(days=term_days)

    invoice = PurchaseInvoice(
        store_id=store_id,
        supplier_id=supplier_id,
        purchase_order_id=purchase_order_id,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=resolved_due_date,
        status="DRAFT",
        subtotal=Decimal("0"),
        discount_total=Decimal("0"),
        tax_total=Decimal("0"),
        grand_total=Decimal("0"),
        amount_paid=Decimal("0"),
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

    # Batched into two IN(...) queries rather than one db.get() per line
    # per table — a plain read with no locking/ordering requirement (the
    # DRAFT this creates has no quantity_invoiced/accounting effect yet),
    # so there is no reason to pay two round-trips per line for a request
    # that can carry up to 500 (mirrors the same fix applied to M5's
    # create_sale_return for the identical reason).
    po_item_ids = {line.purchase_order_item_id for line in lines}
    po_items_by_id = {
        item.id: item
        for item in db.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.id.in_(po_item_ids))
        ).scalars()
    }
    product_ids = {item.product_id for item in po_items_by_id.values()}
    products_by_id = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
    }

    subtotal = Decimal("0")
    discount_total = Decimal("0")
    tax_total = Decimal("0")
    for line in lines:
        po_item = po_items_by_id.get(line.purchase_order_item_id)
        if po_item is None or po_item.purchase_order_id != purchase_order_id:
            raise NotFoundError(
                f"Purchase order item {line.purchase_order_item_id} not found on "
                f"purchase order {purchase_order_id}"
            )
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
            "purchase_order_id": purchase_order_id,
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
    product_id: int
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_invoiced: Decimal
    quantity_invoiceable: Decimal  # quantity_received - quantity_invoiced


def get_invoice_matching_status(
    db: Session, purchase_order_id: int
) -> list[PurchaseOrderItemMatchStatus]:
    """Read-only three-way-match preview for a PO — mirrors
    app.modules.sales.service.get_return_eligibility's read-only-helper
    shape. `quantity_invoiceable` is the exact ceiling
    post_purchase_invoice enforces: never received, ordered."""
    purchase_order = db.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {purchase_order_id} not found")
    return [
        PurchaseOrderItemMatchStatus(
            purchase_order_item_id=item.id,
            product_id=item.product_id,
            quantity_ordered=item.quantity_ordered,
            quantity_received=item.quantity_received,
            quantity_invoiced=item.quantity_invoiced,
            quantity_invoiceable=item.quantity_received - item.quantity_invoiced,
        )
        for item in purchase_order.items
    ]


def _fifo_clearing_amount(
    db: Session,
    *,
    purchase_order_item_id: int,
    already_invoiced_qty: Decimal,
    additional_qty: Decimal,
) -> Decimal:
    """The dollar value of `additional_qty` more units of this PO item
    being invoiced, priced at the ACTUAL recorded cost of whichever
    GoodsReceiptItem lot(s) they fall into — walking receipt lots in
    receipt order (oldest first), skipping `already_invoiced_qty` units
    already consumed by earlier invoices against this same item. This is
    what lets Purchase Clearing's GL balance always be traced back to
    specific receipt data (module docstring above) rather than to a
    single "the" cost per item that may not exist when an item was
    received across multiple receipts at different costs.

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
    total = Decimal("0")
    for receipt_item in receipt_items:
        lot_qty = receipt_item.quantity_received
        if skip >= lot_qty:
            skip -= lot_qty
            continue
        available = lot_qty - skip
        skip = Decimal("0")
        take = min(available, remaining)
        total += take * receipt_item.unit_cost
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
    quantity_received — M6 task Section 4's explicit "PO=100,
    Received=80, Invoice=100 must not silently become valid" example),
    computes the FIFO-matched clearing amount and price variance per
    line, updates quantity_invoiced, and posts the Purchase Clearing/AP/
    variance/tax/discount journal — all in this one transaction.

    Idempotent by state, not by a separate key (mirrors
    submit_purchase_order's status-transition precedent, not
    create_sale_return's client_transaction_id precedent): calling this
    on an already-POSTED/PARTIALLY_PAID/PAID invoice is a no-op that
    returns the current state, since posting a DRAFT is itself the only
    state transition this function performs and it cannot happen twice.

    Locking: locks the PurchaseOrder row FIRST (the exact same row
    receive_goods locks), which serializes THIS invoice's posting against
    both a concurrent goods receipt AND a concurrent posting of ANOTHER
    invoice against the same PO — both mutate the same
    PurchaseOrderItem.quantity_received/quantity_invoiced running totals.
    The invoice's own status is re-checked immediately after acquiring
    that lock (the M5-discovered pattern: a racing caller that already
    posted this exact invoice may have committed while this caller
    waited for the lock)."""
    invoice = get_purchase_invoice(db, purchase_invoice_id)
    _enforce_store_access(caller_store_id, invoice.store_id, "this purchase invoice")
    if invoice.status != "DRAFT":
        # Already posted (or further along) — idempotent no-op. VOIDED is
        # the only DRAFT-adjacent state that reaches here needing a real
        # rejection, since a voided invoice can never become POSTED.
        if invoice.status == "VOIDED":
            raise ConflictError(
                f"Purchase invoice {purchase_invoice_id} is VOIDED and cannot be posted",
                error_code="INVALID_INVOICE_STATE",
            )
        return invoice

    purchase_order = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == invoice.purchase_order_id).with_for_update()
    ).scalar_one_or_none()
    if purchase_order is None:
        raise NotFoundError(f"Purchase order {invoice.purchase_order_id} not found")

    db.refresh(invoice)
    if invoice.status != "DRAFT":
        if invoice.status == "VOIDED":
            raise ConflictError(
                f"Purchase invoice {purchase_invoice_id} is VOIDED and cannot be posted",
                error_code="INVALID_INVOICE_STATE",
            )
        return invoice

    lines = list(invoice.lines)
    requested_by_item: dict[int, Decimal] = {}
    for line in lines:
        requested_by_item[line.purchase_order_item_id] = (
            requested_by_item.get(line.purchase_order_item_id, Decimal("0"))
            + line.quantity_invoiced
        )

    total_clearing = Decimal("0")
    total_variance = Decimal("0")
    invoiced_value_by_item: dict[int, Decimal] = {}
    for line in lines:
        invoiced_value_by_item[line.purchase_order_item_id] = invoiced_value_by_item.get(
            line.purchase_order_item_id, Decimal("0")
        ) + _round_money(line.quantity_invoiced * line.unit_price)

    for po_item_id, requested_qty in requested_by_item.items():
        po_item = db.get(PurchaseOrderItem, po_item_id)
        if po_item is None:
            # Unreachable in practice: create_purchase_invoice already
            # validated every line's purchase_order_item_id against this
            # same purchase_order_id, and PurchaseOrderItem rows are never
            # deleted. Guarded anyway rather than trusting that forever.
            raise NotFoundError(f"Purchase order item {po_item_id} not found")
        already_invoiced = po_item.quantity_invoiced
        new_total = already_invoiced + requested_qty
        if new_total > po_item.quantity_received:
            raise ConflictError(
                f"Cannot invoice {requested_qty} of purchase order item {po_item_id}: only "
                f"{po_item.quantity_received - already_invoiced} remains invoiceable (of "
                f"{po_item.quantity_received} received)",
                error_code="OVER_INVOICING",
            )
        clearing_for_item = _fifo_clearing_amount(
            db,
            purchase_order_item_id=po_item_id,
            already_invoiced_qty=already_invoiced,
            additional_qty=requested_qty,
        )
        total_clearing += clearing_for_item
        total_variance += _ledger_quantize(invoiced_value_by_item[po_item_id]) - clearing_for_item
        po_item.quantity_invoiced = new_total

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


def _extract_clearing_and_variance_from_journal(
    db: Session, journal_entry_id: int
) -> tuple[Decimal, Decimal]:
    """Reads the EXACT clearing_amount/price_variance_amount posted by a
    prior post_purchase_invoice_journal call, straight off its own
    journal lines — used by void_purchase_invoice so a void's reversal is
    provably the mirror of what was actually posted, never a fresh
    recomputation that could disagree with it (e.g. because FIFO receipt-
    lot consumption order is not safely re-derivable once OTHER invoices
    against the same PO items have posted in between — see
    void_purchase_invoice's docstring)."""
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
    """Voids a DRAFT (a pure status flip — nothing financial ever
    happened) or a POSTED-but-unpaid invoice (reverses quantity_invoiced
    and posts an exact compensating PURCHASE_INVOICE_VOID journal entry,
    mirroring app.modules.sales.service.void_sale's "void is a real
    operational undo, not a generic accounting-only reversal" shape).

    Deliberately scoped to invoices with `amount_paid == 0`
    (PARTIALLY_PAID/PAID are rejected with INVOICE_HAS_PAYMENTS) — M6
    does not implement unwinding a supplier payment as part of a void.
    An invoice a payment has already been recorded against must be
    corrected through a future credit-note/refund workflow (deferred —
    see docs/M6_AP_VENDOR_ACCOUNTING.md "Known limitations"), not through
    this function.

    Locking mirrors post_purchase_invoice: the PurchaseOrder row is
    locked before touching any of its items' quantity_invoiced, and the
    invoice's own status is re-checked immediately after acquiring the
    invoice row's own FOR UPDATE lock (taken first, below) — a racing
    concurrent void of the SAME invoice is idempotent (second caller sees
    VOIDED and no-ops)."""
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
            f"Purchase invoice {purchase_invoice_id} has payments recorded against it and "
            "cannot be voided in M6 (deferred: a future credit-note/refund workflow would "
            "handle this)",
            error_code="INVOICE_HAS_PAYMENTS",
        )

    was_posted = invoice.status == "POSTED"
    if was_posted:
        purchase_order = db.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == invoice.purchase_order_id)
            .with_for_update()
        ).scalar_one_or_none()
        if purchase_order is None:
            raise NotFoundError(f"Purchase order {invoice.purchase_order_id} not found")

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

        for line in invoice.lines:
            po_item = db.get(PurchaseOrderItem, line.purchase_order_item_id)
            if po_item is None:
                raise NotFoundError(f"Purchase order item {line.purchase_order_item_id} not found")
            po_item.quantity_invoiced = po_item.quantity_invoiced - line.quantity_invoiced

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


# --- Supplier payments -------------------------------------------------------


def _generate_payment_signature(
    purchase_invoice_id: int, amount: Decimal, payment_method: str
) -> tuple:
    return (purchase_invoice_id, amount, payment_method)


def _match_or_reject_idempotent_payment(
    db: Session,
    *,
    client_transaction_id: str,
    purchase_invoice_id: int,
    amount: Decimal,
    payment_method: str,
) -> SupplierPayment | None:
    existing = db.execute(
        select(SupplierPayment).where(
            SupplierPayment.client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing_signature = _generate_payment_signature(
        existing.purchase_invoice_id, existing.amount, existing.payment_method
    )
    requested_signature = _generate_payment_signature(purchase_invoice_id, amount, payment_method)
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
    purchase_invoice_id: int,
    store_id: int,
    payment_date: date,
    payment_method: str,
    amount: Decimal,
    client_transaction_id: str,
    caller_store_id: int | None,
    reference: str | None = None,
    created_by: int | None = None,
) -> SupplierPayment:
    """Atomically: idempotency fast path -> validate -> lock the
    PurchaseInvoice row -> idempotency re-check (post-lock; see this
    module's create_purchase_invoice docstring and
    docs/M5_RETURNS_VOIDS_REFUNDS.md Section 11 for why this second check
    is not redundant under a genuine concurrent race) -> validate
    invoice state and payment amount -> apply -> audit -> post accounting
    -> return (caller commits).

    Overpayment is rejected outright (ConflictError, error_code=
    OVERPAYMENT) rather than silently accepted or capped — M6 does not
    implement a supplier-credit/advance model (M6 task Section 8: "If
    overpayments/advances are deferred, reject them explicitly")."""
    if payment_method not in SUPPLIER_PAYMENT_METHODS:
        raise ValidationAppError(
            f"Invalid payment method {payment_method!r}", error_code="INVALID_PAYMENT_METHOD"
        )
    if amount <= 0:
        raise ValidationAppError("Payment amount must be positive", error_code="INVALID_AMOUNT")

    existing = _match_or_reject_idempotent_payment(
        db,
        client_transaction_id=client_transaction_id,
        purchase_invoice_id=purchase_invoice_id,
        amount=amount,
        payment_method=payment_method,
    )
    if existing is not None:
        return existing

    if caller_store_id is not None:
        actual_store_id = db.execute(
            select(PurchaseInvoice.store_id).where(PurchaseInvoice.id == purchase_invoice_id)
        ).scalar_one_or_none()
        if actual_store_id is not None and actual_store_id != caller_store_id:
            raise ForbiddenError(
                f"Your account is scoped to store {caller_store_id} and cannot pay an "
                f"invoice in store {actual_store_id}",
                error_code="STORE_ACCESS_DENIED",
            )

    invoice = db.execute(
        select(PurchaseInvoice).where(PurchaseInvoice.id == purchase_invoice_id).with_for_update()
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"Purchase invoice {purchase_invoice_id} not found")

    existing = _match_or_reject_idempotent_payment(
        db,
        client_transaction_id=client_transaction_id,
        purchase_invoice_id=purchase_invoice_id,
        amount=amount,
        payment_method=payment_method,
    )
    if existing is not None:
        return existing

    if invoice.store_id != store_id:
        raise ConflictError(
            f"Purchase invoice {purchase_invoice_id} does not belong to store {store_id}",
            error_code="STORE_MISMATCH",
        )
    if invoice.status not in ("POSTED", "PARTIALLY_PAID"):
        raise ConflictError(
            f"Purchase invoice {purchase_invoice_id} is {invoice.status} and cannot accept a "
            "payment (must be POSTED or PARTIALLY_PAID)",
            error_code="INVALID_INVOICE_STATE",
        )
    remaining = invoice.grand_total - invoice.amount_paid
    if amount > remaining:
        raise ConflictError(
            f"Payment of {amount} exceeds the outstanding balance of {remaining} on purchase "
            f"invoice {purchase_invoice_id}",
            error_code="OVERPAYMENT",
        )

    payment = SupplierPayment(
        store_id=store_id,
        supplier_id=invoice.supplier_id,
        purchase_invoice_id=purchase_invoice_id,
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

    invoice.amount_paid = invoice.amount_paid + amount
    invoice.status = "PAID" if invoice.amount_paid == invoice.grand_total else "PARTIALLY_PAID"

    audit_service.log_event(
        db,
        user_id=created_by,
        action="SUPPLIER_PAYMENT_CREATED",
        entity_type="supplier_payment",
        entity_id=payment.id,
        after={
            "purchase_invoice_id": purchase_invoice_id,
            "amount": str(amount),
            "payment_method": payment_method,
            "resulting_invoice_status": invoice.status,
        },
    )

    accounting_service.post_supplier_payment_journal(
        db, supplier_payment=payment, created_by=created_by
    )

    db.flush()
    return payment


def get_supplier_payment(db: Session, supplier_payment_id: int) -> SupplierPayment:
    payment = db.get(SupplierPayment, supplier_payment_id)
    if payment is None:
        raise NotFoundError(f"Supplier payment {supplier_payment_id} not found")
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
        query = query.where(SupplierPayment.purchase_invoice_id == purchase_invoice_id)
    return list(db.execute(query).scalars().all())


# --- AP subledger / reporting ------------------------------------------------

_OUTSTANDING_INVOICE_STATUSES = ("POSTED", "PARTIALLY_PAID")


@dataclass(frozen=True)
class SupplierApSummary:
    supplier_id: int
    total_owed: Decimal  # Σ (grand_total - amount_paid) across POSTED/PARTIALLY_PAID invoices
    total_overdue: Decimal  # the portion of total_owed whose due_date has passed
    total_current: Decimal  # total_owed - total_overdue
    total_paid: Decimal  # Σ amount_paid across every invoice ever (lifetime)
    outstanding_purchase_clearing: Decimal  # received, not yet invoiced, across this supplier's POs


def get_supplier_ap_summary(
    db: Session, supplier_id: int, *, as_of: date | None = None
) -> SupplierApSummary:
    """Answers the M6 task Section 7 checklist for one supplier. Every
    figure here is derived directly from PurchaseInvoice/SupplierPayment
    rows (the AP subledger) — never from a separately-maintained running
    total, so it always reconciles to what the rows actually say."""
    as_of = as_of or date.today()
    invoices = list(
        db.execute(select(PurchaseInvoice).where(PurchaseInvoice.supplier_id == supplier_id))
        .scalars()
        .all()
    )
    total_owed = Decimal("0")
    total_overdue = Decimal("0")
    total_paid = Decimal("0")
    for invoice in invoices:
        total_paid += invoice.amount_paid
        if invoice.status in _OUTSTANDING_INVOICE_STATUSES:
            balance = invoice.grand_total - invoice.amount_paid
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
        outstanding_purchase_clearing=outstanding_clearing,
    )


@dataclass(frozen=True)
class SupplierTransaction:
    transaction_type: str  # "INVOICE" or "PAYMENT"
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
    transactions = [
        SupplierTransaction(
            transaction_type="INVOICE",
            id=inv.id,
            date_=inv.invoice_date,
            reference=inv.invoice_number,
            amount=inv.grand_total,
            status=inv.status,
        )
        for inv in invoices
    ] + [
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
    transactions.sort(key=lambda t: (t.date_, t.id))
    return transactions


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
    db: Session, *, store_id: int | None = None, as_of: date | None = None
) -> list[ApAgingRow]:
    """Standard aging buckets (current / 1-30 / 31-60 / 61-90 / 90+ days
    past due_date) for every outstanding (POSTED/PARTIALLY_PAID) invoice,
    grouped by supplier."""
    as_of = as_of or date.today()
    query = select(PurchaseInvoice).where(PurchaseInvoice.status.in_(_OUTSTANDING_INVOICE_STATUSES))
    if store_id is not None:
        query = query.where(PurchaseInvoice.store_id == store_id)
    invoices = list(db.execute(query).scalars().all())

    buckets: dict[int, dict[str, Decimal]] = {}
    for invoice in invoices:
        balance = invoice.grand_total - invoice.amount_paid
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


def ap_reconciliation(db: Session, *, store_id: int | None = None) -> ApReconciliationRow:
    """Compares the Accounts Payable GL control-account balance against
    the AP subledger total (Σ outstanding invoice balances) — the M6
    task Section 7/20 financial-control requirement, same shape as
    accounting.service.inventory_reconciliation. Expected to match
    exactly: every SupplierPayment reduces both the invoice's own
    amount_paid AND posts the identical amount to the AP account, and
    every posted invoice's grand_total is exactly what gets credited to
    AP (post_purchase_invoice_journal derives the AP credit directly from
    the invoice's own stored grand_total — never a separately-computed
    value)."""
    rows = accounting_service.trial_balance(db, store_id=store_id)
    ap_row = next((r for r in rows if r.account_code == ACCOUNT_ACCOUNTS_PAYABLE), None)
    gl_balance = (ap_row.total_credit - ap_row.total_debit) if ap_row else Decimal("0")

    query = select(PurchaseInvoice).where(PurchaseInvoice.status.in_(_OUTSTANDING_INVOICE_STATUSES))
    if store_id is not None:
        query = query.where(PurchaseInvoice.store_id == store_id)
    subledger_total = sum(
        (invoice.grand_total - invoice.amount_paid for invoice in db.execute(query).scalars()),
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
    db: Session, *, store_id: int | None = None
) -> PurchaseClearingReconciliationRow:
    """Compares the Purchase Clearing GL balance against the sum, across
    every PurchaseOrderItem (optionally scoped to one store), of the
    FIFO-priced value still received-but-not-invoiced
    (docs/M6_AP_VENDOR_ACCOUNTING.md 'Purchase Clearing lifecycle') — the
    M6 task Section 5/20 "no orphan clearing balances" requirement made
    checkable."""
    rows = accounting_service.trial_balance(db, store_id=store_id)
    clearing_row = next((r for r in rows if r.account_code == ACCOUNT_PURCHASE_CLEARING), None)
    gl_balance = (
        (clearing_row.total_credit - clearing_row.total_debit) if clearing_row else Decimal("0")
    )

    query = select(PurchaseOrder)
    if store_id is not None:
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
