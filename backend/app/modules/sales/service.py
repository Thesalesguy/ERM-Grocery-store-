"""Sale finalization — the critical M2 transaction.

`finalize_sale` implements docs/TECHNICAL_BLUEPRINT.md Section E step 9 and
the M2 task's Section 12 sequence in full:

    validate cart/payments -> lock product rows (sorted, deadlock-safe)
    -> validate store/product consistency -> verify stock -> compute
    price/discount/tax/totals from authoritative catalog data -> validate
    payment -> create sale + items + payments + inventory movements
    -> audit -> return (caller commits)

Nothing here trusts client-submitted prices, tax amounts, or totals
(M2 task Section 14) — only product_id, quantity, and discount_amount are
read from the request; everything financial is computed from
`products`/`tax_rates`.

This function never calls db.commit() or db.rollback() itself. It only
db.flush()es, so every write it makes lives in the caller's ambient
transaction — a route handler commits once, after this returns
successfully; if this function raises anything, nothing it flushed was
ever committed (BR-3: a failed sale creates nothing — no sale, no items,
no payments, no inventory movement).

Concurrency (M2 task Section 13): stock verification happens only after
every affected product row is locked with SELECT ... FOR UPDATE, and
products are locked in ascending product_id order regardless of cart
order, so two concurrent sales that both touch products A and B can never
deadlock by acquiring them in opposite order. See
tests/test_concurrency.py for the automated proof against real
PostgreSQL: with 1 unit of stock and two simultaneous requests for 1 unit
each, exactly one succeeds and the other is rejected with
INSUFFICIENT_STOCK — never -1 stock, never two successful sales.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.audit import service as audit_service
from app.modules.auth.models import Store
from app.modules.inventory import service as inventory_service
from app.modules.products.models import Product
from app.modules.sales.models import Payment, Sale, SaleItem
from app.modules.tax.models import TaxRate

_MONEY_QUANTUM = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SaleLineInput:
    product_id: int
    quantity: Decimal
    discount_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class PaymentInput:
    payment_method: str
    amount: Decimal
    reference: str | None = None


@dataclass(frozen=True)
class _ComputedLine:
    product: Product
    quantity: Decimal
    unit_price: Decimal
    unit_cost: Decimal
    discount_amount: Decimal
    tax_rate_id: int | None
    tax_amount: Decimal
    line_total: Decimal
    line_subtotal: Decimal = field(compare=False)


def _generate_sale_number(store_id: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"S{store_id}-{timestamp}-{secrets.token_hex(3).upper()}"


def _resolve_tax(db: Session, product: Product) -> tuple[int | None, Decimal]:
    """Returns (tax_rate_id, rate_percent). Raises ConflictError if the
    product references a tax rate that isn't currently effective —
    docs/TECHNICAL_BLUEPRINT.md Section E step 7: "using each line's
    product.tax_class_id -> active tax_rates row as of sale date".

    Timezone policy (M2 hardening audit Section 5): "sale date" is the
    UTC calendar date at the instant of finalization — `date.today()`
    was a latent bug here, since it reads the server process's OS-local
    timezone (undefined/arbitrary depending on deployment, and
    inconsistent with `completed_at`'s `datetime.now(UTC)` a few lines
    below in the same function). A tax rate's effective_from/effective_to
    are plain DATE columns with no per-store timezone of their own, and
    `stores.timezone` is not wired into this comparison — using each
    store's local calendar day is a real future refinement (a rate that
    takes effect "at midnight" arguably means midnight in the store's own
    timezone, not UTC), but is out of scope for this hardening pass;
    UTC-everywhere is at least deterministic and matches every other
    timestamp this codebase produces.
    """
    if product.tax_rate_id is None:
        return None, Decimal("0")
    tax_rate = db.get(TaxRate, product.tax_rate_id)
    today = datetime.now(UTC).date()
    if (
        tax_rate is None
        or not tax_rate.is_active
        or tax_rate.effective_from > today
        or (tax_rate.effective_to is not None and tax_rate.effective_to < today)
    ):
        raise ConflictError(
            f"Product {product.id} references a tax rate that is not currently effective; "
            "fix the product's tax configuration before selling it",
            error_code="PRODUCT_TAX_CONFIGURATION_INVALID",
        )
    return tax_rate.id, tax_rate.rate_percent


def finalize_sale(
    db: Session,
    *,
    store_id: int,
    cashier_id: int,
    client_transaction_id: str,
    caller_store_id: int | None,
    lines: list[SaleLineInput],
    payments: list[PaymentInput],
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Sale:
    """`caller_store_id` is the authenticated cashier's own assigned store
    (`CurrentUser.store_id`), NOT the client-submitted `store_id` — a user
    scoped to one store must never be able to sell another store's
    inventory by simply changing the `store_id` field in the request body
    (M2 hardening audit Section 12). `None` means a cross-store user
    (Admin/Manager with no store assignment), who may operate against any
    store's `store_id`.

    `client_transaction_id` is the POS's idempotency key (M2 hardening
    audit Section 7): the same value resent on a retry (double-click,
    network retry) returns the original sale instead of creating a
    second one. See the early-return and IntegrityError-recovery blocks
    below.
    """
    if caller_store_id is not None and caller_store_id != store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"transact against store {store_id}",
            error_code="STORE_ACCESS_DENIED",
        )

    # --- Idempotency fast path ------------------------------------------
    # A sale with this exact client_transaction_id already committed
    # (the common case: the client's first response was lost to a network
    # error/timeout and it retried with the same key) — return it as-is
    # rather than re-running the whole transaction. This does NOT catch a
    # truly simultaneous duplicate submission (two requests with the same
    # key racing before either commits); the UNIQUE constraint on
    # sales.client_transaction_id is the real enforcement for that case —
    # see the IntegrityError handling around the Sale insert below.
    existing = db.execute(
        select(Sale).where(Sale.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if not lines:
        raise ValidationAppError("A sale must have at least one line", error_code="EMPTY_CART")
    if not payments:
        raise ValidationAppError("A sale must have at least one payment", error_code="NO_PAYMENT")

    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")

    # --- Lock every distinct product row up front, in a fixed order ----
    # (ascending product_id) regardless of cart order, so two concurrent
    # sales sharing two products can never deadlock by locking them in
    # opposite order.
    distinct_product_ids = sorted({line.product_id for line in lines})
    locked_products: dict[int, Product] = {}
    for product_id in distinct_product_ids:
        try:
            product = inventory_service.lock_product_for_update(db, product_id)
        except NoResultFound as exc:
            raise NotFoundError(f"Product {product_id} not found") from exc
        if product.store_id != store_id:
            raise ConflictError(
                f"Product {product_id} does not belong to store {store_id}",
                error_code="STORE_MISMATCH",
            )
        if not product.is_active:
            raise ConflictError(
                f"Product {product_id} is not active", error_code="PRODUCT_INACTIVE"
            )
        locked_products[product_id] = product

    # --- Verify stock BEFORE creating anything -------------------------
    # (M2 task Section 7: reject the whole sale, create nothing, if any
    # line is short). Quantities for the same product across multiple
    # lines are summed before checking, since a cart can scan one product
    # more than once.
    requested_qty: dict[int, Decimal] = {}
    for line in lines:
        if line.quantity <= 0:
            raise ValidationAppError(
                "Line quantity must be positive", error_code="INVALID_QUANTITY"
            )
        requested_qty[line.product_id] = requested_qty.get(line.product_id, Decimal("0")) + (
            line.quantity
        )
    for product_id, qty in requested_qty.items():
        product = locked_products[product_id]
        would_be = product.current_qty_on_hand - qty
        if would_be < 0 and not product.allow_negative_stock:
            raise ConflictError(
                f"Insufficient stock for product {product_id}: "
                f"{product.current_qty_on_hand} on hand, {qty} requested",
                error_code="INSUFFICIENT_STOCK",
            )

    # --- Compute line amounts from authoritative catalog/tax data ------
    computed_lines: list[_ComputedLine] = []
    subtotal = Decimal("0")
    discount_total = Decimal("0")
    tax_total = Decimal("0")
    for line in lines:
        product = locked_products[line.product_id]
        line_subtotal = _round_money(line.quantity * product.current_price)
        if line.discount_amount > line_subtotal:
            raise ValidationAppError(
                f"Discount ({line.discount_amount}) cannot exceed line subtotal "
                f"({line_subtotal}) for product {product.id}",
                error_code="INVALID_DISCOUNT",
            )
        taxable_amount = line_subtotal - line.discount_amount
        tax_rate_id, rate_percent = _resolve_tax(db, product)
        tax_amount = _round_money(taxable_amount * rate_percent / Decimal("100"))
        line_total = taxable_amount + tax_amount

        computed_lines.append(
            _ComputedLine(
                product=product,
                quantity=line.quantity,
                unit_price=product.current_price,
                unit_cost=product.current_cost,
                discount_amount=line.discount_amount,
                tax_rate_id=tax_rate_id,
                tax_amount=tax_amount,
                line_total=line_total,
                line_subtotal=line_subtotal,
            )
        )
        subtotal += line_subtotal
        discount_total += line.discount_amount
        tax_total += tax_amount

    grand_total = subtotal - discount_total + tax_total

    # --- Validate payment (server-authoritative; client totals are never
    # trusted) -----------------------------------------------------------
    total_paid = Decimal("0")
    non_cash_paid = Decimal("0")
    for payment in payments:
        total_paid += payment.amount
        if payment.payment_method != "CASH":
            non_cash_paid += payment.amount
    if non_cash_paid > grand_total:
        raise ConflictError(
            "Non-cash payments cannot exceed the sale total — only cash overpayment "
            "(as change) is supported",
            error_code="OVERPAYMENT_NOT_ALLOWED",
        )
    if total_paid < grand_total:
        raise ConflictError(
            f"Payment total ({total_paid}) is less than the amount due ({grand_total})",
            error_code="INSUFFICIENT_PAYMENT",
        )
    change_due = total_paid - grand_total

    # --- Create the sale -------------------------------------------------
    sale = Sale(
        store_id=store_id,
        sale_number=_generate_sale_number(store_id),
        client_transaction_id=client_transaction_id,
        cashier_id=cashier_id,
        status="COMPLETED",
        subtotal=subtotal,
        discount_total=discount_total,
        tax_total=tax_total,
        grand_total=grand_total,
        amount_tendered=total_paid,
        change_due=change_due,
        completed_at=datetime.now(UTC),
    )
    db.add(sale)
    try:
        db.flush()
    except IntegrityError:
        # The early idempotency check above missed a genuinely concurrent
        # duplicate submission (two requests with the same
        # client_transaction_id both passed the SELECT before either
        # committed). The UNIQUE constraint on sales.client_transaction_id
        # is what actually prevents two Sale rows here: PostgreSQL blocks
        # the second INSERT until the first's transaction ends, then
        # rejects it. Roll back everything this attempt did (no partial
        # sale/items/movements survive) and return the row that won.
        db.rollback()
        winner = db.execute(
            select(Sale).where(Sale.client_transaction_id == client_transaction_id)
        ).scalar_one_or_none()
        if winner is None:
            # The constraint fired for some other reason (extremely
            # unlikely, e.g. a hash collision on a different unique
            # column) — re-raise rather than silently returning nothing.
            raise
        return winner

    for computed in computed_lines:
        db.add(
            SaleItem(
                sale_id=sale.id,
                product_id=computed.product.id,
                quantity=computed.quantity,
                unit_price_at_sale=computed.unit_price,
                unit_cost_at_sale=computed.unit_cost,
                discount_amount=computed.discount_amount,
                tax_rate_id=computed.tax_rate_id,
                tax_amount=computed.tax_amount,
                line_total=computed.line_total,
            )
        )
        # One SALE movement per line (not per distinct product) — a
        # product scanned twice in one sale gets two ledger entries,
        # matching how it was actually rung up.
        inventory_service.record_movement(
            db,
            product=computed.product,
            store_id=store_id,
            movement_type="SALE",
            quantity_delta=-computed.quantity,
            unit_cost_at_movement=computed.unit_cost,
            reference_type="sale",
            reference_id=sale.id,
            created_by=cashier_id,
        )

    for payment in payments:
        db.add(
            Payment(
                sale_id=sale.id,
                payment_method=payment.payment_method,
                amount=payment.amount,
                reference=payment.reference,
            )
        )

    audit_service.log_event(
        db,
        user_id=cashier_id,
        action="SALE_COMPLETED",
        entity_type="sale",
        entity_id=sale.id,
        after={
            "sale_number": sale.sale_number,
            "grand_total": grand_total,
            "line_count": len(lines),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.flush()
    return sale


def get_sale(db: Session, sale_id: int) -> Sale:
    sale = db.execute(
        select(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .where(Sale.id == sale_id)
    ).scalar_one_or_none()
    if sale is None:
        raise NotFoundError(f"Sale {sale_id} not found")
    return sale


def list_sales(
    db: Session, *, store_id: int | None = None, limit: int = 50, offset: int = 0
) -> list[Sale]:
    query = (
        select(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .order_by(Sale.created_at.desc(), Sale.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(Sale.store_id == store_id)
    return list(db.execute(query).scalars().all())
