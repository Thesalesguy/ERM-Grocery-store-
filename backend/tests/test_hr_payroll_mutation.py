"""M10 Phase 14: mutation-style tests for HR/payroll's cleanly
interceptable protections (kept as permanent, automated regression
tests) — mirrors tests/test_ap_mutation.py's own precedent and, for the
route+service dual-layer store checks, tests/test_ap_api.py's
`test_store_isolation_mutation_test_removing_enforce_store_access`.

Each test below neuters exactly one protection (a monkeypatched
function, never an inline expression) and asserts the system's
behavior changes in the direction that proves the protection was
actually load-bearing — not just an optimization or a comment. Where a
protection is the ONLY line of defense, disabling it is expected to
(and does) let an otherwise-blocked action through; that is the
regression this test pins against a future accidental removal of the
real call site.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.v1.endpoints.hr as hr_endpoint
import app.api.v1.endpoints.payroll as payroll_endpoint
from app.core.exceptions import ConflictError
from app.modules.accounting.models import JournalEntry, JournalLine
from app.modules.auth.permissions import MANAGER
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.payroll import service as payroll_service
from app.modules.payroll.models import PayrollEmployeeResult
from app.modules.payroll.service import PayrollPeriodInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_store,
    make_user,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _hire(db: Session, store, **overrides):
    defaults = dict(
        employee_number=f"EMP-{unique_suffix()}",
        legal_name="Jane Doe",
        hire_date=date(2024, 1, 1),
        store_id=store.id,
    )
    defaults.update(overrides)
    return hr_service.hire_employee(
        db, EmployeeHireInput(**defaults), actor_id=None, caller_store_id=None
    )


def _pay(db: Session, employee_id: int, rate: Decimal = Decimal("2000.00")) -> None:
    hr_service.change_compensation(
        db,
        employee_id=employee_id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=rate,
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )


def _full_period_through_approved(db: Session, store, approver, **period_overrides):
    employee = _hire(db, store)
    _pay(db, employee.id)
    db.commit()
    defaults = dict(
        store_id=store.id,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 1, 15),
        pay_date=date(2024, 1, 20),
    )
    defaults.update(period_overrides)
    period = payroll_service.create_payroll_period(
        db, PayrollPeriodInput(**defaults), actor_id=None, caller_store_id=None
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    return payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )


# --- 1-2: hire_employee's dual-layer store check -----------------------


def test_hire_employee_route_layer_check_removed_service_layer_still_blocks(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disable the ROUTE-layer enforce_store_access call for hiring and
    confirm the request STILL gets 403 — hr_service.hire_employee's own
    _enforce_store_access is an independent second line of defense."""
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(hr_endpoint, "enforce_store_access", lambda *a, **kw: None)

    response = client.post(
        "/api/v1/hr/employees",
        json={
            "employee_number": f"EMP-{unique_suffix()}",
            "legal_name": "Cross Store Hire",
            "hire_date": "2024-01-01",
            "store_id": store_b.id,
        },
        headers=headers,
    )
    assert response.status_code == 403


def test_hire_employee_service_layer_check_removed_route_layer_still_blocks(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror image: disable hr_service's own _enforce_store_access
    and confirm the ROUTE-layer enforce_store_access call added for
    exactly this reason (M10 Phase 14 audit finding) still blocks the
    request — proving the new route-layer check is not decorative."""
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_b_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(hr_service, "_enforce_store_access", lambda *a, **kw: None)

    response = client.post(
        "/api/v1/hr/employees",
        json={
            "employee_number": f"EMP-{unique_suffix()}",
            "legal_name": "Cross Store Hire 2",
            "hire_date": "2024-01-01",
            "store_id": store_b.id,
        },
        headers=headers,
    )
    assert response.status_code == 403


# --- 3-4: create_payroll_period's dual-layer store check ----------------


def test_create_payroll_period_route_layer_check_removed_service_layer_still_blocks(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_c_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(payroll_endpoint, "enforce_store_access", lambda *a, **kw: None)

    response = client.post(
        "/api/v1/payroll/periods",
        json={
            "store_id": store_b.id,
            "period_start": "2024-01-01",
            "period_end": "2024-01-15",
            "pay_date": "2024-01-20",
        },
        headers=headers,
    )
    assert response.status_code == 403


def test_create_payroll_period_service_layer_check_removed_route_layer_still_blocks(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_d_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    monkeypatch.setattr(payroll_service, "_enforce_store_access", lambda *a, **kw: None)

    response = client.post(
        "/api/v1/payroll/periods",
        json={
            "store_id": store_b.id,
            "period_start": "2024-01-01",
            "period_end": "2024-01-15",
            "pay_date": "2024-01-20",
        },
        headers=headers,
    )
    assert response.status_code == 403


# --- 5-8: single-layer store checks that are the ONLY line of defense ---


def test_removing_compensation_change_store_check_reopens_cross_store_write(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """change_compensation has exactly ONE store check
    (_enforce_store_access_via_current_assignment). Neutering it lets a
    store-A-scoped caller silently change a store-B employee's pay —
    proving the check is necessary, not incidental."""
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_b)
    db.commit()

    monkeypatch.setattr(
        hr_service, "_enforce_store_access_via_current_assignment", lambda *a, **kw: None
    )

    # Would raise ForbiddenError with the real check in place.
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("9999.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=store_a.id,
    )
    current = hr_service.get_current_compensation(db, employee.id)
    assert current is not None
    assert current.rate == Decimal("9999.00")


def test_reassign_employee_target_store_check_is_independently_necessary(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reassign_employee checks BOTH the employee's current store and the
    target store. Neutering only the shared _enforce_store_access
    helper (both call sites) proves at least one of the two calls is
    load-bearing: a store-A caller can reassign a store-A employee INTO
    store B, which the real code forbids."""
    store_a = make_store(db)
    store_b = make_store(db)
    employee = _hire(db, store_a)
    db.commit()

    monkeypatch.setattr(hr_service, "_enforce_store_access", lambda *a, **kw: None)

    hr_service.reassign_employee(
        db,
        employee_id=employee.id,
        store_id=store_b.id,
        department_id=None,
        position_id=None,
        manager_employee_id=None,
        effective_from=date(2024, 2, 1),
        actor_id=None,
        caller_store_id=store_a.id,
    )
    moved = hr_service.get_current_assignment(db, employee.id)
    assert moved is not None
    assert moved.store_id == store_b.id


def test_removing_post_payroll_store_check_reopens_cross_store_posting(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """post_payroll_period's _enforce_store_access is its only store
    check. Neutering it lets a store-A-scoped caller post store B's
    payroll period, creating a real GL liability outside their store."""
    store_a = make_store(db)
    store_b = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store_b, approver)

    monkeypatch.setattr(payroll_service, "_enforce_store_access", lambda *a, **kw: None)

    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=store_a.id
    )
    assert posted.status == "POSTED"
    assert posted.journal_entry_id is not None


def test_removing_reverse_payroll_store_check_reopens_cross_store_reversal(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store_b, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )

    monkeypatch.setattr(payroll_service, "_enforce_store_access", lambda *a, **kw: None)

    reversed_period = payroll_service.reverse_payroll_period(
        db,
        payroll_period_id=posted.id,
        reason="cross-store reversal attempt",
        actor_id=approver.id,
        caller_store_id=store_a.id,
    )
    assert reversed_period.status == "POSTED"  # reversal never flips period status


# --- 9: cross-store payroll contamination at the calculation boundary ---


def test_removing_store_scoped_employee_filter_lets_another_stores_employee_into_the_run(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """calculate_payroll_period only pays employees returned by
    _employees_assigned_to_store_during(store_id=period.store_id, ...).
    Neutering it to ignore the store filter proves that function — not
    some incidental query shape — is what keeps one store's payroll run
    from paying another store's employees."""
    store_a = make_store(db)
    store_b = make_store(db)
    approver = make_user(db)
    employee_a = _hire(db, store_a)
    _pay(db, employee_a.id)
    employee_b = _hire(db, store_b)
    _pay(db, employee_b.id)
    db.commit()

    period = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store_a.id,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            pay_date=date(2024, 1, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )

    monkeypatch.setattr(
        payroll_service,
        "_employees_assigned_to_store_during",
        lambda db, store_id, start, end: [employee_a.id, employee_b.id],
    )

    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    paid_employee_ids = {
        r.employee_id
        for r in db.execute(
            select(PayrollEmployeeResult).where(
                PayrollEmployeeResult.payroll_period_id == period.id
            )
        ).scalars()
    }
    # With the real filter in place this would be {employee_a.id} only.
    assert employee_b.id in paid_employee_ids


# --- 10: missing-coverage gate must not silently produce a $0 result ----


def test_removing_missing_coverage_detection_would_silently_pay_zero(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _resolve_compensation_segments ever returned no segments for an
    assigned employee (e.g. a future bug), the calculation engine's
    MissingCompensationCoverageError is what stops the period from
    calculating at all. Neutering the resolver to always report zero
    segments proves the caller still refuses to proceed rather than
    quietly writing a $0 result into a real payroll period."""
    store = make_store(db)
    approver = make_user(db)
    employee = _hire(db, store)
    _pay(db, employee.id)
    db.commit()
    period = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store.id,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            pay_date=date(2024, 1, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )

    monkeypatch.setattr(
        payroll_service, "_resolve_compensation_segments", lambda db, employee_id, start, end: []
    )

    with pytest.raises(ConflictError):
        payroll_service.calculate_payroll_period(
            db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
        )
    db.rollback()
    reloaded = payroll_service.get_payroll_period(db, period.id)
    assert reloaded.status == "OPEN"
    assert (
        db.execute(
            select(PayrollEmployeeResult).where(
                PayrollEmployeeResult.payroll_period_id == period.id
            )
        )
        .scalars()
        .first()
        is None
    )


# --- 11: reversal recomputes with debit/credit swapped, never re-reads --


def test_reversal_journal_lines_are_the_swap_of_the_original_not_a_copy(db: Session) -> None:
    """Design invariant: reverse_payroll_period must never independently
    read back the original JournalLine rows and never produce a
    same-direction copy — it recomputes from the stored
    PayrollEmployeeResult/*Line data with debit and credit swapped.
    Proven end-to-end: the reversal's lines must be the exact mirror of
    the posting's lines, account-for-account."""
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    reversed_period = payroll_service.reverse_payroll_period(
        db,
        payroll_period_id=posted.id,
        reason="mutation test",
        actor_id=approver.id,
        caller_store_id=None,
    )
    db.commit()

    original_lines = {
        (line.account_id): (line.debit, line.credit)
        for line in db.execute(
            select(JournalLine).where(JournalLine.journal_entry_id == posted.journal_entry_id)
        ).scalars()
    }
    reversal_entry_id = (
        db.execute(
            select(JournalEntry.id).where(
                JournalEntry.source_type == "PAYROLL_REVERSAL",
                JournalEntry.source_id == reversed_period.id,
            )
        )
        .scalars()
        .one()
    )
    reversal_lines = {
        (line.account_id): (line.debit, line.credit)
        for line in db.execute(
            select(JournalLine).where(JournalLine.journal_entry_id == reversal_entry_id)
        ).scalars()
    }
    assert set(original_lines) == set(reversal_lines)
    for account_id, (debit, credit) in original_lines.items():
        rev_debit, rev_credit = reversal_lines[account_id]
        assert rev_debit == credit
        assert rev_credit == debit


# --- 12: append-only privilege carve-out also covers payroll journals ---


def test_runtime_role_cannot_delete_a_posted_payroll_journal_entry(db: Session) -> None:
    """Mirrors test_m10_schema.py's own
    test_runtime_role_cannot_update_or_delete_payroll_reversals, but for
    the journal_entries/journal_lines produced by payroll posting
    itself — the application's runtime DB role must not be able to
    delete a posted period's accounting trail even if application code
    tried to."""
    store = make_store(db)
    approver = make_user(db)
    period = _full_period_through_approved(db, store, approver)
    posted = payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=approver.id, caller_store_id=None
    )
    db.commit()

    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(
            JournalEntry.__table__.delete().where(JournalEntry.id == posted.journal_entry_id)
        )
    db.rollback()
