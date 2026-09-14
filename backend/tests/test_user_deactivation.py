"""M12 Phase 10: the one operational administration capability this
milestone adds -- an operator-facing way to deactivate a terminated or
compromised user's account without shell/DB access (docs/M12_DESIGN.md
Section 1.6). Deliberately narrow: no reactivation, no user creation, no
generic admin UI -- see that section for why this is the one capability
M12 adds and nothing more.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.models import RefreshToken
from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role
from tests.helpers import auth_headers, login


def test_admin_can_deactivate_a_user(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="deactivate_admin")
    target = make_user_with_role(db, store, CASHIER, username="deactivate_target")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin", DEFAULT_TEST_PASSWORD)
    response = client.post(f"/api/v1/auth/users/{target.id}/deactivate", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == target.id
    assert body["is_active"] is False

    db.refresh(target)
    assert target.is_active is False


def test_deactivated_user_cannot_log_in(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="deactivate_admin2")
    make_user_with_role(db, store, CASHIER, username="deactivate_login_target")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin2", DEFAULT_TEST_PASSWORD)
    target_login = login(client, "deactivate_login_target", DEFAULT_TEST_PASSWORD)
    target_id = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {target_login}"}
    ).json()["id"]

    client.post(f"/api/v1/auth/users/{target_id}/deactivate", headers=admin_headers)

    relogin = client.post(
        "/api/v1/auth/login",
        json={"username": "deactivate_login_target", "password": DEFAULT_TEST_PASSWORD},
    )
    assert relogin.status_code == 401


def test_deactivation_immediately_kills_an_already_issued_access_token(
    client: TestClient, db: Session
) -> None:
    """The whole point: revocation must be effective on the very next
    request, not merely block future logins -- a compromise response
    that leaves a live 15-minute access token usable isn't a real
    response."""
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="deactivate_admin3")
    make_user_with_role(db, store, CASHIER, username="deactivate_live_target")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin3", DEFAULT_TEST_PASSWORD)
    target_headers = auth_headers(client, "deactivate_live_target", DEFAULT_TEST_PASSWORD)
    target_id = client.get("/api/v1/auth/me", headers=target_headers).json()["id"]
    assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 200

    client.post(f"/api/v1/auth/users/{target_id}/deactivate", headers=admin_headers)

    still_using_old_token = client.get("/api/v1/auth/me", headers=target_headers)
    assert still_using_old_token.status_code == 401


def test_deactivation_revokes_every_refresh_token_the_user_holds(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="deactivate_admin4")
    target = make_user_with_role(db, store, CASHIER, username="deactivate_refresh_target")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin4", DEFAULT_TEST_PASSWORD)
    login(client, "deactivate_refresh_target", DEFAULT_TEST_PASSWORD)
    refresh_cookie = client.cookies["refresh_token"]

    client.post(f"/api/v1/auth/users/{target.id}/deactivate", headers=admin_headers)

    active_tokens = (
        db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == target.id, RefreshToken.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    assert active_tokens == []

    client.cookies.set("refresh_token", refresh_cookie)
    refresh_response = client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 401


def test_deactivation_is_recorded_in_the_audit_log(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin = make_user_with_role(db, store, ADMIN, username="deactivate_admin5")
    target = make_user_with_role(db, store, CASHIER, username="deactivate_audit_target")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin5", DEFAULT_TEST_PASSWORD)
    client.post(f"/api/v1/auth/users/{target.id}/deactivate", headers=admin_headers)

    event = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.action == "USER_DEACTIVATED", AuditLog.entity_id == target.id)
            .order_by(AuditLog.id.desc())
        )
        .scalars()
        .first()
    )
    assert event is not None
    assert event.user_id == admin.id
    assert event.before_state == {"is_active": True}
    assert event.after_state == {"is_active": False}


def test_admin_cannot_deactivate_their_own_account(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin = make_user_with_role(db, store, ADMIN, username="deactivate_self_admin")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_self_admin", DEFAULT_TEST_PASSWORD)
    response = client.post(f"/api/v1/auth/users/{admin.id}/deactivate", headers=admin_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANNOT_DEACTIVATE_SELF"

    db.refresh(admin)
    assert admin.is_active is True


def test_deactivating_a_nonexistent_user_returns_404(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="deactivate_admin6")
    db.commit()

    admin_headers = auth_headers(client, "deactivate_admin6", DEFAULT_TEST_PASSWORD)
    response = client.post("/api/v1/auth/users/999999999/deactivate", headers=admin_headers)
    assert response.status_code == 404


def test_a_user_without_users_manage_permission_cannot_deactivate_anyone(
    client: TestClient, db: Session
) -> None:
    """A Manager (no users.manage in the M2 permission matrix) and a
    Cashier must both be refused -- this is an Admin-only capability."""
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="deactivate_manager")
    target = make_user_with_role(db, store, CASHIER, username="deactivate_denied_target")
    db.commit()

    manager_headers = auth_headers(client, "deactivate_manager", DEFAULT_TEST_PASSWORD)
    response = client.post(f"/api/v1/auth/users/{target.id}/deactivate", headers=manager_headers)
    assert response.status_code == 403

    db.refresh(target)
    assert target.is_active is True


def test_deactivation_requires_authentication(client: TestClient, db: Session) -> None:
    store = make_store(db)
    target = make_user_with_role(db, store, CASHIER, username="deactivate_no_auth_target")
    db.commit()

    response = client.post(f"/api/v1/auth/users/{target.id}/deactivate")
    assert response.status_code == 401
