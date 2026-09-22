"""Cashier/till shift session lifecycle: open, record a cash movement,
close (with expected-cash derivation, variance, and GL posting).

See docs/M15_DESIGN.md for the full design. Follows the exact
transaction-boundary/idempotency/store-isolation/audit conventions
established by app.modules.sales.service (finalize_sale/create_sale_return)
and app.modules.auth.service (verify_user_credentials/get_user_permissions)
— nothing here is a new pattern.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.audit import service as audit_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import Store
from app.modules.auth.permissions import SHIFT_OVERRIDE
from app.modules.sales.models import Payment, Sale, SaleReturn
from app.modules.shifts.models import CashierShift, CashMovement

_MONEY_QUANTUM = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Local duplicate of the same check every other service module
    makes — see app.modules.sales.service._enforce_store_access's
    docstring for why this is duplicated rather than imported."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


# --- Opening a shift ---------------------------------------------------------


def get_active_shift_for_cashier(db: Session, cashier_id: int) -> CashierShift | None:
    """Read-only, unlocked: the cashier's current OPEN shift, or None. Used
    by read-only callers (the /shifts/active endpoint, list/detail views)
    where a genuinely concurrent close racing the read is inconsequential
    — the caller isn't about to mutate anything based on this result.
    Sale/return attribution uses `lock_active_shift_for_cashier` below
    instead, precisely because it IS about to act on the result."""
    return db.execute(
        select(CashierShift).where(
            CashierShift.cashier_id == cashier_id, CashierShift.status == "OPEN"
        )
    ).scalar_one_or_none()


def lock_active_shift_for_cashier(db: Session, cashier_id: int) -> CashierShift | None:
    """Like `get_active_shift_for_cashier`, but with `FOR UPDATE` — used
    by app.modules.sales.service.finalize_sale/create_sale_return to
    attribute a cash sale/return to the cashier's active shift (docs/
    M15_DESIGN.md "Concurrency: sale finalization vs shift close").

    Why the lock matters: without it, a sale could read "shift X is open"
    a moment before close_shift (which itself takes `FOR UPDATE` on the
    shift row) closes it and freezes `expected_cash_amount` from
    whatever Sale rows were attributed and committed by that instant —
    a sale that read stale "open" state after that snapshot but before
    its own commit would attach `shift_id=X` to an now-CLOSED shift
    without ever being counted in its expected cash, silently
    understating the till by that sale's cash amount.

    Under PostgreSQL's default READ COMMITTED isolation, `SELECT ... FOR
    UPDATE WHERE status = 'OPEN'` re-evaluates the WHERE clause against
    the latest committed row once the lock is actually acquired — so if
    close_shift committed (flipping status to CLOSED) while this call was
    blocked waiting for the lock, this query correctly returns None once
    unblocked, rather than a stale CLOSED row. The caller (finalize_sale/
    create_sale_return) then simply proceeds with `shift_id=None` —
    opportunistic attribution, never a reason to fail the sale itself
    (docs/M15_DESIGN.md "Scope boundary")."""
    return db.execute(
        select(CashierShift)
        .where(CashierShift.cashier_id == cashier_id, CashierShift.status == "OPEN")
        .with_for_update()
    ).scalar_one_or_none()


def open_shift(
    db: Session,
    *,
    store_id: int,
    cashier_id: int,
    opening_float: Decimal,
    client_transaction_id: str,
    caller_store_id: int | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> CashierShift:
    """Atomically: idempotency fast path -> validate -> verify store ->
    verify no other OPEN shift already exists for this cashier -> create
    -> audit -> return (caller commits).

    `client_transaction_id` idempotency mirrors finalize_sale exactly: the
    same value resent on a retry returns the original shift instead of
    creating a second one; a UNIQUE constraint (not just the early SELECT)
    is what actually prevents two rows under genuine concurrency — see the
    IntegrityError recovery block below.

    `cashier_id` is always the AUTHENTICATED caller's own id (the shifts
    endpoint never accepts a target user id) — there is structurally no
    "open a shift for someone else" request to authorize against."""
    existing = db.execute(
        select(CashierShift).where(CashierShift.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if opening_float < 0:
        raise ValidationAppError(
            "Opening float cannot be negative", error_code="INVALID_OPENING_FLOAT"
        )

    _enforce_store_access(caller_store_id, store_id, "this store's shifts")
    store = db.get(Store, store_id)
    if store is None or not store.is_active:
        raise NotFoundError(f"Store {store_id} not found")

    # Friendly, race-tolerant pre-check (the partial unique index below is
    # the real enforcement under genuine concurrency — see the
    # IntegrityError recovery block).
    if get_active_shift_for_cashier(db, cashier_id) is not None:
        raise ConflictError(
            "This cashier already has an open shift — close it before opening another",
            error_code="SHIFT_ALREADY_OPEN",
        )

    shift = CashierShift(
        store_id=store_id,
        cashier_id=cashier_id,
        status="OPEN",
        opening_float=opening_float,
        opened_at=datetime.now(UTC),
        client_transaction_id=client_transaction_id,
    )
    db.add(shift)
    try:
        db.flush()
    except IntegrityError:
        # Either a genuinely concurrent duplicate client_transaction_id
        # submission, or a genuinely concurrent second open-shift request
        # for the same cashier racing past the pre-check above — the
        # uq_cashier_shifts_one_open_per_cashier partial unique index is
        # what actually decides the latter case. Roll back everything this
        # attempt did, then distinguish the two: if a row with this exact
        # key now exists, return it (the idempotent-retry case); otherwise
        # this was the open-shift race, so report it as such.
        db.rollback()
        winner = db.execute(
            select(CashierShift).where(CashierShift.client_transaction_id == client_transaction_id)
        ).scalar_one_or_none()
        if winner is not None:
            return winner
        if get_active_shift_for_cashier(db, cashier_id) is not None:
            raise ConflictError(
                "This cashier already has an open shift — close it before opening another",
                error_code="SHIFT_ALREADY_OPEN",
            ) from None
        raise

    audit_service.log_event(
        db,
        user_id=cashier_id,
        action="SHIFT_OPENED",
        entity_type="cashier_shift",
        entity_id=shift.id,
        after={"store_id": store_id, "opening_float": opening_float},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.flush()
    return shift


# --- Cash movements -----------------------------------------------------


def record_cash_movement(
    db: Session,
    *,
    shift_id: int,
    movement_type: str,
    amount: Decimal,
    reason: str,
    created_by: int,
    client_transaction_id: str,
    caller_store_id: int | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> CashMovement:
    """Atomically: idempotency fast path -> lock the shift (serializes
    against a concurrent close) -> verify open/authorized -> create ->
    audit -> return (caller commits).

    Authorization: the shift's own cashier may record a movement on their
    own shift; recording one on a DIFFERENT cashier's shift additionally
    requires `shift.override` (re-checked here, not just at the endpoint
    — the same belt-and-suspenders pattern
    app.modules.sales.service._resolve_return_approval uses for
    sales.return.approve, so a direct service call can't skip it either).
    """
    existing = db.execute(
        select(CashMovement).where(CashMovement.client_transaction_id == client_transaction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if amount <= 0:
        raise ValidationAppError(
            "Cash movement amount must be positive", error_code="INVALID_AMOUNT"
        )

    shift = db.execute(
        select(CashierShift).where(CashierShift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise NotFoundError(f"Cashier shift {shift_id} not found")
    _enforce_store_access(caller_store_id, shift.store_id, "this shift")

    if created_by != shift.cashier_id:
        actor_permissions = auth_service.get_user_permissions(db, created_by)
        if SHIFT_OVERRIDE not in actor_permissions:
            audit_service.log_event(
                db,
                user_id=created_by,
                action="CASH_MOVEMENT_REJECTED",
                entity_type="cashier_shift",
                entity_id=shift.id,
                after={"reason": "override_permission_denied", "movement_type": movement_type},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.commit()
            raise ForbiddenError(
                "Only the shift's own cashier, or a user with shift.override, may record "
                "a cash movement against it",
                error_code="SHIFT_OVERRIDE_REQUIRED",
            )

    if shift.status != "OPEN":
        raise ConflictError(
            f"Cashier shift {shift_id} is {shift.status}, not OPEN — cash movements can "
            "only be recorded against an open shift",
            error_code="SHIFT_NOT_OPEN",
        )

    movement = CashMovement(
        shift_id=shift_id,
        movement_type=movement_type,
        amount=amount,
        reason=reason,
        created_by=created_by,
        client_transaction_id=client_transaction_id,
    )
    db.add(movement)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(CashMovement).where(CashMovement.client_transaction_id == client_transaction_id)
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    audit_service.log_event(
        db,
        user_id=created_by,
        action="CASH_MOVEMENT_CREATED",
        entity_type="cashier_shift",
        entity_id=shift.id,
        after={"movement_type": movement_type, "amount": amount, "reason": reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.flush()
    return movement


# --- Closing a shift ------------------------------------------------------


def _compute_expected_cash(db: Session, shift: CashierShift) -> Decimal:
    """Derives expected physical cash EXACTLY from this shift's own
    authoritative Sale/Payment/SaleReturn/CashMovement rows — never a
    separately-maintained running balance (docs/M15_DESIGN.md "Expected
    cash formula").

    expected = opening_float
        + SUM(cash Payment.amount across sales attributed to this shift)
        - SUM(Sale.change_due across sales attributed to this shift)
        - SUM(refund_amount of CASH-method returns attributed to this shift)
        + SUM(PAID_IN cash movements) - SUM(PAID_OUT cash movements)

    Cash change handling (docs/M15_DESIGN.md "Shift association", worked
    from finalize_sale's actual payment semantics, not invented): a sale's
    CASH Payment.amount is what was physically TENDERED, not the amount
    applied to the sale — e.g. a $100 sale tendered with $150 cash has
    Payment(method='CASH').amount == 150 and Sale.change_due == 50. The
    physical cash increase from that one sale is 150 - 50 = 100, exactly
    the sale total, which is why change_due is subtracted once per sale
    (not per payment) here: finalize_sale's OVERPAYMENT_NOT_ALLOWED rule
    guarantees change can only ever originate from a cash overpayment, so
    subtracting the sale's whole change_due against the sale's cash
    tender is always correct, regardless of whether other, non-cash
    payments also contributed to the same sale.

    Non-cash tenders (CARD/MOBILE_MONEY/BANK_TRANSFER/OTHER) never appear
    in this formula at all — they don't touch the physical till."""
    cash_tendered = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(Sale, Sale.id == Payment.sale_id)
        .where(Sale.shift_id == shift.id, Payment.payment_method == "CASH")
    ).scalar_one()

    change_given = db.execute(
        select(func.coalesce(func.sum(Sale.change_due), 0)).where(Sale.shift_id == shift.id)
    ).scalar_one()

    cash_refunded = db.execute(
        select(func.coalesce(func.sum(SaleReturn.refund_amount), 0)).where(
            SaleReturn.shift_id == shift.id, SaleReturn.refund_method == "CASH"
        )
    ).scalar_one()

    paid_in = db.execute(
        select(func.coalesce(func.sum(CashMovement.amount), 0)).where(
            CashMovement.shift_id == shift.id, CashMovement.movement_type == "PAID_IN"
        )
    ).scalar_one()

    paid_out = db.execute(
        select(func.coalesce(func.sum(CashMovement.amount), 0)).where(
            CashMovement.shift_id == shift.id, CashMovement.movement_type == "PAID_OUT"
        )
    ).scalar_one()

    expected = (
        shift.opening_float
        + Decimal(cash_tendered)
        - Decimal(change_given if change_given is not None else 0)
        - Decimal(cash_refunded)
        + Decimal(paid_in)
        - Decimal(paid_out)
    )
    return _round_money(expected)


def close_shift(
    db: Session,
    *,
    shift_id: int,
    closing_counted_amount: Decimal,
    actor_id: int,
    caller_store_id: int | None,
    client_transaction_id: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> CashierShift:
    """Atomically: idempotency fast path -> lock the shift row -> verify
    open/authorized -> compute expected cash from authoritative records
    -> compute variance -> freeze the closing fields -> GL-post the
    variance (if nonzero) -> audit -> return (caller commits).

    Authorization mirrors record_cash_movement's self-vs-override split
    exactly (docs/M15_DESIGN.md "Manager override"): the shift's own
    cashier may always close it; a DIFFERENT actor additionally needs
    `shift.override`, re-checked here (not just at the endpoint) so a
    direct service call can't bypass it either. Unlike M14's approval
    gate, this is a PLAIN authorization check, not an inline-credential
    relay — the closing actor is already authenticated as themselves via
    the normal session; there is no second, different principal supplying
    credentials inline (docs/M15_DESIGN.md "Why M14's pattern doesn't
    fit")."""
    existing = db.execute(
        select(CashierShift).where(
            CashierShift.close_client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if closing_counted_amount < 0:
        raise ValidationAppError(
            "Closing counted amount cannot be negative", error_code="INVALID_COUNTED_AMOUNT"
        )

    shift = db.execute(
        select(CashierShift).where(CashierShift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise NotFoundError(f"Cashier shift {shift_id} not found")
    _enforce_store_access(caller_store_id, shift.store_id, "this shift")

    # Re-check idempotency now that the lock guarantees any concurrent
    # identical close request either hasn't started or has fully
    # committed (mirrors create_sale_return's second idempotency check
    # after acquiring the Sale row's lock).
    existing = db.execute(
        select(CashierShift).where(
            CashierShift.close_client_transaction_id == client_transaction_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if actor_id != shift.cashier_id:
        actor_permissions = auth_service.get_user_permissions(db, actor_id)
        if SHIFT_OVERRIDE not in actor_permissions:
            audit_service.log_event(
                db,
                user_id=actor_id,
                action="SHIFT_CLOSE_REJECTED",
                entity_type="cashier_shift",
                entity_id=shift.id,
                after={"reason": "override_permission_denied"},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.commit()
            raise ForbiddenError(
                "Only the shift's own cashier, or a user with shift.override, may close it",
                error_code="SHIFT_OVERRIDE_REQUIRED",
            )

    if shift.status != "OPEN":
        raise ConflictError(
            f"Cashier shift {shift_id} is already {shift.status}",
            error_code="SHIFT_NOT_OPEN",
        )

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SHIFT_CLOSE_INITIATED",
        entity_type="cashier_shift",
        entity_id=shift.id,
        after={"closing_counted_amount": closing_counted_amount},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    expected_cash_amount = _compute_expected_cash(db, shift)
    variance_amount = _round_money(closing_counted_amount - expected_cash_amount)

    shift.status = "CLOSED"
    shift.closed_at = datetime.now(UTC)
    shift.closing_counted_amount = closing_counted_amount
    shift.expected_cash_amount = expected_cash_amount
    shift.variance_amount = variance_amount
    shift.closed_by = actor_id
    shift.close_client_transaction_id = client_transaction_id
    try:
        db.flush()
    except IntegrityError:
        # A genuinely concurrent duplicate close request racing past the
        # first idempotency check but caught here by the UNIQUE constraint
        # on close_client_transaction_id.
        db.rollback()
        winner = db.execute(
            select(CashierShift).where(
                CashierShift.close_client_transaction_id == client_transaction_id
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="SHIFT_CLOSED",
        entity_type="cashier_shift",
        entity_id=shift.id,
        after={
            "expected_cash_amount": expected_cash_amount,
            "closing_counted_amount": closing_counted_amount,
            "variance_amount": variance_amount,
            "closed_by": actor_id,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # Accounting posting shares this same uncommitted transaction — see
    # app.modules.accounting.service.post_cash_shift_variance_journal's
    # docstring. Posts nothing (returns None) on exact reconciliation.
    accounting_service.post_cash_shift_variance_journal(db, shift=shift, created_by=actor_id)

    db.flush()
    return shift


def get_shift(db: Session, shift_id: int) -> CashierShift:
    shift = db.get(CashierShift, shift_id)
    if shift is None:
        raise NotFoundError(f"Cashier shift {shift_id} not found")
    return shift


def list_shifts(
    db: Session,
    *,
    store_id: int | None = None,
    cashier_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CashierShift]:
    query = (
        select(CashierShift)
        .order_by(CashierShift.opened_at.desc(), CashierShift.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if store_id is not None:
        query = query.where(CashierShift.store_id == store_id)
    if cashier_id is not None:
        query = query.where(CashierShift.cashier_id == cashier_id)
    if status is not None:
        query = query.where(CashierShift.status == status)
    return list(db.execute(query).scalars().all())


def list_cash_movements(db: Session, shift_id: int) -> list[CashMovement]:
    return list(
        db.execute(
            select(CashMovement)
            .where(CashMovement.shift_id == shift_id)
            .order_by(CashMovement.created_at)
        )
        .scalars()
        .all()
    )
