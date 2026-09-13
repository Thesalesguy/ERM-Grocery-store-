"""The payroll calculation engine (M10 Phase 5). See docs/M10_DESIGN.md
Section 9: this module is a PURE function of its inputs — no DB reads,
no DB writes, no side effects — so there is exactly one authoritative
calculation, never a second, divergent implementation for (say) a
payslip preview versus the real posted result (mirrors M9 Design
Decision 1's "one authoritative position formula" precedent).

Boundary-split note: PayrollPeriod is PER-STORE (M10 approved decision
#1). If an employee transfers stores mid-period, their AttendanceRecord
rows for the OTHER store belong to that store's own separate
PayrollPeriod, never mixed into this one — so `EmploymentAssignment`
store changes need no split-attribution logic here at all. The one
split that IS still required (M10_DESIGN.md Section 5/6's worked
example) is a COMPENSATION rate change mid-period at the SAME store:
`compensation_segments` below may contain more than one segment, and
this function produces separate earning lines per segment rather than
applying one rate to the whole period.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

_MONEY_QUANTUM = Decimal("0.01")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CompensationInput:
    pay_type: str
    rate: Decimal
    overtime_eligible: bool


@dataclass(frozen=True)
class CompensationSegment:
    start: date
    end: date
    compensation: CompensationInput


@dataclass(frozen=True)
class OvertimePolicyInput:
    threshold_hours_per_period: Decimal
    multiplier: Decimal


@dataclass(frozen=True)
class DeductionConfigInput:
    deduction_type_id: int
    is_employer_contribution: bool
    calculation_method: str
    parameters: dict


@dataclass(frozen=True)
class EarningLineResult:
    earning_type: str
    amount: Decimal
    hours: Decimal | None = None
    rate: Decimal | None = None
    description: str | None = None


@dataclass(frozen=True)
class DeductionLineResult:
    deduction_type_id: int
    is_employer_contribution: bool
    amount: Decimal
    description: str | None = None


@dataclass(frozen=True)
class CalculationResult:
    regular_hours: Decimal
    overtime_hours: Decimal
    gross_pay: Decimal
    total_deductions: Decimal
    total_employer_contributions: Decimal
    net_pay: Decimal
    earning_lines: list[EarningLineResult] = field(default_factory=list)
    deduction_lines: list[DeductionLineResult] = field(default_factory=list)


class MissingCompensationCoverageError(Exception):
    """Raised when compensation_segments don't fully cover the period —
    an employee with attendance/pay obligations but no compensation on
    file for part of the period is a real configuration error, not a
    silent $0 for those days."""


def _apply_bracketed(base: Decimal, brackets: list[dict]) -> Decimal:
    """Progressive brackets: each bracket's `percent` applies only to
    the slice of `base` between the previous bracket's `upto` and its
    own. The last bracket may omit `upto` (or set it null) for an
    unbounded top bracket."""
    total = Decimal("0")
    lower = Decimal("0")
    for bracket in brackets:
        upto_raw = bracket.get("upto")
        percent = Decimal(str(bracket["percent"]))
        if upto_raw is None:
            portion = max(Decimal("0"), base - lower)
            total += portion * percent / Decimal("100")
            break
        upto = Decimal(str(upto_raw))
        portion = max(Decimal("0"), min(base, upto) - lower)
        total += portion * percent / Decimal("100")
        lower = upto
        if base <= upto:
            break
    return total


def _apply_deduction(config: DeductionConfigInput, gross_pay: Decimal) -> Decimal:
    method = config.calculation_method
    params = config.parameters
    if method == "FLAT_AMOUNT":
        return Decimal(str(params["amount"]))
    if method in ("PERCENT_OF_GROSS", "PERCENT_OF_TAXABLE"):
        # M10 has no separate "taxable income" concept (no statutory
        # deduction stack, no pre-tax-benefit ordering) — PERCENT_OF_TAXABLE
        # is accepted as a configuration value for forward compatibility
        # with a future jurisdiction integration, but is computed
        # identically to PERCENT_OF_GROSS today. Documented, not silent.
        percent = Decimal(str(params["percent"]))
        return gross_pay * percent / Decimal("100")
    if method == "BRACKETED":
        return _apply_bracketed(gross_pay, params["brackets"])
    raise ValueError(f"Unknown deduction calculation_method {method!r}")


def calculate_employee_pay(
    *,
    compensation_segments: list[CompensationSegment],
    period_start: date,
    period_end: date,
    attendance_hours_by_segment_index: dict[int, Decimal],
    overtime_policy: OvertimePolicyInput | None,
    deduction_configs: list[DeductionConfigInput],
) -> CalculationResult:
    if not compensation_segments:
        raise MissingCompensationCoverageError(
            "No compensation on file for any part of this period"
        )
    ordered = sorted(compensation_segments, key=lambda seg: seg.start)
    cursor = period_start
    for segment in ordered:
        if segment.start > cursor:
            raise MissingCompensationCoverageError(
                f"No compensation on file for {cursor}..{segment.start}"
            )
        cursor = max(cursor, segment.end + timedelta(days=1))
    if cursor <= period_end:
        raise MissingCompensationCoverageError(
            f"No compensation on file for {cursor}..{period_end}"
        )

    total_regular_hours = Decimal("0")
    total_overtime_hours = Decimal("0")
    gross_pay = Decimal("0")
    earning_lines: list[EarningLineResult] = []
    full_period_days = Decimal((period_end - period_start).days + 1)

    for index, segment in enumerate(ordered):
        comp = segment.compensation
        label = f"{segment.start.isoformat()}..{segment.end.isoformat()}"
        if comp.pay_type == "HOURLY":
            hours = attendance_hours_by_segment_index.get(index, Decimal("0"))
            if comp.overtime_eligible and overtime_policy is not None:
                threshold = overtime_policy.threshold_hours_per_period
                regular_hours = min(hours, threshold)
                overtime_hours = max(Decimal("0"), hours - threshold)
            else:
                regular_hours = hours
                overtime_hours = Decimal("0")

            regular_amount = _quantize(regular_hours * comp.rate)
            earning_lines.append(
                EarningLineResult(
                    earning_type="REGULAR",
                    hours=regular_hours,
                    rate=comp.rate,
                    amount=regular_amount,
                    description=label,
                )
            )
            gross_pay += regular_amount
            total_regular_hours += regular_hours

            if overtime_hours > 0:
                overtime_rate = comp.rate * overtime_policy.multiplier  # type: ignore[union-attr]
                overtime_amount = _quantize(overtime_hours * overtime_rate)
                earning_lines.append(
                    EarningLineResult(
                        earning_type="OVERTIME",
                        hours=overtime_hours,
                        rate=overtime_rate,
                        amount=overtime_amount,
                        description=label,
                    )
                )
                gross_pay += overtime_amount
                total_overtime_hours += overtime_hours
        elif comp.pay_type == "SALARY":
            segment_days = Decimal((segment.end - segment.start).days + 1)
            segment_amount = _quantize(comp.rate * segment_days / full_period_days)
            earning_lines.append(
                EarningLineResult(
                    earning_type="REGULAR",
                    hours=None,
                    rate=comp.rate,
                    amount=segment_amount,
                    description=label,
                )
            )
            gross_pay += segment_amount
        else:
            raise ValueError(f"Unknown pay_type {comp.pay_type!r}")

    deduction_lines: list[DeductionLineResult] = []
    total_deductions = Decimal("0")
    total_employer_contributions = Decimal("0")
    for config in deduction_configs:
        amount = _quantize(_apply_deduction(config, gross_pay))
        deduction_lines.append(
            DeductionLineResult(
                deduction_type_id=config.deduction_type_id,
                is_employer_contribution=config.is_employer_contribution,
                amount=amount,
            )
        )
        if config.is_employer_contribution:
            total_employer_contributions += amount
        else:
            total_deductions += amount

    net_pay = gross_pay - total_deductions

    return CalculationResult(
        regular_hours=total_regular_hours,
        overtime_hours=total_overtime_hours,
        gross_pay=gross_pay,
        total_deductions=total_deductions,
        total_employer_contributions=total_employer_contributions,
        net_pay=net_pay,
        earning_lines=earning_lines,
        deduction_lines=deduction_lines,
    )
