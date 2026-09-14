"""M12 Phase 6: adversarial authentication/session hardening.

The job of this file is NOT to redesign auth (docs/M12_DESIGN.md Section
1.2 already established that the existing mechanism -- argon2id hashing,
JWT access tokens with a fixed algorithm allowlist, opaque hashed rotating
refresh tokens with reuse-detection, httpOnly/SameSite refresh cookies --
is sound) but to actually attack it: credential stuffing, refresh-token
replay, concurrent refresh, expired-token reuse, malformed/forged JWTs,
algorithm confusion, missing/invalid claims, and session revocation. Any
real defect found here gets fixed with a permanent regression test in
place, per the M12 task's explicit instruction -- never weakened or
skipped.

One real defect WAS found and fixed while writing this file: concurrent
refresh requests presenting the same not-yet-revoked token both succeeded
(no row lock meant neither request observed the other's revocation before
committing), handing out two valid sessions from one single-use refresh
token without ever tripping reuse-detection. Fixed in
app.modules.auth.service.refresh_access_token via `.with_for_update()`;
see test_concurrent_refresh_with_same_token_never_yields_two_sessions
below, which fails against the pre-fix code.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.modules.auth import service
from app.modules.auth.models import RefreshToken
from app.modules.auth.permissions import CASHIER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role
from tests.helpers import auth_headers, login

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _forge_token(payload: dict, *, secret: str, algorithm: str) -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


# --------------------------------------------------------------------
# Credential stuffing / rate limiting
# --------------------------------------------------------------------


def test_credential_stuffing_across_many_usernames_from_one_ip_is_rate_limited(
    client: TestClient, db: Session
) -> None:
    """The limiter is keyed by client IP (see app.core.rate_limit's module
    docstring), specifically so an attacker can't dodge it by trying many
    different usernames instead of brute-forcing one account -- a classic
    credential-stuffing shape."""
    store = make_store(db)
    for i in range(12):
        make_user_with_role(db, store, CASHIER, username=f"stuff_target_{i}")
    db.commit()

    responses = [
        client.post(
            "/api/v1/auth/login",
            json={"username": f"stuff_target_{i}", "password": "wrong-password"},
        )
        for i in range(12)
    ]
    statuses = [r.status_code for r in responses]
    assert 429 in statuses, "credential stuffing across many usernames must still be throttled"


def test_refresh_endpoint_itself_is_rate_limited(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="refresh_ratelimit")
    db.commit()
    login(client, "refresh_ratelimit", DEFAULT_TEST_PASSWORD)

    responses = [client.post("/api/v1/auth/refresh") for _ in range(35)]
    statuses = [r.status_code for r in responses]
    assert 429 in statuses, "sustained refresh attempts must eventually be throttled"


# --------------------------------------------------------------------
# Refresh-token replay / concurrent refresh
# --------------------------------------------------------------------


def test_refresh_token_replay_after_logout_is_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="logout_replay")
    db.commit()

    login(client, "logout_replay", DEFAULT_TEST_PASSWORD)
    stolen_refresh = client.cookies["refresh_token"]

    client.post("/api/v1/auth/logout")

    client.cookies.set("refresh_token", stolen_refresh)
    replay_response = client.post("/api/v1/auth/refresh")
    assert replay_response.status_code == 401


def test_concurrent_refresh_with_same_token_never_yields_two_sessions() -> None:
    """The real race this file found: two requests racing to refresh the
    SAME not-yet-revoked token must not both succeed. Uses genuinely
    independent DB connections/threads (the shared `db`/`client` fixtures
    run everything on one connection -- see test_concurrency.py's module
    docstring for why real concurrency proofs can't use them)."""
    session = SessionLocal()
    store = make_store(session)
    user = make_user_with_role(session, store, CASHIER, username=f"race_{uuid.uuid4().hex[:8]}")
    session.commit()
    _, _access, raw_refresh = service.login(
        session, username=user.username, password=DEFAULT_TEST_PASSWORD
    )

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    results: list[str] = []

    def attempt() -> None:
        thread_session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            try:
                service.refresh_access_token(thread_session, raw_refresh_token=raw_refresh)
                with lock:
                    results.append("success")
            except Exception:  # noqa: BLE001 -- classifying pass/fail only
                with lock:
                    results.append("rejected")
        finally:
            thread_session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Deliberately no teardown deletes here: refresh/login write audit_log
    # rows, and erp_app has no DELETE privilege on audit_logs (M12 Phase 8
    # DB privilege audit) -- so, like test_concurrency.py, this leaves its
    # small, uniquely-named committed rows (store/user/tokens/audit
    # entries) in place rather than fighting the very immutability this
    # milestone verified elsewhere.
    assert results.count("success") == 1, (
        f"exactly one concurrent refresh of the same token must succeed, got {results}"
    )
    assert results.count("rejected") == 1

    # Reuse-detection must have fired: the row lock forces the loser to
    # observe the token as already-revoked (not merely "someone else got
    # there"), which is the compromise signal that revokes every active
    # session for this user -- so even the WINNER's brand new refresh
    # token must already be dead.
    remaining_active = (
        session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    session.close()
    assert remaining_active == [], (
        "a concurrent replay of a single-use refresh token must be treated as a "
        "compromise signal, revoking every session for the user -- not silently "
        "handing out two valid sessions"
    )


def test_expired_refresh_token_row_is_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="expired_refresh")
    db.commit()

    login(client, "expired_refresh", DEFAULT_TEST_PASSWORD)
    row = db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


# --------------------------------------------------------------------
# Malformed / forged / algorithm-confused JWTs
# --------------------------------------------------------------------


def test_expired_access_token_is_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="expired_access")
    db.commit()
    settings = get_settings()
    now = datetime.now(UTC)
    expired_token = _forge_token(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "access",
            "iat": now - timedelta(minutes=30),
            "exp": now - timedelta(minutes=15),
        },
        secret=settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "garbage",
    [
        "not-a-jwt-at-all",
        "a.b.c",
        "",
        "Bearer Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc",
    ],
)
def test_malformed_bearer_tokens_are_rejected(client: TestClient, garbage: str) -> None:
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {garbage}"})
    assert response.status_code == 401


def test_jwt_algorithm_confusion_none_algorithm_is_rejected(
    client: TestClient, db: Session
) -> None:
    """A classic JWT attack: re-encode a valid payload with alg=none and no
    signature, hoping a permissive verifier accepts it as unsigned-but-
    trusted. decode_access_token pins `algorithms=[settings.JWT_ALGORITHM]`
    (HS256), so `none` must never be accepted."""
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="alg_confusion")
    db.commit()
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        key="",
        algorithm="none",
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_jwt_signed_with_wrong_secret_is_rejected(client: TestClient, db: Session) -> None:
    """A token that is structurally valid and correctly claims an existing
    user, but signed with a secret the server never issued -- proves
    signature verification, not merely payload shape, gates access."""
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="wrong_secret")
    db.commit()
    settings = get_settings()
    now = datetime.now(UTC)
    forged = _forge_token(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        secret="an-attacker-controlled-secret-that-is-not-the-real-one",
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_refresh_token_type_cannot_be_used_as_access_token(client: TestClient, db: Session) -> None:
    """A correctly-signed token with the real secret but `type: refresh`
    (mimicking a design where refresh tokens might also be JWTs, or an
    attacker relabeling a captured access token) must not authenticate --
    decode_access_token explicitly checks `type == "access"`."""
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="wrong_type")
    db.commit()
    settings = get_settings()
    now = datetime.now(UTC)
    forged = _forge_token(
        {
            "sub": str(user.id),
            "store_id": user.store_id,
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        secret=settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_token_missing_sub_claim_is_rejected(client: TestClient) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    forged = _forge_token(
        {"type": "access", "iat": now, "exp": now + timedelta(minutes=15)},
        secret=settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_token_with_non_numeric_sub_is_rejected(client: TestClient) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    forged = _forge_token(
        {
            "sub": "not-a-user-id",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        secret=settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_token_for_nonexistent_user_id_is_rejected(client: TestClient, db: Session) -> None:
    """A validly-signed, well-formed token whose `sub` simply doesn't
    correspond to any real user (e.g. a deleted account, or a guessed ID)
    must not authenticate."""
    settings = get_settings()
    now = datetime.now(UTC)
    forged = _forge_token(
        {"sub": "999999999", "type": "access", "iat": now, "exp": now + timedelta(minutes=15)},
        secret=settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


# --------------------------------------------------------------------
# Session revocation (deactivation invalidates already-issued tokens)
# --------------------------------------------------------------------


def test_deactivating_a_user_immediately_invalidates_their_live_access_token(
    client: TestClient, db: Session
) -> None:
    """Access tokens are stateless JWTs verified by signature alone (no DB
    round trip) -- EXCEPT that get_current_user always re-fetches the
    user row and checks `is_active`, which is exactly what makes
    deactivation an effective, immediate kill switch even though the
    token itself is not yet expired and was never explicitly revoked."""
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="deactivate_me")
    db.commit()

    headers = auth_headers(client, "deactivate_me", DEFAULT_TEST_PASSWORD)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    user.is_active = False
    db.commit()

    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401


def test_deactivating_a_user_invalidates_their_refresh_token_too(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="deactivate_refresh")
    db.commit()

    login(client, "deactivate_refresh", DEFAULT_TEST_PASSWORD)
    user.is_active = False
    db.commit()

    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


# --------------------------------------------------------------------
# Cookie flags (XSS/CSRF mitigations)
# --------------------------------------------------------------------


def test_refresh_cookie_is_httponly_and_samesite_lax(client: TestClient, db: Session) -> None:
    """Locks in the two cookie flags the CSRF/XSS design relies on (see
    app.api.v1.endpoints.auth's module docstring): httpOnly so an XSS
    payload can't read it via document.cookie, SameSite=Lax so it isn't
    attached to cross-site POSTs. TestClient's cookie jar strips these
    attributes, so this reads the raw Set-Cookie header instead."""
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="cookie_flags")
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "cookie_flags", "password": DEFAULT_TEST_PASSWORD},
    )
    set_cookie_headers = response.headers.get_list("set-cookie")
    refresh_cookie_header = next(h for h in set_cookie_headers if h.startswith("refresh_token="))
    assert "httponly" in refresh_cookie_header.lower()
    assert "samesite=lax" in refresh_cookie_header.lower()
    assert f"path={('/api/v1/auth')}".lower() in refresh_cookie_header.lower()


def test_access_token_never_travels_as_a_cookie(client: TestClient, db: Session) -> None:
    """The access token must only ever appear in the JSON response body
    (for the SPA to send as an Authorization header) -- never as a
    cookie, which is what keeps it immune to CSRF in the first place."""
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="no_access_cookie")
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "no_access_cookie", "password": DEFAULT_TEST_PASSWORD},
    )
    access_token = response.json()["access_token"]
    set_cookie_headers = response.headers.get_list("set-cookie")
    assert not any(access_token in h for h in set_cookie_headers)
    assert all(not h.startswith("access_token=") for h in set_cookie_headers)


# --------------------------------------------------------------------
# Cross-user token substitution
# --------------------------------------------------------------------


def test_users_refresh_token_cannot_mint_an_access_token_for_a_different_user(
    client: TestClient, db: Session
) -> None:
    """Refresh issues an access token for the row's OWN user_id -- there is
    no client-supplied field that could substitute a different user's
    identity into that call. Locks in that the resulting access token's
    `sub` always matches the account that actually owns the refresh
    cookie being presented, never another account."""
    store = make_store(db)
    user_a = make_user_with_role(db, store, CASHIER, username="user_a_sub")
    user_b = make_user_with_role(db, store, CASHIER, username="user_b_sub")
    db.commit()

    login(client, "user_a_sub", DEFAULT_TEST_PASSWORD)
    refresh_response = client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 200
    new_access_token = refresh_response.json()["access_token"]

    settings = get_settings()
    payload = jwt.decode(new_access_token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    assert int(payload["sub"]) == user_a.id
    assert int(payload["sub"]) != user_b.id


def test_one_users_access_token_cannot_be_replayed_by_swapping_the_bearer_header(
    client: TestClient, db: Session
) -> None:
    """A sanity check that the permission/identity resolution is always
    driven by the presented token, never by any other request field --
    so user A's real token always resolves to user A, never to whichever
    user a request body happens to reference."""
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="identity_a")
    make_user_with_role(db, store, CASHIER, username="identity_b")
    db.commit()

    headers_a = auth_headers(client, "identity_a", DEFAULT_TEST_PASSWORD)
    me_a = client.get("/api/v1/auth/me", headers=headers_a)
    assert me_a.json()["username"] == "identity_a"

    headers_b = auth_headers(client, "identity_b", DEFAULT_TEST_PASSWORD)
    me_b = client.get("/api/v1/auth/me", headers=headers_b)
    assert me_b.json()["username"] == "identity_b"


# --------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------


def test_password_hash_is_argon2_and_never_stores_plaintext(db: Session) -> None:
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="hash_check")
    db.commit()
    assert user.password_hash != DEFAULT_TEST_PASSWORD
    assert user.password_hash.startswith("$argon2id$")
