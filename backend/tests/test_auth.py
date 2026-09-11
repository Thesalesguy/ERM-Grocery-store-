"""Authentication: login, protected routes, RBAC denial, and the
refresh/logout token lifecycle."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, CASHIER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role
from tests.helpers import auth_headers, login


def test_successful_login_returns_access_token(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier1")
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "cashier1", "password": DEFAULT_TEST_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20
    # Refresh token travels as an httpOnly cookie, not in the JSON body.
    assert "refresh_token" in response.cookies


def test_login_with_wrong_password_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier2")
    db.commit()

    response = client.post(
        "/api/v1/auth/login", json={"username": "cashier2", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_login_with_unknown_username_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": "no-such-user", "password": "irrelevant"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_inactive_user_cannot_log_in(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier_inactive", is_active=False)
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "cashier_inactive", "password": DEFAULT_TEST_PASSWORD},
    )
    assert response.status_code == 401


def test_protected_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/products")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_protected_endpoint_with_garbage_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/products", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert response.status_code == 401


def test_permission_denial_returns_403(client: TestClient, db: Session) -> None:
    """A Cashier has pos.use but not products.write."""
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier3")
    db.commit()

    headers = auth_headers(client, "cashier3", DEFAULT_TEST_PASSWORD)
    response = client.post(
        "/api/v1/products",
        headers=headers,
        json={"store_id": store.id, "sku": "X", "name": "X", "current_price": "1.00"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_admin_can_access_write_endpoints(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, ADMIN, username="admin1")
    db.commit()

    headers = auth_headers(client, "admin1", DEFAULT_TEST_PASSWORD)
    response = client.post(
        "/api/v1/products",
        headers=headers,
        json={
            "store_id": store.id,
            "sku": "ADMIN-SKU",
            "name": "Admin Product",
            "current_price": "5.00",
        },
    )
    assert response.status_code == 201


def test_refresh_token_rotates_and_issues_new_access_token(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier4")
    db.commit()

    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": "cashier4", "password": DEFAULT_TEST_PASSWORD},
    )
    old_refresh_cookie = login_response.cookies["refresh_token"]

    refresh_response = client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 200
    new_access_token = refresh_response.json()["access_token"]
    # The new access token is genuinely valid (not just present).
    me_response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access_token}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["username"] == "cashier4"
    # The rotated-out refresh token must no longer work.
    client.cookies.set("refresh_token", old_refresh_cookie)
    replay_response = client.post("/api/v1/auth/refresh")
    assert replay_response.status_code == 401


def test_logout_revokes_refresh_token(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cashier5")
    db.commit()

    login(client, "cashier5", DEFAULT_TEST_PASSWORD)
    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    refresh_response = client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 401
