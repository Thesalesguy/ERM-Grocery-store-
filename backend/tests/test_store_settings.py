"""M16: store settings (Settings screen).

`store.settings.read`/`store.settings.write` previously had no API
surface at all -- return_approval_threshold_amount (M14) and
attendance_day_boundary_hour (M10) existed only as columns with no way
to view or change them without shell/DB access. See docs/M16_DESIGN.md
"Settings screen" for the full design, including why `is_active` is
deliberately not exposed as editable here.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import AUDITOR, CASHIER, MANAGER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role, unique_suffix
from tests.helpers import auth_headers


def _manager(db: Session, store) -> str:
    username = f"mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    return username


def test_manager_can_read_and_update_settings(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    read_resp = client.get(f"/api/v1/stores/{store.id}/settings", headers=headers)
    assert read_resp.status_code == 200
    body = read_resp.json()
    assert body["attendance_day_boundary_hour"] == 0
    assert body["return_approval_threshold_amount"] is None

    update_resp = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={
            "attendance_day_boundary_hour": 6,
            "return_approval_threshold_amount": "50.00",
        },
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["attendance_day_boundary_hour"] == 6
    assert updated["return_approval_threshold_amount"] == "50.00"

    # Persisted, not just returned in the response.
    reread = client.get(f"/api/v1/stores/{store.id}/settings", headers=headers)
    assert reread.json()["attendance_day_boundary_hour"] == 6
    assert reread.json()["return_approval_threshold_amount"] == "50.00"


def test_clear_return_approval_threshold(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"return_approval_threshold_amount": "100.00"},
    )
    clear_resp = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"clear_return_approval_threshold": True},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["return_approval_threshold_amount"] is None


def test_negative_threshold_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"return_approval_threshold_amount": "-5.00"},
    )
    assert resp.status_code == 422


def test_out_of_range_attendance_hour_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"attendance_day_boundary_hour": 24},
    )
    assert resp.status_code == 422


def test_cashier_cannot_write_settings_but_auditor_can_read(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    cashier_username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=cashier_username)
    auditor_username = f"auditor_{unique_suffix()}"
    make_user_with_role(db, store, AUDITOR, username=auditor_username)
    db.commit()

    cashier_headers = auth_headers(client, cashier_username, DEFAULT_TEST_PASSWORD)
    resp = client.get(f"/api/v1/stores/{store.id}/settings", headers=cashier_headers)
    assert resp.status_code == 403
    resp = client.put(f"/api/v1/stores/{store.id}/settings", headers=cashier_headers, json={})
    assert resp.status_code == 403

    auditor_headers = auth_headers(client, auditor_username, DEFAULT_TEST_PASSWORD)
    resp = client.get(f"/api/v1/stores/{store.id}/settings", headers=auditor_headers)
    assert resp.status_code == 200
    resp = client.put(f"/api/v1/stores/{store.id}/settings", headers=auditor_headers, json={})
    assert resp.status_code == 403


def test_cross_store_manager_cannot_view_or_change_another_stores_settings(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = _manager(db, store_a)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.get(f"/api/v1/stores/{store_b.id}/settings", headers=headers)
    assert resp.status_code == 404

    resp = client.put(
        f"/api/v1/stores/{store_b.id}/settings",
        headers=headers,
        json={"attendance_day_boundary_hour": 5},
    )
    assert resp.status_code == 403


def test_is_active_is_not_editable_via_settings(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"is_active": False, "name": "Still Active Store"},
    )
    assert resp.status_code == 200
    # is_active was silently ignored (schema has no such field) -- still
    # active, confirming it cannot be flipped via this endpoint.
    assert resp.json()["is_active"] is True
    assert resp.json()["name"] == "Still Active Store"


def test_audit_log_records_settings_change(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _manager(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={"attendance_day_boundary_hour": 8},
    )

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "STORE_SETTINGS_UPDATED", AuditLog.entity_id == store.id
        )
    ).scalar_one()
    assert audit_row.after_state["attendance_day_boundary_hour"] == 8
