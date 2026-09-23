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
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationAppError,
)
from app.core.rate_limit import approval_rate_limiter
from app.modules.accounting import service as accounting_service
from app.modules.accounting.service import SaleReturnLineEffect
from app.modules.audit import service as audit_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import Store
from app.modules.auth.permissions import SALES_RETURN_APPROVE
from app.modules.inventory import service as inventory_service
from app.modules.products.models import Product
from app.modules.sales.models import (
    PAYMENT_METHODS,
    Payment,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
)
from app.modules.shifts import service as shifts_service
from app.modules.tax.models import TaxRate

_MONEY_QUANTUM = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Mirrors app.modules.purchasing.service._enforce_store_access —
    duplicated (not imported) so this module keeps no dependency on the
    auth module and stays testable by calling its functions directly,
    same rationale as finalize_sale's existing store check."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


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
    # M15 (docs/M15_DESIGN.md "Shift association"): opportunistic, never
    # mandatory — if the cashier currently has no open shift, shift_id is
    # simply NULL and finalize_sale behaves exactly as it did before M15.
    active_shift = shifts_service.lock_active_shift_for_cashier(db, cashier_id)
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
        shift_id=active_shift.id if active_shift is not None else None,
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

    # Accounting posting happens inline, in the same uncommitted
    # transaction as everything above (M4 task Section 19: atomicity).
    # If this raises, the sale/items/payments/movements/audit row already
    # flushed above are discarded too — nothing commits until the route
    # handler's single db.commit() after finalize_sale returns.
    accounting_service.post_sale_journal(
        db,
        sale=sale,
        computed_lines=computed_lines,
        payments=payments,
        created_by=cashier_id,
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


# --- Sale returns / voids (M5) ----------------------------------------------
#
# docs/M5_RETURNS_VOIDS_REFUNDS.md has the full design. Summary of the
# load-bearing decisions:
#
# - A "void" is not a separate code path: every Sale here is created
#   already COMPLETED with inventory already moved, so void_sale() is a
#   thin wrapper that computes "every line's full remaining quantity"
#   and calls create_sale_return() with it — same locking, same
#   validation, same accounting, same audit machinery, distinguished
#   only by which audit action string is logged.
# - Return pricing NEVER reads current product price/tax/WAC — every
#   dollar amount is derived from the ORIGINAL SaleItem's frozen
#   unit_price_at_sale/unit_cost_at_sale and a *proportional, telescoping*
#   share of discount_amount/tax_amount (see _proportional_share below),
#   so repeated partial returns of one line can never sum to more than
#   the original line's discount/tax, regardless of rounding.
# - SaleItem.quantity_returned is a maintained cache incremented only
#   here, under a lock on the parent Sale row acquired FIRST — the exact
#   same "lock the parent to serialize writes to a child aggregate"
#   pattern app.modules.purchasing.service.receive_goods established for
#   PurchaseOrder/PurchaseOrderItem.quantity_received.
# - WAC on a restocked return reuses the existing
#   inventory_service.compute_new_wac unchanged — a return is treated as
#   a "receipt" of previously-owned inventory at its own historical cost
#   (unit_cost_at_sale). If nothing else has changed a product's WAC
#   since the original sale, this returns the pre-sale WAC exactly
#   (averaging a cost with itself); if the WAC has since moved, it
#   correctly blends the returned units' known historical cost into the
#   current average — no new algorithm, the existing one already
#   supports this.


@dataclass(frozen=True)
class SaleReturnLineInput:
    sale_item_id: int
    quantity: Decimal
    restock: bool = True


def _generate_return_number(store_id: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"RET{store_id}-{timestamp}-{secrets.token_hex(3).upper()}"


def _proportional_share(
    *,
    total: Decimal,
    original_qty: Decimal,
    already_returned_qty: Decimal,
    this_return_qty: Decimal,
) -> Decimal:
    """The telescoping-entitlement technique: this return's share of a
    line-level aggregate (discount_amount or tax_amount) is the
    difference between the rounded cumulative entitlement AFTER this
    return and the rounded cumulative entitlement BEFORE it — not
    `total * this_return_qty / original_qty` computed fresh each time.

    This guarantees the sum of every partial return's share, across any
    number of separate return transactions against the same line, never
    exceeds `total` and telescopes to exactly `total` once the full
    quantity has been returned — regardless of how rounding falls on any
    individual return. If `original_qty` were ever zero this would
    divide by zero, but SaleItem.quantity has a CHECK (> 0), so an
    original line always has a positive quantity to divide by.
    """
    if total == 0:
        return Decimal("0")
    new_cumulative_qty = already_returned_qty + this_return_qty
    entitled_after = _round_money(total * new_cumulative_qty / original_qty)
    entitled_before = _round_money(total * already_returned_qty / original_qty)
    return entitled_after - entitled_before


def _compute_line_refund(
    sale_item: SaleItem, requested_qty: Decimal
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Returns (refund_price, discount_refunded, tax_refunded,
    line_refund_amount) for returning `requested_qty` of `sale_item`.
    Pure — reads only the frozen SaleItem fields already loaded, no DB
    write. Factored out (M14, docs/M14_DESIGN.md) so the SAME computation
    both (a) decides whether the M14 approval gate applies, before any
    row is created, and (b) produces the SaleReturnItem actually posted —
    the amount a manager approves and the amount that posts are
    guaranteed identical by construction, not by convention.
    """
    already_returned = sale_item.quantity_returned
    refund_price = _round_money(sale_item.unit_price_at_sale * requested_qty)
    discount_refunded = _proportional_share(
        total=sale_item.discount_amount,
        original_qty=sale_item.quantity,
        already_returned_qty=already_returned,
        this_return_qty=requested_qty,
    )
    tax_refunded = _proportional_share(
        total=sale_item.tax_amount,
        original_qty=sale_item.quantity,
        already_returned_qty=already_returned,
        this_return_qty=requested_qty,
    )
    line_refund_amount = refund_price - discount_refunded + tax_refunded
    return refund_price, discount_refunded, tax_refunded, line_refund_amount


def _resolve_return_approval(
    db: Session,
    *,
    store: Store,
    refund_amount: Decimal,
    initiating_user_id: int | None,
    approver_username: str | None,
    approver_password: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[bool, int | None]:
    """The M14 approval gate (docs/M14_DESIGN.md). Returns
    (approval_required, approved_by) for the caller to freeze onto the
    new SaleReturn row. Raises before any SaleReturn/SaleReturnItem row
    is created (create_sale_return calls this before its first db.add),
    so a rejected approval leaves nothing to roll back — no orphaned
    approval state, no partially-authorized financial operation.

    Threshold semantics: `refund_amount >= store.return_approval_threshold_amount`
    ("at or above" — see docs/M14_DESIGN.md "Boundary semantics" for why
    this, not `>`, is the intended boundary). A NULL threshold means the
    gate is inactive for this store (pre-M14 behavior, unchanged).
    """
    threshold = store.return_approval_threshold_amount
    if threshold is None or refund_amount < threshold:
        return False, None

    if not approver_username or not approver_password:
        raise ForbiddenError(
            "This operation totals "
            f"{refund_amount} which is at or above this store's approval threshold "
            f"of {threshold}; a manager/admin approver's credentials are required",
            error_code="APPROVAL_REQUIRED",
        )

    approver = auth_service.verify_user_credentials(db, approver_username, approver_password)
    if approver is None:
        audit_service.log_event(
            db,
            user_id=None,
            action="SALE_RETURN_APPROVAL_FAILED",
            entity_type="store",
            entity_id=store.id,
            after={
                "attempted_approver_username": approver_username,
                "reason": "invalid_credentials",
                "refund_amount": refund_amount,
                "threshold": threshold,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        raise UnauthorizedError(
            "Invalid approver credentials", error_code="INVALID_APPROVER_CREDENTIALS"
        )

    if approver.id == initiating_user_id:
        audit_service.log_event(
            db,
            user_id=approver.id,
            action="SALE_RETURN_APPROVAL_FAILED",
            entity_type="store",
            entity_id=store.id,
            after={
                "reason": "self_approval_not_allowed",
                "refund_amount": refund_amount,
                "threshold": threshold,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        raise ForbiddenError(
            "The user who initiated this return/void cannot also approve it — a "
            "different manager or admin must approve",
            error_code="SELF_APPROVAL_NOT_ALLOWED",
        )

    approver_permissions = auth_service.get_user_permissions(db, approver.id)
    if SALES_RETURN_APPROVE not in approver_permissions:
        audit_service.log_event(
            db,
            user_id=approver.id,
            action="SALE_RETURN_APPROVAL_FAILED",
            entity_type="store",
            entity_id=store.id,
            after={
                "reason": "approval_permission_denied",
                "refund_amount": refund_amount,
                "threshold": threshold,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        raise ForbiddenError(
            "This user does not hold return/void approval authority",
            error_code="APPROVAL_PERMISSION_DENIED",
        )

    try:
        _enforce_store_access(approver.store_id, store.id, "this store's return approvals")
    except ForbiddenError:
        audit_service.log_event(
            db,
            user_id=approver.id,
            action="SALE_RETURN_APPROVAL_FAILED",
            entity_type="store",
            entity_id=store.id,
            after={
                "reason": "approval_store_access_denied",
                "approver_store_id": approver.store_id,
                "refund_amount": refund_amount,
                "threshold": threshold,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        raise

    audit_service.log_event(
        db,
        user_id=approver.id,
        action="SALE_RETURN_APPROVAL_GRANTED",
        entity_type="store",
        entity_id=store.id,
        after={
            "initiating_user_id": initiating_user_id,
            "refund_amount": refund_amount,
            "threshold": threshold,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    # A legitimate manager who mistyped their own password a couple of
    # times before succeeding shouldn't stay throttled afterward — mirrors
    # login_rate_limiter's own reset-on-success behavior exactly.
    if ip_address:
        approval_rate_limiter.reset(ip_address)
    return True, approver.id


def _match_or_reject_idempotent_return(
    db: Session,
    *,
    client_transaction_id: str,
    sale_id: int,
    refund_method: str,
    lines: list[SaleReturnLineInput],
) -> SaleReturn | None:
    """Looks up an existing SaleReturn by `client_transaction_id` and
    either returns it (identical payload), raises IDEMPOTENCY_KEY_CONFLICT
    (a different payload reusing the same key), or returns None (no prior
    return with this key — the caller may proceed).

    Called twice by create_sale_return: once before any lock (fast path
    for the common sequential-retry case), and once again immediately
    after the Sale row lock is acquired. The second call is not redundant
    — two callers racing with the SAME client_transaction_id can both pass
    the first, unlocked check before either has committed. Postgres holds
    the Sale row's FOR UPDATE lock until commit, so by the time the loser
    acquires that lock the winner's row (if any) is already committed and
    visible here, closing the race that would otherwise surface as the
    loser hitting SALE_NOT_RETURNABLE or EXCESSIVE_RETURN_QUANTITY instead
    of transparently receiving the winner's result.
    """
    existing = db.execute(
        select(SaleReturn).where(SaleReturn.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing_items = (
        db.execute(select(SaleReturnItem).where(SaleReturnItem.sale_return_id == existing.id))
        .scalars()
        .all()
    )
    existing_signature = sorted(
        (item.sale_item_id, item.quantity, item.restock) for item in existing_items
    )
    requested_signature = sorted((line.sale_item_id, line.quantity, line.restock) for line in lines)
    if (
        existing.sale_id != sale_id
        or existing.refund_method != refund_method
        or existing_signature != requested_signature
    ):
        raise ConflictError(
            f"client_transaction_id {client_transaction_id!r} was already used for a "
            "different return request",
            error_code="IDEMPOTENCY_KEY_CONFLICT",
        )
    return existing


def create_sale_return(
    db: Session,
    *,
    sale_id: int,
    store_id: int,
    return_date: date,
    lines: list[SaleReturnLineInput],
    refund_method: str,
    client_transaction_id: str,
    caller_store_id: int | None,
    reason: str | None = None,
    created_by: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    _is_void: bool = False,
    approver_username: str | None = None,
    approver_password: str | None = None,
) -> SaleReturn:
    """Atomically: idempotency fast path -> validate -> lock the Sale row
    -> validate sale/line state -> compute the refund total and check the
    M14 approval gate (docs/M14_DESIGN.md) -> lock affected product rows
    (restocked lines only, sorted, deadlock-safe) -> compute refund per
    line from frozen sale data -> post one inventory movement + WAC
    recompute per restocked line -> update quantity_returned -> advance
    Sale.status -> audit -> post accounting -> return (caller commits).

    `client_transaction_id` idempotency (mirrors finalize_sale/
    receive_goods exactly) is extended here per M5 task Section 8: a
    retried request with the SAME key and an equivalent payload returns
    the original return; the SAME key with a DIFFERENT payload (a
    conflicting reuse — e.g. a client bug reusing a UUID for a different
    return) is rejected with IDEMPOTENCY_KEY_CONFLICT rather than
    silently returning an unrelated result or silently creating a second
    one.

    M14: `approver_username`/`approver_password` are only consulted if
    the computed refund total requires approval (see
    _resolve_return_approval) — the approval check runs, and can raise,
    BEFORE the first `db.add` below, so a rejected approval creates
    nothing (no SaleReturn, no SaleReturnItem, no inventory movement, no
    journal entry) for the caller's rollback/no-commit to undo.
    """
    if refund_method not in PAYMENT_METHODS:
        raise ValidationAppError(
            f"Invalid refund method {refund_method!r}", error_code="INVALID_REFUND_METHOD"
        )

    # --- Idempotency fast path (before any lock/validation work). This
    # alone is NOT sufficient under concurrency: two callers with the same
    # client_transaction_id can both pass this check before either has
    # committed (see the second check after the Sale lock below, which is
    # what actually closes the race). -------------------------------------
    existing = _match_or_reject_idempotent_return(
        db,
        client_transaction_id=client_transaction_id,
        sale_id=sale_id,
        refund_method=refund_method,
        lines=lines,
    )
    if existing is not None:
        return existing

    if not lines:
        raise ValidationAppError("A return must have at least one line", error_code="EMPTY_RETURN")
    for line in lines:
        if line.quantity <= 0:
            raise ValidationAppError(
                "Return quantity must be positive", error_code="INVALID_QUANTITY"
            )

    # --- Store-scope check against the sale's REAL store, before any
    # lock is taken (fail fast, no wasted lock contention for a request
    # that's going to be rejected anyway — receive_goods's pattern). ----
    if caller_store_id is not None:
        actual_store_id = db.execute(
            select(Sale.store_id).where(Sale.id == sale_id)
        ).scalar_one_or_none()
        if actual_store_id is not None and actual_store_id != caller_store_id:
            raise ForbiddenError(
                f"Your account is scoped to store {caller_store_id} and cannot return "
                f"against a sale in store {actual_store_id}",
                error_code="STORE_ACCESS_DENIED",
            )

    # --- Lock the Sale row: serializes every write to any of its items'
    # quantity_returned (see module docstring above). --------------------
    sale = db.execute(select(Sale).where(Sale.id == sale_id).with_for_update()).scalar_one_or_none()
    if sale is None:
        raise NotFoundError(f"Sale {sale_id} not found")

    # --- Idempotency re-check, now that the Sale row's lock guarantees
    # any concurrent identical request either hasn't started or has fully
    # committed (see _match_or_reject_idempotent_return's docstring). ----
    existing = _match_or_reject_idempotent_return(
        db,
        client_transaction_id=client_transaction_id,
        sale_id=sale_id,
        refund_method=refund_method,
        lines=lines,
    )
    if existing is not None:
        return existing

    if sale.store_id != store_id:
        raise ConflictError(
            f"Sale {sale_id} does not belong to store {store_id}", error_code="STORE_MISMATCH"
        )
    if sale.status not in ("COMPLETED", "PARTIALLY_REFUNDED"):
        raise ConflictError(
            f"Sale {sale_id} is {sale.status} and cannot be returned against "
            "(must be COMPLETED or PARTIALLY_REFUNDED)",
            error_code="SALE_NOT_RETURNABLE",
        )

    # --- Resolve and validate every line's SaleItem, summing duplicate
    # lines against the same item (finalize_sale's cart-summing pattern).
    # Batched into one IN(...) query rather than one db.get() per line —
    # unlike the product-locking loop below (which needs FOR UPDATE in a
    # fixed per-row order for deadlock safety), this is a plain read with
    # no ordering requirement, so there is no reason to pay one
    # round-trip per line for a request that can carry up to 500. --------
    distinct_sale_item_ids = {line.sale_item_id for line in lines}
    sale_items_by_id: dict[int, SaleItem] = {
        item.id: item
        for item in db.execute(
            select(SaleItem).where(SaleItem.id.in_(distinct_sale_item_ids))
        ).scalars()
    }
    requested_qty_by_item: dict[int, Decimal] = {}
    restock_by_item: dict[int, bool] = {}
    for line in lines:
        sale_item = sale_items_by_id.get(line.sale_item_id)
        if sale_item is None or sale_item.sale_id != sale_id:
            # A tampered/foreign sale_item_id — never reveal whether the
            # ID exists at all, just that it's not valid on this sale.
            raise NotFoundError(f"Sale item {line.sale_item_id} not found on sale {sale_id}")
        requested_qty_by_item[sale_item.id] = (
            requested_qty_by_item.get(sale_item.id, Decimal("0")) + line.quantity
        )
        # If the same item is returned in two lines with conflicting
        # restock flags in one request, the more conservative (restock)
        # wins — never silently discards a restock the client asked for.
        restock_by_item[sale_item.id] = restock_by_item.get(sale_item.id, False) or line.restock

    for sale_item_id, requested_qty in requested_qty_by_item.items():
        sale_item = sale_items_by_id[sale_item_id]
        remaining = sale_item.quantity - sale_item.quantity_returned
        if requested_qty > remaining:
            raise ConflictError(
                f"Cannot return {requested_qty} of sale item {sale_item_id}: only "
                f"{remaining} remains returnable (of {sale_item.quantity} originally sold)",
                error_code="EXCESSIVE_RETURN_QUANTITY",
            )

    # --- M14 approval gate (docs/M14_DESIGN.md): compute the exact refund
    # total this request would produce, purely from the already-resolved,
    # already-locked-via-Sale SaleItem data above — no mutation has
    # happened yet, so this is safe to compute (and to raise out of)
    # before any product lock or row creation. The SAME per-line helper
    # is called again in the posting loop below for the SAME items/
    # quantities, so the amount approved here and the amount posted below
    # are always identical. -----------------------------------------------
    store = db.get(Store, store_id)
    if store is None:
        raise NotFoundError(f"Store {store_id} not found")
    total_refund_amount_for_gate = sum(
        (
            _compute_line_refund(sale_items_by_id[item_id], qty)[3]
            for item_id, qty in requested_qty_by_item.items()
        ),
        start=Decimal("0"),
    )
    approval_required, approved_by = _resolve_return_approval(
        db,
        store=store,
        refund_amount=total_refund_amount_for_gate,
        initiating_user_id=created_by,
        approver_username=approver_username,
        approver_password=approver_password,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # --- Lock every distinct product row that will actually be
    # restocked, ascending id order (deadlock-safe). ---------------------
    restock_product_ids = sorted(
        {
            sale_items_by_id[item_id].product_id
            for item_id, restock in restock_by_item.items()
            if restock
        }
    )
    locked_products: dict[int, Product] = {
        product_id: inventory_service.lock_product_for_update(db, product_id)
        for product_id in restock_product_ids
    }

    # M15 (docs/M15_DESIGN.md "Shift association"): the PROCESSING
    # cashier's own open shift at the moment this return/void is created
    # — may differ from whatever shift (if any) the ORIGINAL sale
    # happened under. Opportunistic, never mandatory, same as
    # finalize_sale's Sale.shift_id above.
    active_shift = (
        shifts_service.lock_active_shift_for_cashier(db, created_by)
        if created_by is not None
        else None
    )
    sale_return = SaleReturn(
        sale_id=sale_id,
        store_id=store_id,
        return_number=_generate_return_number(store_id),
        client_transaction_id=client_transaction_id,
        reason=reason,
        refund_method=refund_method,
        refund_amount=Decimal("0"),  # filled in below once every line is computed
        processed_by=created_by,
        approval_required=approval_required,
        approved_by=approved_by,
        shift_id=active_shift.id if active_shift is not None else None,
        return_date=return_date,
    )
    db.add(sale_return)
    try:
        db.flush()
    except IntegrityError:
        # Genuinely concurrent duplicate submission — see finalize_sale's
        # identical recovery block. Rolling back also releases the Sale/
        # product locks this attempt acquired.
        db.rollback()
        winner = db.execute(
            select(SaleReturn).where(SaleReturn.client_transaction_id == client_transaction_id)
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    audit_service.log_event(
        db,
        user_id=created_by,
        action="SALE_RETURN_INITIATED",
        entity_type="sale_return",
        entity_id=sale_return.id,
        after={
            "sale_id": sale_id,
            "line_count": len(lines),
            "client_transaction_id": client_transaction_id,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return_effects: list[SaleReturnLineEffect] = []
    total_refund_amount = Decimal("0")
    for sale_item_id, requested_qty in requested_qty_by_item.items():
        sale_item = sale_items_by_id[sale_item_id]
        restock = restock_by_item[sale_item_id]
        already_returned = sale_item.quantity_returned

        refund_price, discount_refunded, tax_refunded, line_refund_amount = _compute_line_refund(
            sale_item, requested_qty
        )
        total_refund_amount += line_refund_amount

        db.add(
            SaleReturnItem(
                sale_return_id=sale_return.id,
                sale_item_id=sale_item.id,
                quantity=requested_qty,
                unit_price_refunded=sale_item.unit_price_at_sale,
                discount_refunded=discount_refunded,
                tax_refunded=tax_refunded,
                unit_cost_refunded=sale_item.unit_cost_at_sale,
                restock=restock,
            )
        )

        if restock:
            product = locked_products[sale_item.product_id]
            new_wac = inventory_service.compute_new_wac(
                existing_qty=product.current_qty_on_hand,
                existing_wac=product.current_cost,
                received_qty=requested_qty,
                received_unit_cost=sale_item.unit_cost_at_sale,
            )
            inventory_service.record_movement(
                db,
                product=product,
                store_id=store_id,
                movement_type="SALE_RETURN",
                quantity_delta=requested_qty,
                unit_cost_at_movement=sale_item.unit_cost_at_sale,
                reference_type="sale_return",
                reference_id=sale_return.id,
                created_by=created_by,
                new_product_cost=new_wac,
            )

        sale_item.quantity_returned = already_returned + requested_qty

        return_effects.append(
            SaleReturnLineEffect(
                refund_price=refund_price,
                discount_refunded=discount_refunded,
                tax_refunded=tax_refunded,
                restock=restock,
                quantity=requested_qty,
                unit_cost_refunded=sale_item.unit_cost_at_sale,
            )
        )

    sale_return.refund_amount = total_refund_amount

    # --- Advance Sale.status (mirrors purchasing's
    # _advance_purchase_order_status exactly) ----------------------------
    all_items = list(sale.items)
    if all_items and all(item.quantity_returned >= item.quantity for item in all_items):
        sale.status = "REFUNDED"
    elif any(item.quantity_returned > 0 for item in all_items):
        sale.status = "PARTIALLY_REFUNDED"

    audit_service.log_event(
        db,
        user_id=created_by,
        action="VOID_COMPLETED" if _is_void else "SALE_RETURN_COMPLETED",
        entity_type="sale_return",
        entity_id=sale_return.id,
        after={
            "sale_id": sale_id,
            "return_number": sale_return.return_number,
            "refund_amount": total_refund_amount,
            "refund_method": refund_method,
            "resulting_sale_status": sale.status,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    accounting_service.post_sale_return_journal(
        db,
        sale_return=sale_return,
        return_date=return_date,
        return_lines=return_effects,
        created_by=created_by,
    )

    db.flush()
    return sale_return


def void_sale(
    db: Session,
    *,
    sale_id: int,
    store_id: int,
    return_date: date,
    refund_method: str,
    client_transaction_id: str,
    caller_store_id: int | None,
    reason: str | None = None,
    created_by: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    approver_username: str | None = None,
    approver_password: str | None = None,
) -> SaleReturn:
    """A full-sale void = a return of every line's full remaining
    quantity, restocked, in one call — see the module docstring above
    for why this is not a separate implementation. Idempotent under the
    same client_transaction_id as create_sale_return (they share one
    uniqueness space on sale_returns.client_transaction_id). Subject to
    the same M14 approval gate as an ordinary return (docs/M14_DESIGN.md)
    — voids are not exempt: the sale.void permission gates who may
    INITIATE a void, which is a separate question from who may APPROVE
    one that reaches the store's threshold."""
    sale = db.execute(select(Sale).where(Sale.id == sale_id).with_for_update()).scalar_one_or_none()
    if sale is None:
        raise NotFoundError(f"Sale {sale_id} not found")
    _enforce_store_access(caller_store_id, sale.store_id, "this sale")

    remaining_lines = [
        SaleReturnLineInput(
            sale_item_id=item.id, quantity=item.quantity - item.quantity_returned, restock=True
        )
        for item in sale.items
        if item.quantity > item.quantity_returned
    ]
    if not remaining_lines:
        raise ConflictError(
            f"Sale {sale_id} has nothing left to void (already fully refunded)",
            error_code="NOTHING_TO_VOID",
        )

    return create_sale_return(
        db,
        sale_id=sale_id,
        store_id=store_id,
        return_date=return_date,
        lines=remaining_lines,
        refund_method=refund_method,
        client_transaction_id=client_transaction_id,
        caller_store_id=caller_store_id,
        reason=reason or "Full sale void",
        created_by=created_by,
        ip_address=ip_address,
        user_agent=user_agent,
        _is_void=True,
        approver_username=approver_username,
        approver_password=approver_password,
    )


def get_sale_return(db: Session, sale_return_id: int) -> SaleReturn:
    sale_return = db.execute(
        select(SaleReturn)
        .options(selectinload(SaleReturn.items))
        .where(SaleReturn.id == sale_return_id)
    ).scalar_one_or_none()
    if sale_return is None:
        raise NotFoundError(f"Sale return {sale_return_id} not found")
    return sale_return


def list_sale_returns(
    db: Session,
    *,
    sale_id: int | None = None,
    store_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SaleReturn]:
    query = (
        select(SaleReturn)
        .options(selectinload(SaleReturn.items))
        .order_by(SaleReturn.created_at.desc(), SaleReturn.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if sale_id is not None:
        query = query.where(SaleReturn.sale_id == sale_id)
    if store_id is not None:
        query = query.where(SaleReturn.store_id == store_id)
    return list(db.execute(query).scalars().all())


@dataclass(frozen=True)
class SaleItemReturnEligibility:
    sale_item_id: int
    product_id: int
    quantity: Decimal
    quantity_returned: Decimal
    quantity_returnable: Decimal


def get_return_eligibility(db: Session, sale_id: int) -> list[SaleItemReturnEligibility]:
    """Read-only: how much of each line on this sale can still be
    returned. Used by the eligibility endpoint and the frontend return
    screen — never authoritative for what a return actually applies
    (create_sale_return re-validates everything itself, under a lock, at
    request time)."""
    sale = get_sale(db, sale_id)
    return [
        SaleItemReturnEligibility(
            sale_item_id=item.id,
            product_id=item.product_id,
            quantity=item.quantity,
            quantity_returned=item.quantity_returned,
            quantity_returnable=item.quantity - item.quantity_returned,
        )
        for item in sale.items
    ]
