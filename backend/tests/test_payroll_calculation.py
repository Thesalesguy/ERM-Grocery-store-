"""M10 Phase 5: payroll calculation engine tests. Pure-function tests —
no database — covering hourly/salary earnings, overtime, compensation-
boundary splits, deduction calculation methods, and the missing-
compensation-coverage guard."""

from datetime import date
from decimal import Decimal

import pytest

from app.modules.payroll.calculation import (
    CompensationInput,
    CompensationSegment,
    DeductionConfigInput,
    MissingCompensationCoverageError,
    OvertimePolicyInput,
    calculate_employee_pay,
)


def _hourly_segment(start, end, rate, overtime_eligible=True) -> CompensationSegment:
    return CompensationSegment(
        start=start,
        end=end,
        compensation=CompensationInput(
            pay_type="HOURLY", rate=Decimal(rate), overtime_eligible=overtime_eligible
        ),
    )


def _salary_segment(start, end, rate) -> CompensationSegment:
    return CompensationSegment(
        start=start,
        end=end,
        compensation=CompensationInput(
            pay_type="SALARY", rate=Decimal(rate), overtime_eligible=False
        ),
    )


def test_simple_hourly_no_overtime() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_hourly_segment(date(2024, 1, 1), date(2024, 1, 15), "15.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        attendance_hours_by_segment_index={0: Decimal("40")},
        overtime_policy=OvertimePolicyInput(
            threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
        ),
        deduction_configs=[],
    )
    assert result.regular_hours == Decimal("40")
    assert result.overtime_hours == Decimal("0")
    assert result.gross_pay == Decimal("600.00")
    assert result.net_pay == Decimal("600.00")
    assert len(result.earning_lines) == 1
    assert result.earning_lines[0].earning_type == "REGULAR"


def test_hourly_with_overtime() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_hourly_segment(date(2024, 1, 1), date(2024, 1, 15), "20.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        attendance_hours_by_segment_index={0: Decimal("90")},
        overtime_policy=OvertimePolicyInput(
            threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
        ),
        deduction_configs=[],
    )
    assert result.regular_hours == Decimal("80")
    assert result.overtime_hours == Decimal("10")
    # 80 * 20 = 1600 regular; 10 * (20 * 1.5) = 300 overtime; gross = 1900
    assert result.gross_pay == Decimal("1900.00")
    earning_types = {line.earning_type for line in result.earning_lines}
    assert earning_types == {"REGULAR", "OVERTIME"}


def test_hourly_not_overtime_eligible_ignores_threshold() -> None:
    result = calculate_employee_pay(
        compensation_segments=[
            _hourly_segment(date(2024, 1, 1), date(2024, 1, 15), "20.00", overtime_eligible=False)
        ],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        attendance_hours_by_segment_index={0: Decimal("90")},
        overtime_policy=OvertimePolicyInput(
            threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
        ),
        deduction_configs=[],
    )
    assert result.regular_hours == Decimal("90")
    assert result.overtime_hours == Decimal("0")
    assert result.gross_pay == Decimal("1800.00")


def test_salary_full_period_single_segment() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_salary_segment(date(2024, 1, 1), date(2024, 1, 31), "3100.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[],
    )
    assert result.gross_pay == Decimal("3100.00")
    assert result.regular_hours == Decimal("0")


def test_salary_pro_rated_across_a_compensation_boundary() -> None:
    """The worked example from M10_DESIGN.md Section 6: a rate change
    mid-period is pro-rated by calendar days in each segment."""
    result = calculate_employee_pay(
        compensation_segments=[
            _salary_segment(date(2024, 6, 24), date(2024, 6, 30), "1400.00"),  # 7 days
            _salary_segment(date(2024, 7, 1), date(2024, 7, 7), "1400.00"),  # 7 days
        ],
        period_start=date(2024, 6, 24),
        period_end=date(2024, 7, 7),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[],
    )
    # 14-day period, two 7-day segments — each contributes half.
    assert len(result.earning_lines) == 2
    assert sum(line.amount for line in result.earning_lines) == result.gross_pay


def test_hourly_pro_rated_across_a_compensation_boundary_uses_per_segment_hours() -> None:
    result = calculate_employee_pay(
        compensation_segments=[
            _hourly_segment(date(2024, 6, 24), date(2024, 6, 30), "10.00"),
            _hourly_segment(date(2024, 7, 1), date(2024, 7, 7), "12.00"),
        ],
        period_start=date(2024, 6, 24),
        period_end=date(2024, 7, 7),
        attendance_hours_by_segment_index={0: Decimal("30"), 1: Decimal("32")},
        overtime_policy=OvertimePolicyInput(
            threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
        ),
        deduction_configs=[],
    )
    # 30*10 + 32*12 = 300 + 384 = 684, no overtime since each segment's
    # hours are well under the period threshold applied per segment.
    assert result.gross_pay == Decimal("684.00")
    assert result.regular_hours == Decimal("62")


def test_missing_compensation_for_the_whole_period_is_rejected() -> None:
    with pytest.raises(MissingCompensationCoverageError):
        calculate_employee_pay(
            compensation_segments=[],
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            attendance_hours_by_segment_index={},
            overtime_policy=None,
            deduction_configs=[],
        )


def test_gap_in_compensation_coverage_is_rejected() -> None:
    with pytest.raises(MissingCompensationCoverageError):
        calculate_employee_pay(
            compensation_segments=[
                _hourly_segment(date(2024, 1, 1), date(2024, 1, 5), "15.00"),
                # gap: Jan 6-9 has no compensation on file
                _hourly_segment(date(2024, 1, 10), date(2024, 1, 15), "15.00"),
            ],
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            attendance_hours_by_segment_index={0: Decimal("10"), 1: Decimal("10")},
            overtime_policy=OvertimePolicyInput(
                threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
            ),
            deduction_configs=[],
        )


# --- Deductions ---------------------------------------------------------


def test_flat_amount_deduction() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_salary_segment(date(2024, 1, 1), date(2024, 1, 31), "2000.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[
            DeductionConfigInput(
                deduction_type_id=1,
                is_employer_contribution=False,
                calculation_method="FLAT_AMOUNT",
                parameters={"amount": "50.00"},
            )
        ],
    )
    assert result.total_deductions == Decimal("50.00")
    assert result.net_pay == Decimal("1950.00")


def test_percent_of_gross_deduction() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_salary_segment(date(2024, 1, 1), date(2024, 1, 31), "2000.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[
            DeductionConfigInput(
                deduction_type_id=1,
                is_employer_contribution=False,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "5.00"},
            )
        ],
    )
    assert result.total_deductions == Decimal("100.00")
    assert result.net_pay == Decimal("1900.00")


def test_bracketed_deduction() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_salary_segment(date(2024, 1, 1), date(2024, 1, 31), "3000.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[
            DeductionConfigInput(
                deduction_type_id=1,
                is_employer_contribution=False,
                calculation_method="BRACKETED",
                parameters={
                    "brackets": [
                        {"upto": "1000.00", "percent": "0"},
                        {"upto": "2000.00", "percent": "10"},
                        {"upto": None, "percent": "20"},
                    ]
                },
            )
        ],
    )
    # First 1000 @ 0% = 0; next 1000 (1000-2000) @ 10% = 100;
    # remaining 1000 (2000-3000) @ 20% = 200. Total = 300.
    assert result.total_deductions == Decimal("300.00")
    assert result.net_pay == Decimal("2700.00")


def test_employer_contribution_does_not_reduce_net_pay() -> None:
    result = calculate_employee_pay(
        compensation_segments=[_salary_segment(date(2024, 1, 1), date(2024, 1, 31), "2000.00")],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 31),
        attendance_hours_by_segment_index={},
        overtime_policy=None,
        deduction_configs=[
            DeductionConfigInput(
                deduction_type_id=1,
                is_employer_contribution=True,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "3.00"},
            ),
            DeductionConfigInput(
                deduction_type_id=2,
                is_employer_contribution=False,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "5.00"},
            ),
        ],
    )
    assert result.total_employer_contributions == Decimal("60.00")
    assert result.total_deductions == Decimal("100.00")
    # net_pay excludes the employer contribution entirely.
    assert result.net_pay == Decimal("1900.00")
    assert result.gross_pay - result.total_deductions == result.net_pay


def test_net_math_invariant_holds_across_all_cases() -> None:
    """Mirrors the DB's own ck_payroll_employee_results_net_math CHECK
    constraint — this pure function must never produce a result that
    would violate it."""
    result = calculate_employee_pay(
        compensation_segments=[
            _hourly_segment(date(2024, 1, 1), date(2024, 1, 15), "18.50"),
        ],
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        attendance_hours_by_segment_index={0: Decimal("83.25")},
        overtime_policy=OvertimePolicyInput(
            threshold_hours_per_period=Decimal("80"), multiplier=Decimal("1.5")
        ),
        deduction_configs=[
            DeductionConfigInput(
                deduction_type_id=1,
                is_employer_contribution=False,
                calculation_method="PERCENT_OF_GROSS",
                parameters={"percent": "7.65"},
            )
        ],
    )
    assert result.net_pay == result.gross_pay - result.total_deductions
