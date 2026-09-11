"""Password hashing and JWT helpers.

**Decision (M2): JWT access tokens + opaque, hashed, rotating refresh
tokens** — not server-side sessions. Rationale:

- The frontend is a decoupled SPA calling a JSON API; a short-lived,
  stateless access token verified by signature alone (no DB round trip)
  keeps the hot path (every authenticated request, including every
  barcode lookup) fast — a session-lookup-per-request model would add a
  DB query to every single POS interaction.
- A pure stateless JWT can't be revoked before it expires, so it is kept
  very short-lived (15 minutes) and paired with a refresh token that
  *is* server-side state (the `refresh_tokens` table) — giving us actual
  revocation (logout, compromise response) without paying a DB lookup on
  every request, only on the infrequent refresh.
- This matches the mechanism docs/TECHNICAL_BLUEPRINT.md Section H
  already specified.

Password hashing uses argon2id (via argon2-cffi), the blueprint's
preferred choice, with library defaults (time_cost/memory_cost tuned by
argon2-cffi's own defaults, which target this same ~ms-scale-a bit
under blueprint's suggested ~250ms — an operator can raise them later
via PasswordHasher(...) kwargs if warranted).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

_password_hasher = PasswordHasher()


def hash_password(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except VerifyMismatchError:
        return False


def create_access_token(*, user_id: int, store_id: int | None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "store_id": store_id,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (or a subclass) on any invalid/expired token —
    callers translate that to a 401, never leaking the underlying reason."""
    settings = get_settings()
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return payload


def generate_refresh_token() -> tuple[str, str]:
    """Returns (raw_token_for_the_client, sha256_hash_for_storage).

    The raw value is only ever handed to the client once, at issuance; the
    database stores only its hash (the same pattern as a password), so a
    stolen database dump doesn't hand over usable refresh tokens.
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
