"""M10 Phase 1 database test session (docs/M10_DESIGN.md): proves the
schema itself — not yet any service layer, which doesn't exist until
Phase 2 — actually enforces every invariant it claims to, against real
PostgreSQL. Required scenarios (per the M10 implementation task):
adjacent/overlapping/identical/open-ended/historical/concurrent
effective-dated periods, duplicate employee number, duplicate payroll
period, invalid monetary values, invalid lifecycle states, FK integrity,
and the append-only privilege carve-out on `payroll_reversals`.
"""

import threading
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from tests.factories import make_store, unique_suffix


def _make_employee(db: Session, **overrides) -> int:
    defaults = {
        "employee_number": f"EMP-{unique_suffix()}",
        "legal_name": "Test Employee",
        "hire_date": date(2024, 1, 1),
    }
    defaults.update(overrides)
    return db.execute(
        text(
            "INSERT INTO employees (employee_number, legal_name, hire_date, created_at) "
            "VALUES (:employee_number, :legal_name, :hire_date, now()) RETURNING id"
        ),
        defaults,
    ).scalar_one()


def _insert_status_period(
    db: Session, employee_id: int, status: str, effective_from: date, effective_to: date | None
) -> int:
    return db.execute(
        text(
            "INSERT INTO employment_status_periods "
            "(employee_id, status, effective_from, effective_to, created_at) "
            "VALUES (:employee_id, :status, :effective_from, :effective_to, now()) RETURNING id"
        ),
        {
            "employee_id": employee_id,
            "status": status,
            "effective_from": effective_from,
            "effective_to": effective_to,
        },
    ).scalar_one()


# --- Employee master data ---------------------------------------------------


def test_duplicate_employee_number_is_rejected(db: Session) -> None:
    number = f"EMP-{unique_suffix()}"
    _make_employee(db, employee_number=number)
    db.commit()
    with pytest.raises(IntegrityError):
        _make_employee(db, employee_number=number)
    db.rollback()


def test_employment_status_period_with_nonexistent_employee_is_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError):
        _insert_status_period(db, 999_999_999, "ACTIVE", date(2024, 1, 1), None)
    db.rollback()


def test_invalid_employment_status_is_rejected(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    with pytest.raises(IntegrityError):
        _insert_status_period(db, employee_id, "ON_VACATION_FOREVER", date(2024, 1, 1), None)
    db.rollback()


# --- Effective-dated non-overlap: EmploymentStatusPeriod (required scenarios) ---


def test_adjacent_status_periods_are_allowed(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), date(2024, 6, 30))
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 7, 1), date(2024, 12, 31))
    db.commit()


def test_overlapping_status_periods_are_rejected(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), date(2024, 6, 30))
    db.commit()
    with pytest.raises(IntegrityError):
        _insert_status_period(db, employee_id, "ON_LEAVE", date(2024, 3, 1), date(2024, 4, 30))
    db.rollback()


def test_identical_status_periods_are_rejected(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), date(2024, 6, 30))
    db.commit()
    with pytest.raises(IntegrityError):
        _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), date(2024, 6, 30))
    db.rollback()


def test_open_ended_current_period_blocks_a_second_open_ended_period(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), None)
    db.commit()
    with pytest.raises(IntegrityError):
        _insert_status_period(db, employee_id, "TERMINATED", date(2025, 1, 1), None)
    db.rollback()


def test_historical_period_before_an_open_ended_period_is_allowed(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2024, 1, 1), None)
    db.commit()
    _insert_status_period(db, employee_id, "ACTIVE", date(2020, 1, 1), date(2020, 12, 31))
    db.commit()


def test_concurrent_overlapping_status_period_inserts_serialize_to_one_success() -> None:
    """Proves the EXCLUDE constraint, not just application-level checking,
    is what prevents two genuinely concurrent transactions from both
    believing a date range is free (the exact race M9's hardening pass
    proved a service-layer-only check cannot defend against) — uses real
    independent connections, not the `db` fixture's savepoint isolation
    (see tests/test_purchasing_concurrency.py's module docstring for why)."""
    setup = SessionLocal()
    try:
        employee_id = setup.execute(
            text(
                "INSERT INTO employees (employee_number, legal_name, hire_date, created_at) "
                "VALUES (:n, 'Concurrency Test', '2024-01-01', now()) RETURNING id"
            ),
            {"n": f"EMP-{unique_suffix()}"},
        ).scalar_one()
        setup.commit()
    finally:
        setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(effective_from: date, effective_to: date) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            session.execute(
                text(
                    "INSERT INTO employment_status_periods "
                    "(employee_id, status, effective_from, effective_to, created_at) "
                    "VALUES (:employee_id, 'ACTIVE', :ef, :et, now())"
                ),
                {"employee_id": employee_id, "ef": effective_from, "et": effective_to},
            )
            session.commit()
            results.append("SUCCESS")
        except Exception:
            session.rollback()
            results.append("REJECTED")
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=(date(2025, 1, 1), date(2025, 6, 30)))
    t2 = threading.Thread(target=attempt, args=(date(2025, 3, 1), date(2025, 9, 30)))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert sorted(results) == ["REJECTED", "SUCCESS"]


# --- AttendanceRecord: overlap + VOIDED-correction exclusion -----------------


def test_overlapping_attendance_shifts_are_rejected(db: Session) -> None:
    store = make_store(db)
    employee_id = _make_employee(db)
    db.commit()
    db.execute(
        text(
            "INSERT INTO attendance_records "
            "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
            " created_at) "
            "VALUES (:e, :s, '2024-02-01', '2024-02-01 09:00:00+00', "
            "'2024-02-01 17:00:00+00', 'MANUAL', 'CLOSED', now())"
        ),
        {"e": employee_id, "s": store.id},
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO attendance_records "
                "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
                " created_at) "
                "VALUES (:e, :s, '2024-02-01', '2024-02-01 12:00:00+00', "
                "'2024-02-01 13:00:00+00', 'MANUAL', 'CLOSED', now())"
            ),
            {"e": employee_id, "s": store.id},
        )
    db.rollback()


def test_a_voided_correction_does_not_block_the_replacement_record(db: Session) -> None:
    store = make_store(db)
    employee_id = _make_employee(db)
    db.commit()
    original_id = db.execute(
        text(
            "INSERT INTO attendance_records "
            "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
            " created_at) "
            "VALUES (:e, :s, '2024-02-01', '2024-02-01 09:00:00+00', "
            "'2024-02-01 17:00:00+00', 'MANUAL', 'CLOSED', now()) RETURNING id"
        ),
        {"e": employee_id, "s": store.id},
    ).scalar_one()
    db.commit()
    db.execute(
        text("UPDATE attendance_records SET status = 'VOIDED' WHERE id = :id"),
        {"id": original_id},
    )
    db.commit()
    # The correction can now legitimately overlap the voided original.
    db.execute(
        text(
            "INSERT INTO attendance_records "
            "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
            " correction_of_id, correction_reason, created_at) "
            "VALUES (:e, :s, '2024-02-01', '2024-02-01 09:00:00+00', "
            "'2024-02-01 17:30:00+00', 'MANUAL', 'CLOSED', :orig, 'Forgot to clock out on time', "
            "now())"
        ),
        {"e": employee_id, "s": store.id, "orig": original_id},
    )
    db.commit()


def test_attendance_correction_without_reason_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee_id = _make_employee(db)
    db.commit()
    original_id = db.execute(
        text(
            "INSERT INTO attendance_records "
            "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
            " created_at) "
            "VALUES (:e, :s, '2024-02-01', '2024-02-01 09:00:00+00', "
            "'2024-02-01 17:00:00+00', 'MANUAL', 'VOIDED', now()) RETURNING id"
        ),
        {"e": employee_id, "s": store.id},
    ).scalar_one()
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO attendance_records "
                "(employee_id, store_id, work_date, clock_in_at, clock_out_at, source, status, "
                " correction_of_id, created_at) "
                "VALUES (:e, :s, '2024-02-01', '2024-02-01 09:00:00+00', "
                "'2024-02-01 17:30:00+00', 'MANUAL', 'CLOSED', :orig, now())"
            ),
            {"e": employee_id, "s": store.id, "orig": original_id},
        )
    db.rollback()


# --- Global effective-dated config: OvertimePolicy, DeductionRate -----------


def test_overlapping_overtime_policies_are_rejected(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO overtime_policies "
            "(threshold_hours_per_period, multiplier, effective_from, effective_to, created_at) "
            "VALUES (40, 1.5, '2024-01-01', '2024-12-31', now())"
        )
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO overtime_policies "
                "(threshold_hours_per_period, multiplier, effective_from, effective_to, "
                " created_at) "
                "VALUES (35, 2.0, '2024-06-01', '2025-01-01', now())"
            )
        )
    db.rollback()


def test_overtime_policy_with_multiplier_below_one_is_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO overtime_policies "
                "(threshold_hours_per_period, multiplier, effective_from, created_at) "
                "VALUES (40, 0.5, '2024-01-01', now())"
            )
        )
    db.rollback()


def test_overlapping_deduction_rates_are_rejected(db: Session) -> None:
    deduction_type_id = db.execute(
        text(
            "INSERT INTO deduction_types (code, name, category, created_at) "
            "VALUES (:code, 'Health Insurance', 'EMPLOYEE', now()) RETURNING id"
        ),
        {"code": f"HEALTH-{unique_suffix()}"},
    ).scalar_one()
    db.commit()
    db.execute(
        text(
            "INSERT INTO deduction_rates "
            "(deduction_type_id, calculation_method, parameters, effective_from, effective_to, "
            " created_at) "
            "VALUES (:t, 'PERCENT_OF_GROSS', '{\"percent\": \"5.00\"}', '2024-01-01', "
            "'2024-06-30', now())"
        ),
        {"t": deduction_type_id},
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO deduction_rates "
                "(deduction_type_id, calculation_method, parameters, effective_from, "
                " effective_to, created_at) "
                "VALUES (:t, 'PERCENT_OF_GROSS', '{\"percent\": \"6.00\"}', '2024-03-01', "
                "'2024-04-30', now())"
            ),
            {"t": deduction_type_id},
        )
    db.rollback()


# --- PayrollPeriod: duplicates, monetary values, lifecycle consistency ------


def test_duplicate_payroll_period_same_store_and_range_is_rejected(db: Session) -> None:
    store = make_store(db)
    db.commit()
    db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now())"
        ),
        {"s": store.id},
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-25', 'DRAFT', now())"
            ),
            {"s": store.id},
        )
    db.rollback()


def test_same_date_range_at_a_different_store_is_allowed(db: Session) -> None:
    """Proves per-store isolation (approved decision #1): the SAME
    (period_start, period_end) at a DIFFERENT store is not a duplicate."""
    store_a = make_store(db)
    store_b = make_store(db)
    db.commit()
    db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now())"
        ),
        {"s": store_a.id},
    )
    db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now())"
        ),
        {"s": store_b.id},
    )
    db.commit()


def test_payroll_period_pay_date_before_period_end_is_rejected(db: Session) -> None:
    store = make_store(db)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                "VALUES (:s, '2024-01-16', '2024-01-31', '2024-01-05', 'DRAFT', now())"
            ),
            {"s": store.id},
        )
    db.rollback()


def test_posted_payroll_period_without_journal_entry_is_rejected(db: Session) -> None:
    """Invalid lifecycle state: POSTED must always carry a journal_entry_id
    (and an approval), never a status flip with nothing behind it."""
    store = make_store(db)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'POSTED', now())"
            ),
            {"s": store.id},
        )
    db.rollback()


def test_approved_payroll_period_without_approver_is_rejected(db: Session) -> None:
    store = make_store(db)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'APPROVED', now())"
            ),
            {"s": store.id},
        )
    db.rollback()


def test_payroll_employee_result_net_pay_must_equal_gross_minus_deductions(db: Session) -> None:
    store = make_store(db)
    employee_id = _make_employee(db)
    db.commit()
    period_id = db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now()) RETURNING id"
        ),
        {"s": store.id},
    ).scalar_one()
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_employee_results "
                "(payroll_period_id, employee_id, status, store_id, pay_type, pay_rate, "
                " gross_pay, total_deductions, net_pay, created_at) "
                "VALUES (:p, :e, 'DRAFT', :s, 'HOURLY', 15.00, 1200.00, 100.00, 1150.00, now())"
            ),
            {"p": period_id, "e": employee_id, "s": store.id},
        )
    db.rollback()


def test_payroll_employee_result_negative_gross_pay_is_rejected(db: Session) -> None:
    store = make_store(db)
    employee_id = _make_employee(db)
    db.commit()
    period_id = db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now()) RETURNING id"
        ),
        {"s": store.id},
    ).scalar_one()
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_employee_results "
                "(payroll_period_id, employee_id, status, store_id, pay_type, pay_rate, "
                " gross_pay, total_deductions, net_pay, created_at) "
                "VALUES (:p, :e, 'DRAFT', :s, 'HOURLY', 15.00, -100.00, 0, -100.00, now())"
            ),
            {"p": period_id, "e": employee_id, "s": store.id},
        )
    db.rollback()


def test_compensation_period_negative_rate_is_rejected(db: Session) -> None:
    employee_id = _make_employee(db)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO compensation_periods "
                "(employee_id, pay_type, rate, pay_frequency, effective_from, created_at) "
                "VALUES (:e, 'HOURLY', -5.00, 'BIWEEKLY', '2024-01-01', now())"
            ),
            {"e": employee_id},
        )
    db.rollback()


# --- Append-only privilege carve-out: payroll_reversals ---------------------


def test_runtime_role_cannot_update_or_delete_payroll_reversals(db: Session) -> None:
    """Mirrors tests/test_constraints.py's audit_logs/inventory_movements
    check and accounting's journal_entries/journal_lines check — a
    PayrollReversal is genuinely append-only forever (a period is
    reversed exactly once) so UPDATE/DELETE must be rejected by
    PostgreSQL itself for the actual runtime role, not merely by
    application code choosing not to call them."""
    store = make_store(db)
    db.commit()
    period_id = db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now()) RETURNING id"
        ),
        {"s": store.id},
    ).scalar_one()
    user_id = db.execute(
        text(
            "INSERT INTO users (username, email, password_hash, full_name, is_active, "
            " created_at) "
            "VALUES (:u, :e, 'x', 'Test User', true, now()) RETURNING id"
        ),
        {"u": f"user_{unique_suffix()}", "e": f"{unique_suffix()}@test.local"},
    ).scalar_one()
    journal_entry_id = db.execute(
        text(
            "INSERT INTO journal_entries "
            "(journal_number, store_id, posting_date, entry_type, source_type, created_at) "
            "VALUES (:n, :s, '2024-01-20', 'STANDARD', 'MANUAL', now()) RETURNING id"
        ),
        {"n": f"JE-{unique_suffix()}", "s": store.id},
    ).scalar_one()
    db.commit()

    row_id = db.execute(
        text(
            "INSERT INTO payroll_reversals "
            "(payroll_period_id, reversal_journal_entry_id, reason, reversed_by, reversed_at, "
            " created_at) "
            "VALUES (:p, :j, 'Test reversal', :u, now(), now()) RETURNING id"
        ),
        {"p": period_id, "j": journal_entry_id, "u": user_id},
    ).scalar_one()
    db.commit()

    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(
            text("UPDATE payroll_reversals SET reason = 'HACKED' WHERE id = :id"),
            {"id": row_id},
        )
    db.rollback()

    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("DELETE FROM payroll_reversals WHERE id = :id"), {"id": row_id})
    db.rollback()


def test_second_reversal_of_the_same_payroll_period_is_rejected(db: Session) -> None:
    store = make_store(db)
    db.commit()
    period_id = db.execute(
        text(
            "INSERT INTO payroll_periods "
            "(store_id, period_start, period_end, pay_date, status, created_at) "
            "VALUES (:s, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now()) RETURNING id"
        ),
        {"s": store.id},
    ).scalar_one()
    user_id = db.execute(
        text(
            "INSERT INTO users (username, email, password_hash, full_name, is_active, "
            " created_at) "
            "VALUES (:u, :e, 'x', 'Test User', true, now()) RETURNING id"
        ),
        {"u": f"user_{unique_suffix()}", "e": f"{unique_suffix()}@test.local"},
    ).scalar_one()
    je1 = db.execute(
        text(
            "INSERT INTO journal_entries "
            "(journal_number, store_id, posting_date, entry_type, source_type, created_at) "
            "VALUES (:n, :s, '2024-01-20', 'STANDARD', 'MANUAL', now()) RETURNING id"
        ),
        {"n": f"JE-{unique_suffix()}", "s": store.id},
    ).scalar_one()
    je2 = db.execute(
        text(
            "INSERT INTO journal_entries "
            "(journal_number, store_id, posting_date, entry_type, source_type, created_at) "
            "VALUES (:n, :s, '2024-01-21', 'STANDARD', 'MANUAL', now()) RETURNING id"
        ),
        {"n": f"JE-{unique_suffix()}", "s": store.id},
    ).scalar_one()
    db.commit()

    db.execute(
        text(
            "INSERT INTO payroll_reversals "
            "(payroll_period_id, reversal_journal_entry_id, reason, reversed_by, reversed_at, "
            " created_at) "
            "VALUES (:p, :j, 'First reversal', :u, now(), now())"
        ),
        {"p": period_id, "j": je1, "u": user_id},
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payroll_reversals "
                "(payroll_period_id, reversal_journal_entry_id, reason, reversed_by, "
                " reversed_at, created_at) "
                "VALUES (:p, :j, 'Second reversal attempt', :u, now(), now())"
            ),
            {"p": period_id, "j": je2, "u": user_id},
        )
    db.rollback()
