"""Authentication business logic and the FastAPI dependencies routes use
to require an authenticated user (`get_current_user`) or a specific
permission (`require_permission`).

Route handlers never touch passwords, tokens, or the permission-resolution
query directly — they depend on these functions, matching the same
model/schema/service/route separation established in M1.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.modules.audit import service as audit_service
from app.modules.auth.models import Permission, RefreshToken, RolePermission, User, UserRole

# A real argon2 hash of an unguessable, never-used password. Verifying
# against this when no matching user exists keeps the login endpoint's
# response time roughly constant regardless of whether the username is
# real, so timing can't be used to enumerate valid usernames.
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-account-timing-safety-only")


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    full_name: str
    store_id: int | None
    permissions: frozenset[str]

    def has_permission(self, code: str) -> bool:
        return code in self.permissions


def get_user_permissions(db: Session, user_id: int) -> frozenset[str]:
    codes = db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
    ).scalars()
    return frozenset(codes)


def authenticate_user(db: Session, username: str, password: str) -> User:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    hash_to_check = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)
    if user is None or not user.is_active or not password_ok:
        # Committed immediately (not left for a caller's transaction to
        # commit) since the caller is about to raise and unwind — an
        # uncommitted audit row would be rolled back with it.
        audit_service.log_event(
            db,
            user_id=user.id if user is not None else None,
            action="LOGIN_FAILURE",
            entity_type="user",
            entity_id=user.id if user is not None else None,
            after={"username": username},
        )
        db.commit()
        raise UnauthorizedError("Invalid username or password", error_code="INVALID_CREDENTIALS")
    return user


def login(
    db: Session,
    *,
    username: str,
    password: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str]:
    """Returns (user, access_token, raw_refresh_token). Commits — login is
    always a standalone top-level operation, never composed as one step of
    a larger transaction, so there is no atomicity reason to defer the
    commit to a caller (contrast app.modules.sales.service.finalize_sale)."""
    user = authenticate_user(db, username, password)
    access_token = create_access_token(user_id=user.id, store_id=user.store_id)
    raw_refresh, token_hash = generate_refresh_token()
    settings = get_settings()
    now = datetime.now(UTC)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    user.last_login_at = now
    audit_service.log_event(
        db,
        user_id=user.id,
        action="LOGIN_SUCCESS",
        entity_type="user",
        entity_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.commit()
    return user, access_token, raw_refresh


def refresh_access_token(
    db: Session,
    *,
    raw_refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str]:
    """Validates and rotates a refresh token: the old one is revoked and a
    new one issued in the same call, so a stolen-and-replayed old token
    stops working the moment the legitimate client refreshes."""
    token_hash = hash_refresh_token(raw_refresh_token)
    row = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at < now:
        raise UnauthorizedError("Invalid or expired refresh token")

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid or expired refresh token")

    row.revoked_at = now
    access_token = create_access_token(user_id=user.id, store_id=user.store_id)
    raw_new, new_hash = generate_refresh_token()
    settings = get_settings()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    db.commit()
    return user, access_token, raw_new


def logout(db: Session, *, raw_refresh_token: str) -> None:
    """Idempotent: revoking an already-revoked/unknown token is a no-op,
    not an error — logout should never leak whether a token was valid."""
    token_hash = hash_refresh_token(raw_refresh_token)
    row = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()


_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """The core "who is calling" dependency. Raises UnauthorizedError (401)
    for anything wrong with the token — missing, malformed, expired,
    wrong type, or referencing a user that no longer exists/is inactive —
    without distinguishing which, so a caller can't probe token internals.
    """
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError("Invalid token payload") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid or expired token")

    permissions = get_user_permissions(db, user.id)
    return CurrentUser(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        store_id=user.store_id,
        permissions=permissions,
    )


def require_permission(permission_code: str) -> Callable[[CurrentUser], CurrentUser]:
    """Dependency factory: `Depends(require_permission(PRODUCTS_WRITE))`.
    Requires authentication first (via get_current_user), then checks the
    resolved permission set — a route never checks roles/permissions
    inline, keeping every protected route's authorization declared in its
    signature rather than scattered through its body."""

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not current_user.has_permission(permission_code):
            raise ForbiddenError(f"Missing required permission: {permission_code}")
        return current_user

    return dependency
