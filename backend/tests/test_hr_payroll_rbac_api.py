"""M10 Phase 11: full RBAC/multi-store matrix for HR and payroll,
exercised through the real HTTP endpoints. See app.modules.auth.permissions
for the full rationale, in particular:

- Manager gets payroll.approve but NEVER payroll.post/payroll.reverse
  (a documented conflict-of-interest decision, not an oversight).
- HR Clerk gets hr.read/hr.write/attendance.*/payroll.read/
  payroll.calculate but NEVER hr.compensation.write, payroll.approve,
  payroll.post, or payroll.reverse.
- A store-scoped user can never reach another store's employees or
  payroll periods.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import AUDITOR, CASHIER, HR_CLERK, MANAGER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role
from tests.helpers import auth_headers


def _hire_via_api(client: TestClient, headers: dict, store_id: int, **overrides) -> dict:
    payload = {
        "employee_number": f"EMP-{overrides.pop('suffix', 'X')}",
        "legal_name": "Jane Doe",
        "hire_date": "2024-01-01",
        "store_id": store_id,
    }
    payload.update(overrides)
    response = client.post("/api/v1/hr/employees", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- Cashier: no HR/payroll access at all -----------------------------------


def test_cashier_cannot_read_employees(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="rbac_cashier_1")
    db.commit()
    headers = auth_headers(client, "rbac_cashier_1", DEFAULT_TEST_PASSWORD)
    response = client.get("/api/v1/hr/employees", headers=headers)
    assert response.status_code == 403


def test_cashier_cannot_read_payroll_periods(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="rbac_cashier_2")
    db.commit()
    headers = auth_headers(client, "rbac_cashier_2", DEFAULT_TEST_PASSWORD)
    response = client.get("/api/v1/payroll/periods", headers=headers)
    assert response.status_code == 403


# --- HR Clerk: minimal scope, never accounting/reversal ---------------------


def test_hr_clerk_can_hire_and_manage_attendance(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, HR_CLERK, username="rbac_hrclerk_1", store_id=store.id)
    db.commit()
    headers = auth_headers(client, "rbac_hrclerk_1", DEFAULT_TEST_PASSWORD)

    employee = _hire_via_api(client, headers, store.id, suffix="HRC1")
    response = client.post(
        "/api/v1/hr/attendance/clock-in",
        json={
            "employee_id": employee["id"],
            "store_id": store.id,
            "clock_in_at": "2024-03-01T09:00:00Z",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_hr_clerk_cannot_read_or_write_compensation(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, HR_CLERK, username="rbac_hrclerk_2", store_id=store.id)
    db.commit()
    headers = auth_headers(client, "rbac_hrclerk_2", DEFAULT_TEST_PASSWORD)
    employee = _hire_via_api(client, headers, store.id, suffix="HRC2")

    write_response = client.post(
        f"/api/v1/hr/employees/{employee['id']}/compensation",
        json={
            "effective_from": "2024-01-01",
            "pay_type": "HOURLY",
            "rate": "15.00",
            "pay_frequency": "BIWEEKLY",
        },
        headers=headers,
    )
    assert write_response.status_code == 403

    read_response = client.get(
        f"/api/v1/hr/employees/{employee['id']}/compensation-history", headers=headers
    )
    assert read_response.status_code == 403


def test_hr_clerk_can_prepare_but_not_approve_payroll(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, HR_CLERK, username="rbac_hrclerk_3", store_id=store.id)
    db.commit()
    headers = auth_headers(client, "rbac_hrclerk_3", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/payroll/periods",
        json={
            "store_id": store.id,
            "period_start": "2024-01-01",
            "period_end": "2024-01-15",
            "pay_date": "2024-01-20",
        },
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text
    period_id = create_response.json()["id"]

    open_response = client.post(f"/api/v1/payroll/periods/{period_id}/open", headers=headers)
    assert open_response.status_code == 200

    approve_response = client.post(f"/api/v1/payroll/periods/{period_id}/approve", headers=headers)
    assert approve_response.status_code == 403

    post_response = client.post(f"/api/v1/payroll/periods/{period_id}/post", headers=headers)
    assert post_response.status_code == 403

    reverse_response = client.post(
        f"/api/v1/payroll/periods/{period_id}/reverse",
        json={"reason": "test"},
        headers=headers,
    )
    assert reverse_response.status_code == 403


# --- Manager: approve yes, post/reverse no -----------------------------


def test_manager_can_approve_but_not_post_or_reverse_payroll(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="rbac_manager_1", store_id=store.id)
    db.commit()
    headers = auth_headers(client, "rbac_manager_1", DEFAULT_TEST_PASSWORD)

    employee = _hire_via_api(client, headers, store.id, suffix="MGR1")
    comp_response = client.post(
        f"/api/v1/hr/employees/{employee['id']}/compensation",
        json={
            "effective_from": "2024-01-01",
            "pay_type": "SALARY",
            "rate": "2000.00",
            "pay_frequency": "MONTHLY",
        },
        headers=headers,
    )
    assert comp_response.status_code == 200, comp_response.text

    create_response = client.post(
        "/api/v1/payroll/periods",
        json={
            "store_id": store.id,
            "period_start": "2024-01-01",
            "period_end": "2024-01-15",
            "pay_date": "2024-01-20",
        },
        headers=headers,
    )
    period_id = create_response.json()["id"]
    client.post(f"/api/v1/payroll/periods/{period_id}/open", headers=headers)
    calc_response = client.post(
        f"/api/v1/payroll/periods/{period_id}/calculate", json={}, headers=headers
    )
    assert calc_response.status_code == 200, calc_response.text

    approve_response = client.post(f"/api/v1/payroll/periods/{period_id}/approve", headers=headers)
    assert approve_response.status_code == 200, approve_response.text

    post_response = client.post(
        f"/api/v1/payroll/periods/{period_id}/post", json={}, headers=headers
    )
    assert post_response.status_code == 403

    reverse_response = client.post(
        f"/api/v1/payroll/periods/{period_id}/reverse",
        json={"reason": "test"},
        headers=headers,
    )
    assert reverse_response.status_code == 403


# --- Store scoping -------------------------------------------------------


def test_store_scoped_manager_cannot_read_another_stores_employees(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_user_with_role(db, store_a, MANAGER, username="rbac_scope_mgr", store_id=store_a.id)
    db.commit()
    headers = auth_headers(client, "rbac_scope_mgr", DEFAULT_TEST_PASSWORD)

    hire_response = client.post(
        "/api/v1/hr/employees",
        json={
            "employee_number": "EMP-SCOPE1",
            "legal_name": "Store B Employee",
            "hire_date": "2024-01-01",
            "store_id": store_b.id,
        },
        headers=headers,
    )
    assert hire_response.status_code == 403


def test_store_scoped_manager_cannot_read_another_stores_payroll_period(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    make_user_with_role(db, store_a, MANAGER, username="rbac_scope_mgr2", store_id=store_a.id)
    make_user_with_role(db, store_b, MANAGER, username="rbac_scope_mgr3", store_id=store_b.id)
    db.commit()
    headers_a = auth_headers(client, "rbac_scope_mgr2", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "rbac_scope_mgr3", DEFAULT_TEST_PASSWORD)

    create_response = client.post(
        "/api/v1/payroll/periods",
        json={
            "store_id": store_b.id,
            "period_start": "2024-01-01",
            "period_end": "2024-01-15",
            "pay_date": "2024-01-20",
        },
        headers=headers_b,
    )
    period_id = create_response.json()["id"]

    read_response = client.get(f"/api/v1/payroll/periods/{period_id}", headers=headers_a)
    assert read_response.status_code == 403


# --- Auditor: read-only -----------------------------------------------------


def test_auditor_can_read_but_not_write_hr_or_payroll(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, AUDITOR, username="rbac_auditor_1")
    db.commit()
    headers = auth_headers(client, "rbac_auditor_1", DEFAULT_TEST_PASSWORD)

    read_response = client.get("/api/v1/hr/employees", headers=headers)
    assert read_response.status_code == 200

    write_response = client.post(
        "/api/v1/hr/employees",
        json={
            "employee_number": "EMP-AUD1",
            "legal_name": "Should Not Work",
            "hire_date": "2024-01-01",
            "store_id": store.id,
        },
        headers=headers,
    )
    assert write_response.status_code == 403

    payroll_read_response = client.get("/api/v1/payroll/periods", headers=headers)
    assert payroll_read_response.status_code == 200


# --- Audit/privacy: compensation rate never leaks into an error/log --------


def test_invalid_compensation_request_error_never_echoes_the_rate(
    client: TestClient, db: Session
) -> None:
    """M10 approved decision #5: even a validation-failure error response
    must not echo a sensitive amount back in a way that could end up in
    a request/response log at a proxy layer this app doesn't control -
    the amount belongs only in the authorized read response, never in an
    error message."""
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="rbac_privacy_1", store_id=store.id)
    db.commit()
    headers = auth_headers(client, "rbac_privacy_1", DEFAULT_TEST_PASSWORD)
    employee = _hire_via_api(client, headers, store.id, suffix="PRIV1")

    response = client.post(
        f"/api/v1/hr/employees/{employee['id']}/compensation",
        json={
            "effective_from": "2024-01-01",
            "pay_type": "HOURLY",
            "rate": "999999.99",
            "pay_frequency": "NOT_A_REAL_FREQUENCY",
        },
        headers=headers,
    )
    assert response.status_code >= 400
    assert "999999.99" not in response.text
