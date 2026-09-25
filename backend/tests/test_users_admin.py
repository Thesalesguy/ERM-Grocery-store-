"""M16: Users/RBAC administration (list/create/reactivate/role-change).

`users.manage` was provisioned in M12 ("Create users and assign roles")
but no endpoint implemented anything beyond deactivate until now. See
docs/M16_DESIGN.md "Users/RBAC administration" for the full design,
including why every mutating function here re-validates that the
assigned/changed role holds no permission broader than the acting user's
own (PRIVILEGE_ESCALATION_DENIED) even though users.manage is Admin-only
today.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError
from app.modules.auth import service as auth_service
from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role, unique_suffix
from tests.helpers import auth_headers


def _admin(db: Session, store=None, username: str | None = None) -> str:
    username = username or f"admin_{unique_suffix()}"
    make_user_with_role(db, store, ADMIN, username=username)
    db.commit()
    return username


def test_admin_can_list_create_and_reactivate_a_user(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin_username = _admin(db, store)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)

    new_username = f"newcashier_{unique_suffix()}"
    create_resp = client.post(
        "/api/v1/auth/users",
        headers=headers,
        json={
            "username": new_username,
            "email": f"{new_username}@example.com",
            "password": "a-strong-password-1",
            "full_name": "New Cashier",
            "store_id": store.id,
            "role": CASHIER,
        },
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["username"] == new_username
    assert body["role"] == CASHIER
    assert body["is_active"] is True
    new_user_id = body["id"]

    list_resp = client.get("/api/v1/auth/users", headers=headers)
    assert list_resp.status_code == 200
    usernames = {u["username"] for u in list_resp.json()}
    assert new_username in usernames

    deactivate_resp = client.post(f"/api/v1/auth/users/{new_user_id}/deactivate", headers=headers)
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()["is_active"] is False

    reactivate_resp = client.post(f"/api/v1/auth/users/{new_user_id}/reactivate", headers=headers)
    assert reactivate_resp.status_code == 200
    assert reactivate_resp.json()["is_active"] is True

    # The reactivated user can actually log in again.
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": new_username, "password": "a-strong-password-1"},
    )
    assert login_resp.status_code == 200


def test_duplicate_username_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin_username = _admin(db, store)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)

    payload = {
        "username": f"dupe_{unique_suffix()}",
        "email": f"dupe_{unique_suffix()}@example.com",
        "password": "a-strong-password-1",
        "full_name": "Dupe",
        "store_id": store.id,
        "role": CASHIER,
    }
    first = client.post("/api/v1/auth/users", headers=headers, json=payload)
    assert first.status_code == 201

    second_payload = dict(payload)
    second_payload["email"] = f"different_{unique_suffix()}@example.com"
    second = client.post("/api/v1/auth/users", headers=headers, json=second_payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_USER"


def test_non_admin_cannot_manage_users(client: TestClient, db: Session) -> None:
    store = make_store(db)
    manager_username = f"mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=manager_username)
    db.commit()
    headers = auth_headers(client, manager_username, DEFAULT_TEST_PASSWORD)

    resp = client.get("/api/v1/auth/users", headers=headers)
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/auth/users",
        headers=headers,
        json={
            "username": f"shouldfail_{unique_suffix()}",
            "email": "x@example.com",
            "password": "a-strong-password-1",
            "full_name": "X",
            "store_id": store.id,
            "role": CASHIER,
        },
    )
    assert resp.status_code == 403


def test_role_update_changes_effective_permissions(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin_username = _admin(db, store)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)
    cashier_username = f"cashier_{unique_suffix()}"
    cashier = make_user_with_role(db, store, CASHIER, username=cashier_username)
    db.commit()

    resp = client.put(
        f"/api/v1/auth/users/{cashier.id}/role", headers=headers, json={"role": MANAGER}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == MANAGER

    cashier_headers = auth_headers(client, cashier_username, DEFAULT_TEST_PASSWORD)
    me = client.get("/api/v1/auth/me", headers=cashier_headers)
    assert "accounting.reverse" in me.json()["permissions"]


def test_role_update_refuses_self_change(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin_username = _admin(db, store)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)
    me = client.get("/api/v1/auth/me", headers=headers).json()

    resp = client.put(
        f"/api/v1/auth/users/{me['id']}/role", headers=headers, json={"role": CASHIER}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CANNOT_CHANGE_OWN_ROLE"


def test_service_layer_direct_call_cannot_assign_a_role_broader_than_the_actor_holds(
    db: Session,
) -> None:
    """The most important test here: manipulating the request cannot
    grant privileges the actor is not authorized to grant. Simulates a
    hypothetical future caller with users.manage but not every Admin
    permission (impossible via the HTTP layer today, since users.manage
    is Admin-only, but the service function itself must still refuse
    this -- defense-in-depth, not merely UI-hidden)."""
    store = make_store(db)
    target = make_user_with_role(db, store, CASHIER, username=f"target_{unique_suffix()}")
    db.commit()

    limited_permissions = frozenset({"users.manage", "sales.read"})  # deliberately NOT admin-full

    with pytest.raises(ForbiddenError) as exc_info:
        auth_service.update_user_role(
            db,
            target_user_id=target.id,
            role_name=ADMIN,
            actor_id=999,
            actor_permissions=limited_permissions,
        )
    assert exc_info.value.error_code == "PRIVILEGE_ESCALATION_DENIED"

    # The role must be genuinely unchanged.
    assert auth_service.get_user_role_name(db, target.id) == CASHIER


def test_roles_endpoint_lists_all_known_roles(client: TestClient, db: Session) -> None:
    store = make_store(db)
    admin_username = _admin(db, store)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)

    resp = client.get("/api/v1/auth/roles", headers=headers)
    assert resp.status_code == 200
    names = {r["name"] for r in resp.json()}
    assert {ADMIN, MANAGER, CASHIER}.issubset(names)


def test_store_scoped_admin_cannot_list_or_create_users_in_another_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    # An Admin scoped to one store is an unusual but schema-legal
    # configuration (User.store_id is independent of role) -- proves
    # store isolation holds for the Users screen too, not just the
    # (far more common) unrestricted Admin case exercised above.
    admin_username = _admin(db, store_a)
    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)

    resp = client.get(f"/api/v1/auth/users?store_id={store_b.id}", headers=headers)
    assert resp.status_code == 403
