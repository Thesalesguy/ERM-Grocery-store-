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
from decimal import Decimal

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.audit import service as audit_service
from app.modules.hr.models import (
    AttendanceRecord,
    CompensationPeriod,
    EmploymentAssignment,
    OvertimePolicy,
)
from app.modules.payroll.calculation import (
    CompensationInput,
    CompensationSegment,
    DeductionConfigInput,
    MissingCompensationCoverageError,
    OvertimePolicyInput,
    calculate_employee_pay,
)
from app.modules.payroll.models import (
    DeductionRate,
    DeductionType,
    PayrollDeductionLine,
    PayrollEarningLine,
    PayrollEmployeeResult,
    PayrollPeriod,
    PayrollReversal,
)

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


# --- Calculation (M10 Phase 5) -----------------------------------------------
#
# The pure calculation itself lives in app.modules.payroll.calculation —
# everything below is the INPUTS-gathering and RESULTS-persisting layer
# around it (M10_DESIGN.md Section 9's three-way split). `calculate`/
# `recalculate` are the same function: a period already CALCULATED is
# recalculated by deleting and reinserting its derived rows wholesale
# (never a row-by-row patch), exactly per the design.

_CALCULABLE_STATUSES = ("OPEN", "CALCULATED")


def _employees_assigned_to_store_during(
    db: Session, store_id: int, period_start: date, period_end: date
) -> list[int]:
    """Every employee with an EmploymentAssignment at this store
    overlapping the period — regardless of their CURRENT status
    (M10_DESIGN.md Section 4: a period may still reference attendance/
    compensation from when the employee was ACTIVE even after they
    later become TERMINATED)."""
    rows = db.execute(
        select(EmploymentAssignment.employee_id)
        .where(
            EmploymentAssignment.store_id == store_id,
            EmploymentAssignment.effective_from <= period_end,
            or_(
                EmploymentAssignment.effective_to.is_(None),
                EmploymentAssignment.effective_to >= period_start,
            ),
        )
        .distinct()
    ).scalars()
    return list(rows)


def _resolve_compensation_segments(
    db: Session, employee_id: int, period_start: date, period_end: date
) -> list[tuple[date, date, CompensationPeriod]]:
    rows = db.execute(
        select(CompensationPeriod)
        .where(
            CompensationPeriod.employee_id == employee_id,
            CompensationPeriod.effective_from <= period_end,
            or_(
                CompensationPeriod.effective_to.is_(None),
                CompensationPeriod.effective_to >= period_start,
            ),
        )
        .order_by(CompensationPeriod.effective_from)
    ).scalars()
    segments = []
    for comp in rows:
        seg_start = max(comp.effective_from, period_start)
        seg_end = (
            min(comp.effective_to, period_end) if comp.effective_to is not None else period_end
        )
        segments.append((seg_start, seg_end, comp))
    return segments


def _sum_attendance_hours(
    db: Session, employee_id: int, store_id: int, seg_start: date, seg_end: date
) -> Decimal:
    rows = db.execute(
        select(AttendanceRecord.clock_in_at, AttendanceRecord.clock_out_at).where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.store_id == store_id,
            AttendanceRecord.work_date >= seg_start,
            AttendanceRecord.work_date <= seg_end,
            AttendanceRecord.status == "CLOSED",
        )
    ).all()
    # timedelta.total_seconds() returns a float — feeding that into
    # Decimal() would import binary floating-point imprecision into a
    # financial calculation (e.g. Decimal(0.1) != Decimal("0.1")).
    # timedelta.days/.seconds/.microseconds are exact integers, so the
    # total is computed as an exact integer count of microseconds first.
    total_microseconds = 0
    for clock_in_at, clock_out_at in rows:
        delta = clock_out_at - clock_in_at
        total_microseconds += (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(total_microseconds) / Decimal(3_600_000_000)


def _resolve_overtime_policy(db: Session, as_of: date) -> OvertimePolicy | None:
    """The one GLOBAL overtime policy (M10 approved decision #7)
    effective as of the period's END date — a deterministic, documented
    choice (not left ambiguous) for the case where a policy changes
    mid-period."""
    return db.execute(
        select(OvertimePolicy).where(
            OvertimePolicy.effective_from <= as_of,
            or_(OvertimePolicy.effective_to.is_(None), OvertimePolicy.effective_to >= as_of),
        )
    ).scalar_one_or_none()


def _resolve_deduction_configs(db: Session, as_of: date) -> list[DeductionConfigInput]:
    """All DeductionRate rows effective as of the period's end date,
    joined with their DeductionType for category (M10 approved decision
    #8: generic, effective-dated, JSONB-parameterized configuration —
    no statutory formula hardcoded here)."""
    rows = db.execute(
        select(DeductionRate, DeductionType)
        .join(DeductionType, DeductionRate.deduction_type_id == DeductionType.id)
        .where(
            DeductionRate.effective_from <= as_of,
            or_(DeductionRate.effective_to.is_(None), DeductionRate.effective_to >= as_of),
            DeductionType.is_active.is_(True),
        )
    ).all()
    return [
        DeductionConfigInput(
            deduction_type_id=deduction_type.id,
            is_employer_contribution=deduction_type.category == "EMPLOYER_CONTRIBUTION",
            calculation_method=rate.calculation_method,
            parameters=rate.parameters,
        )
        for rate, deduction_type in rows
    ]


def _resolve_assignment_as_of(
    db: Session, employee_id: int, as_of: date
) -> EmploymentAssignment | None:
    return db.execute(
        select(EmploymentAssignment).where(
            EmploymentAssignment.employee_id == employee_id,
            EmploymentAssignment.effective_from <= as_of,
            or_(
                EmploymentAssignment.effective_to.is_(None),
                EmploymentAssignment.effective_to >= as_of,
            ),
        )
    ).scalar_one_or_none()


def calculate_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    actor_id: int | None,
    caller_store_id: int | None,
    client_transaction_id: str | None = None,
) -> PayrollPeriod:
    """OPEN -> CALCULATED, or CALCULATED -> CALCULATED (recalculate).
    Deletes and reinserts every PayrollEmployeeResult/*Line row for this
    period wholesale — never a row-by-row patch (M10_DESIGN.md Section
    9's explicit "clean, unambiguous supersession" requirement).

    Idempotency: `_lock_payroll_period`'s row lock is the PRIMARY
    concurrency defense (two concurrent calculate calls for the SAME
    period fully serialize — the second sees the first's committed
    result before doing any work of its own). `client_transaction_id`
    is stored for Phase 10's explicit-key idempotency layer to build on;
    this phase does not yet check it for duplicate-request detection."""
    if actor_id is None:
        raise ValidationAppError(
            "Calculation requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status not in _CALCULABLE_STATUSES:
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status} and cannot be "
            "(re)calculated",
            error_code="INVALID_PERIOD_STATE",
        )

    existing_result_ids = list(
        db.execute(
            select(PayrollEmployeeResult.id).where(
                PayrollEmployeeResult.payroll_period_id == period.id
            )
        ).scalars()
    )
    if existing_result_ids:
        db.execute(
            delete(PayrollEarningLine).where(
                PayrollEarningLine.payroll_employee_result_id.in_(existing_result_ids)
            )
        )
        db.execute(
            delete(PayrollDeductionLine).where(
                PayrollDeductionLine.payroll_employee_result_id.in_(existing_result_ids)
            )
        )
        db.execute(
            delete(PayrollEmployeeResult).where(PayrollEmployeeResult.id.in_(existing_result_ids))
        )
        db.flush()

    overtime_policy_row = _resolve_overtime_policy(db, period.period_end)
    overtime_policy_input = (
        OvertimePolicyInput(
            threshold_hours_per_period=overtime_policy_row.threshold_hours_per_period,
            multiplier=overtime_policy_row.multiplier,
        )
        if overtime_policy_row is not None
        else None
    )
    deduction_configs = _resolve_deduction_configs(db, period.period_end)

    employee_ids = _employees_assigned_to_store_during(
        db, period.store_id, period.period_start, period.period_end
    )

    total_gross = Decimal("0")
    total_deductions = Decimal("0")
    total_employer_contributions = Decimal("0")
    total_net_pay = Decimal("0")

    for employee_id in employee_ids:
        raw_segments = _resolve_compensation_segments(
            db, employee_id, period.period_start, period.period_end
        )
        engine_segments = []
        attendance_hours: dict[int, Decimal] = {}
        for index, (seg_start, seg_end, comp) in enumerate(raw_segments):
            engine_segments.append(
                CompensationSegment(
                    start=seg_start,
                    end=seg_end,
                    compensation=CompensationInput(
                        pay_type=comp.pay_type,
                        rate=comp.rate,
                        overtime_eligible=comp.overtime_eligible,
                    ),
                )
            )
            attendance_hours[index] = _sum_attendance_hours(
                db, employee_id, period.store_id, seg_start, seg_end
            )

        try:
            calc_result = calculate_employee_pay(
                compensation_segments=engine_segments,
                period_start=period.period_start,
                period_end=period.period_end,
                attendance_hours_by_segment_index=attendance_hours,
                overtime_policy=overtime_policy_input,
                deduction_configs=deduction_configs,
            )
        except MissingCompensationCoverageError as exc:
            raise ConflictError(
                f"Employee {employee_id} has incomplete compensation coverage for this "
                f"period: {exc}",
                error_code="MISSING_COMPENSATION_COVERAGE",
            ) from exc

        last_comp = raw_segments[-1][2]
        assignment = _resolve_assignment_as_of(db, employee_id, period.period_end)

        result = PayrollEmployeeResult(
            payroll_period_id=period.id,
            employee_id=employee_id,
            status="DRAFT",
            store_id=period.store_id,
            department_id=assignment.department_id if assignment is not None else None,
            position_id=assignment.position_id if assignment is not None else None,
            pay_type=last_comp.pay_type,
            pay_rate=last_comp.rate,
            currency=last_comp.currency,
            regular_hours=calc_result.regular_hours,
            overtime_hours=calc_result.overtime_hours,
            gross_pay=calc_result.gross_pay,
            total_deductions=calc_result.total_deductions,
            total_employer_contributions=calc_result.total_employer_contributions,
            net_pay=calc_result.net_pay,
        )
        db.add(result)
        db.flush()

        for earning_line in calc_result.earning_lines:
            db.add(
                PayrollEarningLine(
                    payroll_employee_result_id=result.id,
                    earning_type=earning_line.earning_type,
                    hours=earning_line.hours,
                    rate=earning_line.rate,
                    amount=earning_line.amount,
                    description=earning_line.description,
                )
            )
        for deduction_line in calc_result.deduction_lines:
            db.add(
                PayrollDeductionLine(
                    payroll_employee_result_id=result.id,
                    deduction_type_id=deduction_line.deduction_type_id,
                    is_employer_contribution=deduction_line.is_employer_contribution,
                    amount=deduction_line.amount,
                    description=deduction_line.description,
                )
            )

        total_gross += calc_result.gross_pay
        total_deductions += calc_result.total_deductions
        total_employer_contributions += calc_result.total_employer_contributions
        total_net_pay += calc_result.net_pay

    period.status = "CALCULATED"
    period.calculated_by = actor_id
    period.calculated_at = datetime.now(UTC)
    period.calculation_client_transaction_id = client_transaction_id
    period.total_gross = total_gross
    period.total_deductions = total_deductions
    period.total_employer_contributions = total_employer_contributions
    period.total_net_pay = total_net_pay
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Payroll period {payroll_period_id} calculation client_transaction_id "
            "was already used",
            error_code="DUPLICATE_CALCULATION_REQUEST",
        ) from exc

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_CALCULATED",
        entity_type="payroll_period",
        entity_id=period.id,
        after={"employee_count": len(employee_ids)},
    )
    db.commit()
    db.refresh(period)
    return period


def list_payroll_employee_results(
    db: Session, *, payroll_period_id: int
) -> list[PayrollEmployeeResult]:
    return list(
        db.execute(
            select(PayrollEmployeeResult)
            .where(PayrollEmployeeResult.payroll_period_id == payroll_period_id)
            .order_by(PayrollEmployeeResult.employee_id)
        )
        .scalars()
        .all()
    )


# --- Posting and reversal (M10 Phase 6) --------------------------------------
#
# The actual GL posting logic lives in
# app.modules.accounting.service.post_payroll_journal/
# post_payroll_reversal_journal — this module only orchestrates the
# payroll-side state transition around it, called INLINE before this
# function's own final commit (the same "posting is atomic with the
# operational event" rule every other domain in this codebase follows —
# see accounting/service.py's own module docstring).


def post_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    actor_id: int | None,
    caller_store_id: int | None,
    client_transaction_id: str | None = None,
) -> PayrollPeriod:
    """APPROVED -> POSTED. Idempotent by state: calling this on an
    already-POSTED period is a no-op returning the current row (mirrors
    `post_purchase_invoice`) — the DB's own partial unique index on
    (source_type='PAYROLL_POSTING', source_id=payroll_period_id) is the
    ultimate backstop against a genuine double-post slipping through."""
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status == "POSTED":
        return period
    if period.status != "APPROVED":
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status}, not APPROVED",
            error_code="INVALID_PERIOD_STATE",
        )
    if actor_id is None:
        raise ValidationAppError(
            "Posting requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    results = list_payroll_employee_results(db, payroll_period_id=period.id)
    if not results:
        raise ConflictError(
            f"Payroll period {payroll_period_id} has no calculated results to post",
            error_code="NO_CALCULATED_RESULTS",
        )

    journal_entry = accounting_service.post_payroll_journal(
        db, payroll_period=period, payroll_employee_results=results, posted_by=actor_id
    )

    period.status = "POSTED"
    period.journal_entry_id = journal_entry.id
    period.posted_by = actor_id
    period.posted_at = datetime.now(UTC)
    period.posting_client_transaction_id = client_transaction_id
    for result in results:
        result.status = "FINAL"
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Payroll period {payroll_period_id} posting client_transaction_id was " "already used",
            error_code="DUPLICATE_POSTING_REQUEST",
        ) from exc

    # M10 approved decision #5: identifiers and non-sensitive context
    # only — never gross/net pay or any other monetary amount.
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_POSTED",
        entity_type="payroll_period",
        entity_id=period.id,
        after={"journal_entry_id": journal_entry.id, "employee_count": len(results)},
    )
    db.commit()
    db.refresh(period)
    return period


def reverse_payroll_period(
    db: Session,
    *,
    payroll_period_id: int,
    reason: str,
    actor_id: int | None,
    caller_store_id: int | None,
) -> PayrollPeriod:
    """POSTED-only. `PayrollPeriod.status` NEVER changes on reversal — it
    stays POSTED forever (mirrors JournalEntry's own append-only
    convention); "was this period reversed" is a derived fact answered
    by whether a PayrollReversal row references it. Idempotent: a
    period already reversed returns the current row without creating a
    second reversal (the DB's own unique constraint on
    payroll_reversals.payroll_period_id is the backstop)."""
    if not reason or not reason.strip():
        raise ValidationAppError(
            "A reversal reason is required", error_code="REVERSAL_REASON_REQUIRED"
        )
    period = _lock_payroll_period(db, payroll_period_id)
    _enforce_store_access(caller_store_id, period.store_id, "payroll period")
    if period.status != "POSTED":
        raise ConflictError(
            f"Payroll period {payroll_period_id} is {period.status}, not POSTED",
            error_code="INVALID_PERIOD_STATE",
        )
    if actor_id is None:
        raise ValidationAppError(
            "Reversal requires an authenticated actor", error_code="ACTOR_REQUIRED"
        )

    existing_reversal = db.execute(
        select(PayrollReversal).where(PayrollReversal.payroll_period_id == period.id)
    ).scalar_one_or_none()
    if existing_reversal is not None:
        return period

    results = list_payroll_employee_results(db, payroll_period_id=period.id)
    reversal_entry = accounting_service.post_payroll_reversal_journal(
        db, payroll_period=period, payroll_employee_results=results, reversed_by=actor_id
    )

    db.add(
        PayrollReversal(
            payroll_period_id=period.id,
            reversal_journal_entry_id=reversal_entry.id,
            reason=reason,
            reversed_by=actor_id,
            reversed_at=datetime.now(UTC),
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Payroll period {payroll_period_id} has already been reversed",
            error_code="ALREADY_REVERSED",
        ) from exc

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="PAYROLL_PERIOD_REVERSED",
        entity_type="payroll_period",
        entity_id=period.id,
        after={"reversal_journal_entry_id": reversal_entry.id, "reason": reason},
    )
    db.commit()
    db.refresh(period)
    return period
