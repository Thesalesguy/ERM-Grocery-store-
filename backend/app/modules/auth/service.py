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
from decimal import Decimal

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationAppError,
)
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.modules.audit import service as audit_service
from app.modules.auth.models import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    Store,
    User,
    UserRole,
)
from app.modules.auth.permissions import ALL_ROLES, ROLE_PERMISSIONS

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


def verify_user_credentials(db: Session, username: str, password: str) -> User | None:
    """A non-raising sibling of `authenticate_user`, for inline
    identity-verification INSIDE another operation's own transaction
    (M14, app.modules.sales.service's return/void approval gate) rather
    than as a standalone login.

    Deliberately NOT a thin wrapper around `authenticate_user`: that
    function commits immediately and logs a `LOGIN_FAILURE` audit event
    on failure — correct for an actual login attempt, but both would be
    wrong here. A premature commit here would commit whatever the
    caller's own in-progress transaction had already flushed (nothing,
    if callers check approval before their first `db.add`, as
    create_sale_return does — but this function must not assume that of
    every future caller). And "LOGIN_FAILURE" would mislabel a failed
    approval attempt as a failed login in the audit trail. Callers own
    their own audit logging and commit timing; this function only
    verifies and returns.

    Reuses `verify_password` and the same dummy-hash timing-safety idiom
    as `authenticate_user` (a nonexistent/inactive username must take the
    same time as a wrong password for a real one), so this new
    credential-check surface has the identical timing characteristics as
    login rather than a weaker, hand-rolled comparison.
    """
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    hash_to_check = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)
    if user is None or not user.is_active or not password_ok:
        return None
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


def _revoke_all_refresh_tokens_for_user(db: Session, user_id: int) -> None:
    now = datetime.now(UTC)
    active_tokens = db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    ).scalars()
    for token in active_tokens:
        token.revoked_at = now


def refresh_access_token(
    db: Session,
    *,
    raw_refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str]:
    """Validates and rotates a refresh token: the old one is revoked and a
    new one issued in the same call, so a stolen-and-replayed old token
    stops working the moment the legitimate client refreshes.

    Reuse-detection response (M2 hardening audit Section 9 — previously an
    accepted gap, closed here): presenting a token that is specifically
    ALREADY REVOKED (as opposed to merely unknown or expired) means either
    two different parties possess what was supposed to be one single-use
    token — the legitimate client already rotated past it, and this is a
    replay of the stale value, which is exactly what a stolen-and-copied
    refresh token being used after the real client refreshed looks like —
    or, more mundanely, a logged-out session retrying. Either way, the
    safe response is the same: treat it as a compromise signal and revoke
    every OTHER still-active token for that user, forcing every session
    to re-authenticate, and record the event in the audit log. A merely
    unknown or expired token (never issued, or aged out) gets the same
    generic 401 as before — nothing here changes what an attacker
    observes in the response, only what the server does about it.

    `.with_for_update()` (M12 Phase 6 hardening audit — previously an
    undetected defect, closed here): without a row lock, two requests
    presenting the SAME not-yet-revoked token concurrently (a legitimate
    client racing an attacker's stolen copy, or a double-submit) both read
    `revoked_at IS NULL` before either commits, and both proceed to mark
    it revoked and mint a brand-new refresh token — handing out two valid
    sessions from one single-use token with reuse-detection never firing,
    since neither request ever observed the row as already revoked. The
    lock forces the second transaction to block until the first commits,
    then re-read the now-revoked row and correctly take the reuse-detected
    branch below, exactly as if the second request had arrived a moment
    later instead of at the same instant.
    """
    token_hash = hash_refresh_token(raw_refresh_token)
    row = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    ).scalar_one_or_none()
    now = datetime.now(UTC)

    if row is not None and row.revoked_at is not None:
        _revoke_all_refresh_tokens_for_user(db, row.user_id)
        audit_service.log_event(
            db,
            user_id=row.user_id,
            action="REFRESH_TOKEN_REUSE_DETECTED",
            entity_type="user",
            entity_id=row.user_id,
            after={"revoked_all_sessions": True},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        raise UnauthorizedError("Invalid or expired refresh token")

    if row is None or row.expires_at < now:
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


def deactivate_user(
    db: Session,
    *,
    target_user_id: int,
    actor_id: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> User:
    """M12 Phase 10 (docs/M12_DESIGN.md Section 1.6): the one
    operator-facing administrative capability this milestone adds —
    revoking a terminated or compromised account's access without shell/
    DB access. Sets `users.is_active = False` (already checked at login
    and on every token-refresh/get_current_user call) and immediately
    revokes every refresh token the account holds, so a session that is
    mid-way through its 15-minute access-token lifetime is not merely
    prevented from logging in again — get_current_user's own is_active
    check kills it on the very next request, and the refresh revocation
    stops it from quietly renewing past that point (M12 Phase 6/9:
    session revocation must be immediate, not "eventually", for a
    compromise response to mean anything).

    Self-deactivation is refused: the actor locking out their OWN account
    is exactly the scenario that would force a return to the shell/DB
    access this capability exists to avoid (there being no reactivation
    capability in this milestone — docs/M12_DESIGN.md Section 20 — a
    lone admin deactivating themselves would have no way back in through
    the API at all).
    """
    if target_user_id == actor_id:
        raise ConflictError(
            "You cannot deactivate your own account.", error_code="CANNOT_DEACTIVATE_SELF"
        )
    user = db.get(User, target_user_id)
    if user is None:
        raise NotFoundError(f"User {target_user_id} not found")
    was_active = user.is_active
    user.is_active = False
    _revoke_all_refresh_tokens_for_user(db, target_user_id)
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="USER_DEACTIVATED",
        entity_type="user",
        entity_id=target_user_id,
        before={"is_active": was_active},
        after={"is_active": False},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.commit()
    return user


# --- M16: user administration (Users/RBAC screen) --------------------------
#
# `users.manage` was provisioned in M12 ("Create users and assign roles")
# but no endpoint implemented list/create/reactivate/role-assignment until
# now — deactivate_user above was M12's one operator-facing capability.
# Every mutating function here re-validates that the assigned/changed
# role holds no permission the ACTING user doesn't themselves hold
# (PRIVILEGE_ESCALATION_DENIED) — users.manage is Admin-only today (no
# other role is granted it), so this is defense-in-depth rather than a
# currently-reachable gap, but it makes the check meaningful the moment
# any future milestone ever grants users.manage more broadly, exactly
# the same discipline M14/M15/M16's own reversal/override checks follow.


def list_users(db: Session, *, store_id: int | None = None) -> list[User]:
    query = select(User).order_by(User.username)
    if store_id is not None:
        query = query.where(User.store_id == store_id)
    return list(db.execute(query).scalars().all())


def get_user_role_name(db: Session, user_id: int) -> str | None:
    return db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).scalar_one_or_none()


def _validate_role_within_actor_permissions(
    role_name: str, actor_permissions: frozenset[str]
) -> None:
    if role_name not in ALL_ROLES:
        raise ValidationAppError(f"Unknown role {role_name!r}", error_code="INVALID_ROLE")
    role_permission_set = frozenset(ROLE_PERMISSIONS.get(role_name, []))
    if not role_permission_set.issubset(actor_permissions):
        raise ForbiddenError(
            f"Cannot assign role {role_name!r}: it holds a permission you do not have",
            error_code="PRIVILEGE_ESCALATION_DENIED",
        )


def create_user(
    db: Session,
    *,
    username: str,
    email: str,
    password: str,
    full_name: str,
    store_id: int | None,
    role_name: str,
    actor_id: int,
    actor_permissions: frozenset[str],
) -> User:
    _validate_role_within_actor_permissions(role_name, actor_permissions)
    user = User(
        store_id=store_id,
        username=username,
        email=email,
        password_hash=hash_password(password),
        full_name=full_name,
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Username {username!r} or email {email!r} is already in use",
            error_code="DUPLICATE_USER",
        ) from exc

    role = db.execute(select(Role).where(Role.name == role_name)).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="USER_CREATED",
        entity_type="user",
        entity_id=user.id,
        after={"username": username, "role": role_name, "store_id": store_id},
    )
    db.commit()
    db.refresh(user)
    return user


def reactivate_user(db: Session, *, target_user_id: int, actor_id: int) -> User:
    """The counterpart deactivate_user's own docstring named as missing
    from M12 ("there being no reactivation capability in this
    milestone")."""
    user = db.get(User, target_user_id)
    if user is None:
        raise NotFoundError(f"User {target_user_id} not found")
    was_active = user.is_active
    user.is_active = True
    audit_service.log_event(
        db,
        user_id=actor_id,
        action="USER_REACTIVATED",
        entity_type="user",
        entity_id=target_user_id,
        before={"is_active": was_active},
        after={"is_active": True},
    )
    db.commit()
    return user


def update_user_role(
    db: Session,
    *,
    target_user_id: int,
    role_name: str,
    actor_id: int,
    actor_permissions: frozenset[str],
) -> User:
    """Self-role-change is refused (mirrors deactivate_user's own
    self-deactivation refusal exactly): a lone Admin downgrading their
    own role would have no way back in through the API at all."""
    if target_user_id == actor_id:
        raise ConflictError("You cannot change your own role.", error_code="CANNOT_CHANGE_OWN_ROLE")
    _validate_role_within_actor_permissions(role_name, actor_permissions)
    user = db.get(User, target_user_id)
    if user is None:
        raise NotFoundError(f"User {target_user_id} not found")
    role = db.execute(select(Role).where(Role.name == role_name)).scalar_one()

    previous_role_name = get_user_role_name(db, target_user_id)
    db.execute(delete(UserRole).where(UserRole.user_id == target_user_id))
    db.add(UserRole(user_id=target_user_id, role_id=role.id))

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="USER_ROLE_CHANGED",
        entity_type="user",
        entity_id=target_user_id,
        before={"role": previous_role_name},
        after={"role": role_name},
    )
    db.commit()
    db.refresh(user)
    return user


# --- M16: store settings (Settings screen) ----------------------------
#
# Exposes exactly the Store columns that already existed with documented
# semantics before this milestone (attendance_day_boundary_hour, M10;
# return_approval_threshold_amount, M14) plus the store's basic identity
# fields (name/address/timezone) -- nothing invented. `is_active` is
# deliberately NOT editable here: deactivating a store is a far more
# consequential operation than a settings change (no evidence anywhere
# in this repository of what should cascade from it), so it stays
# read-only via this surface.


def get_store(db: Session, store_id: int) -> Store:
    store = db.get(Store, store_id)
    if store is None:
        raise NotFoundError(f"Store {store_id} not found")
    return store


def update_store_settings(
    db: Session,
    *,
    store_id: int,
    name: str | None = None,
    address: str | None = None,
    timezone: str | None = None,
    attendance_day_boundary_hour: int | None = None,
    return_approval_threshold_amount: Decimal | None = None,
    clear_return_approval_threshold: bool = False,
    actor_id: int,
    caller_store_id: int | None,
) -> Store:
    """Only the fields explicitly passed are changed. `None` is itself a
    MEANINGFUL value for `return_approval_threshold_amount` (clears the
    threshold, disabling M14's approval gate for this store) -- since a
    plain `None` default can't distinguish "not passed" from "explicitly
    clear it", `clear_return_approval_threshold` disambiguates: only when
    it is True does a `None`/omitted `return_approval_threshold_amount`
    actually clear the column; otherwise omitting it leaves the existing
    threshold untouched.

    `caller_store_id` mirrors every other module's own duplicated
    `_enforce_store_access(caller_store_id, target_store_id, noun)`
    service-layer check (raw ids, not a `CurrentUser` -- this module's
    own `enforce_store_access` above takes the latter and is an
    endpoint-layer helper, not meant for direct service-layer reuse, per
    that function's own docstring)."""
    if caller_store_id is not None and caller_store_id != store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {caller_store_id} and cannot access "
            f"this store's settings in store {store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    store = get_store(db, store_id)

    before = {
        "name": store.name,
        "address": store.address,
        "timezone": store.timezone,
        "attendance_day_boundary_hour": store.attendance_day_boundary_hour,
        "return_approval_threshold_amount": (
            str(store.return_approval_threshold_amount)
            if store.return_approval_threshold_amount is not None
            else None
        ),
    }

    if name is not None:
        store.name = name
    if address is not None:
        store.address = address
    if timezone is not None:
        store.timezone = timezone
    if attendance_day_boundary_hour is not None:
        if not 0 <= attendance_day_boundary_hour <= 23:
            raise ValidationAppError(
                "attendance_day_boundary_hour must be between 0 and 23",
                error_code="INVALID_ATTENDANCE_DAY_BOUNDARY_HOUR",
            )
        store.attendance_day_boundary_hour = attendance_day_boundary_hour
    if clear_return_approval_threshold:
        store.return_approval_threshold_amount = None
    elif return_approval_threshold_amount is not None:
        if return_approval_threshold_amount < 0:
            raise ValidationAppError(
                "return_approval_threshold_amount must be non-negative",
                error_code="INVALID_RETURN_APPROVAL_THRESHOLD",
            )
        store.return_approval_threshold_amount = return_approval_threshold_amount

    audit_service.log_event(
        db,
        user_id=actor_id,
        action="STORE_SETTINGS_UPDATED",
        entity_type="store",
        entity_id=store.id,
        before=before,
        after={
            "name": store.name,
            "address": store.address,
            "timezone": store.timezone,
            "attendance_day_boundary_hour": store.attendance_day_boundary_hour,
            "return_approval_threshold_amount": (
                str(store.return_approval_threshold_amount)
                if store.return_approval_threshold_amount is not None
                else None
            ),
        },
    )
    db.commit()
    db.refresh(store)
    return store


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


def enforce_store_access(current_user: CurrentUser, target_store_id: int) -> None:
    """Raises ForbiddenError if `current_user` is scoped to a specific
    store (`store_id` is not None) and `target_store_id` is a different
    store (M2 hardening audit Section 12: multi-store safety). A user with
    `store_id is None` (Admin/Manager not tied to one store) may act on
    any store — that is a deliberate cross-store role, not a gap.

    This must be called with a server-trusted `target_store_id` (the
    store a specific already-loaded product/sale/adjustment actually
    belongs to, or a path/query parameter used only to *filter*), never
    with an unchecked client-submitted value used to *decide* what gets
    written — see app.modules.sales.service.finalize_sale for the
    highest-stakes case, which does this check itself for exactly that
    reason (it is also called directly by tests/concurrency code, not
    only through the HTTP route).
    """
    if current_user.store_id is not None and current_user.store_id != target_store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"access store {target_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )


def scoped_store_filter(current_user: CurrentUser, requested_store_id: int | None) -> int | None:
    """For list/search endpoints with an optional `store_id` filter: a
    store-scoped user is always restricted to their own store (defaulting
    to it when they didn't specify one, rejecting any other value they
    did specify) — never falls back to "no filter = every store" the way
    an unscoped cross-store user's omitted filter does.
    """
    if current_user.store_id is None:
        return requested_store_id
    if requested_store_id is not None and requested_store_id != current_user.store_id:
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot "
            f"view store {requested_store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    return current_user.store_id


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
