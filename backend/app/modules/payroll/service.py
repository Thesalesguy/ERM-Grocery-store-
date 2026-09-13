"""Payroll period lifecycle (M10 Phase 4). See docs/M10_DESIGN.md Section
8 for the full state machine:

    DRAFT --(open)--> OPEN --(calculate)--> CALCULATED --(approve)--> APPROVED --(post)--> POSTED
      |                                                                                       |
      +-- cancel (DRAFT/OPEN/CALCULATED/APPROVED only, never POSTED)          reverse (Phase 6,
                                                                                POSTED only) -->

This module builds the transitions that need neither the calculation
engine (Phase 5: `calculate`/`recalculate`) nor the accounting
integration (Phase 6: `post`/`reverse`) — `create`, `open`, `approve`,
and `cancel`. Building `approve` here even though nothing can reach
CALCULATED until Phase 5 exists is intentional: it is pure lifecycle/
business-rule code with no calculation-engine dependency, so it belongs
in this phase's skeleton rather than being artificially deferred.

Every transition is idempotent BY STATE (mirrors
`app.modules.ap.service.post_purchase_invoice`): calling `open` on an
already-OPEN period, or `cancel` on an already-CANCELLED one, returns
the current row rather than erroring — this is the PRIMARY idempotency
mechanism. `calculation_client_transaction_id`/`posting_client_transaction_id`
(Phase 5/6) are the SECONDARY, explicit-key layer for the specific race
where two concurrent callers both observe the pre-transition state
before either commits.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.audit import service as audit_service
from app.modules.hr.models import AttendanceRecord
from app.modules.payroll.models import PayrollPeriod

# Cancel is allowed from any state except POSTED (docs/M10_DESIGN.md
# Section 8's diagram) — CANCELLED itself and POSTED are both terminal.
_CANCELLABLE_STATUSES = ("DRAFT", "OPEN", "CALCULATED", "APPROVED")


def _enforce_store_access(caller_store_id: int | None, target_store_id: int, noun: str) -> None:
    """Mirrors every other module's own copy of this check (see
    app.modules.purchasing.service's identical helper for the reasoning
    behind duplicating rather than importing it)."""
    if caller_store_id is not None and caller_store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot "
            f"access {noun} in store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


@dataclass(frozen=True)
class PayrollPeriodInput:
    store_id: int
    period_start: date
    period_end: date
    pay_date: date
    payroll_run_id: str | None = None


def create_payroll_period(
    db: Session,
    data: PayrollPeriodInput,
    *,
    actor_id: int | None,
    caller_store_id: int | None,
) -> PayrollPeriod:
    _enforce_store_access(caller_store_id, data.store_id, "payroll period")
    if data.period_end < data.period_start:
        raise ValidationAppError(
            "period_end must not be before period_start", error_code="INVALID_PERIOD_RANGE"
        )
    if data.pay_date < data.period_end:
        raise ValidationAppError(
            "pay_date must not be before period_end", error_code="INVALID_PAY_DATE"
        )

    period = PayrollPeriod(
        store_id=data.store_id,
        period_start=data.period_start,
        period_end=data.period_end,
        pay_date=data.pay_date,
        payroll_run_id=data.payroll_run_id,
        status="DRAFT",
    )
    db.add(period)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Store {data.store_id} already has a payroll period covering "
            f"{data.period_start}..{data.period_end}",
            error_code="DUPLICATE_PAYROLL_PERIOD",
        ) from exc

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_CREATED",
        entity_type="payroll_period",
        entity_id=period.id,
        after={
            "store_id": data.store_id,
            "period_start": data.period_start,
            "period_end": data.period_end,
        },
    )
    db.commit()
    db.refresh(period)
    return period


def get_payroll_period(db: Session, payroll_period_id: int) -> PayrollPeriod:
    period = db.get(PayrollPeriod, payroll_period_id)
    if period is None:
        raise NotFoundError(f"Payroll period {payroll_period_id} not found")
    return period


def list_payroll_periods(
    db: Session,
    *,
    caller_store_id: int | None = None,
    status: str | None = None,
) -> list[PayrollPeriod]:
    query = select(PayrollPeriod).order_by(
        PayrollPeriod.store_id, PayrollPeriod.period_start.desc()
    )
    if caller_store_id is not None:
        query = query.where(PayrollPeriod.store_id == caller_store_id)
    if status is not None:
        query = query.where(PayrollPeriod.status == status)
    return list(db.execute(query).scalars().all())


def _lock_payroll_period(db: Session, payroll_period_id: int) -> PayrollPeriod:
    period = db.execute(
        select(PayrollPeriod).where(PayrollPeriod.id == payroll_period_id).with_for_update()
    ).scalar_one_or_none()
    if period is None:
        raise NotFoundError(f"Payroll period {payroll_period_id} not found")
    return period


def open_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    actor_id: int | None,
    caller_store_id: int | None,
) -> PayrollPeriod:
    """DRAFT -> OPEN: attendance is considered substantively complete and
    the period is "locked" for new attendance entry in the normal flow
    (M10_DESIGN.md Section 8) — this module does not itself enforce that
    lock on AttendanceRecord (Phase 3's clock_in/clock_out have no
    payroll-period awareness by design; the calculation engine, Phase 5,
    is what actually reads attendance for a period), so OPEN here is a
    pure status/audit checkpoint."""
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status != "DRAFT":
        if period.status == "OPEN":
            return period
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status}, not DRAFT",
            error_code="INVALID_PERIOD_STATE",
        )

    period.status = "OPEN"
    db.flush()
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_OPENED",
        entity_type="payroll_period",
        entity_id=period.id,
    )
    db.commit()
    db.refresh(period)
    return period


def approve_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    actor_id: int | None,
    caller_store_id: int | None,
) -> PayrollPeriod:
    """CALCULATED -> APPROVED. Staleness guard (M10_DESIGN.md failure-
    injection matrix item 4): if any AttendanceRecord within this
    period's store/date range was created or corrected AFTER
    `calculated_at`, the calculated numbers may be stale and approval is
    refused — recalculate (Phase 5) must run again first. Mirrors M9's
    own stale-recommendation re-validation philosophy."""
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status != "CALCULATED":
        if period.status == "APPROVED":
            return period
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status}, not CALCULATED",
            error_code="INVALID_PERIOD_STATE",
        )
    if actor_id is None:
        raise ValidationAppError(
            "Approval requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    if period.calculated_at is None:
        # Should be structurally impossible — ck_payroll_periods_calculation_consistency
        # requires calculated_at whenever status is CALCULATED — but this is a
        # clean business error instead of trusting an assert (which Python can
        # strip with -O) if that invariant is ever violated some other way.
        raise ConflictError(
            f"Payroll period {payroll_period_id} is CALCULATED but has no calculated_at "
            "timestamp — data integrity issue, contact support",
            error_code="MISSING_CALCULATION_TIMESTAMP",
        )
    stale_attendance_exists = db.execute(
        select(AttendanceRecord.id)
        .where(
            AttendanceRecord.store_id == period.store_id,
            AttendanceRecord.work_date >= period.period_start,
            AttendanceRecord.work_date <= period.period_end,
            AttendanceRecord.updated_at.is_not(None),
            AttendanceRecord.updated_at > period.calculated_at,
        )
        .limit(1)
    ).first()
    if stale_attendance_exists is not None:
        raise ConflictError(
            f"Payroll period {payroll_period_id} has attendance corrections newer than "
            "its last calculation — recalculate before approving",
            error_code="STALE_CALCULATION",
        )

    period.status = "APPROVED"
    period.approved_by = actor_id
    period.approved_at = datetime.now(UTC)
    db.flush()
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_APPROVED",
        entity_type="payroll_period",
        entity_id=period.id,
    )
    db.commit()
    db.refresh(period)
    return period


def cancel_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    reason: str,
    actor_id: int | None,
    caller_store_id: int | None,
) -> PayrollPeriod:
    if not reason or not reason.strip():
        raise ValidationAppError(
            "A cancellation reason is required", error_code="CANCELLATION_REASON_REQUIRED"
        )
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status == "CANCELLED":
        return period
    if period.status not in _CANCELLABLE_STATUSES:
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status} and can no longer "
            "be cancelled",
            error_code="INVALID_PERIOD_STATE",
        )

    period.status = "CANCELLED"
    db.flush()
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_CANCELLED",
        entity_type="payroll_period",
        entity_id=period.id,
        after={"reason": reason},
    )
    db.commit()
    db.refresh(period)
    return period
