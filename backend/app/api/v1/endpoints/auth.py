"""Authentication endpoints.

The refresh token travels as an httpOnly, SameSite=Lax cookie (never
readable by JS — mitigates token theft via XSS); the access token travels
in the JSON response body, for the SPA to attach as an `Authorization:
Bearer` header on every subsequent request (never as a cookie itself, so
it's immune to CSRF — a forged cross-site request can't forge an
Authorization header). See app.core.security's module docstring for the
full JWT-vs-sessions design decision.
"""

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.rate_limit import login_rate_limiter, refresh_rate_limiter
from app.modules.auth import service
from app.modules.auth.schemas import AccessTokenResponse, CurrentUserResponse, LoginRequest
from app.modules.auth.service import CurrentUser, get_current_user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, raw_refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        httponly=True,
        secure=settings.is_production or settings.ENVIRONMENT == "staging",
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )


@router.post("/login", response_model=AccessTokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AccessTokenResponse:
    client_ip = _client_ip(request)
    # Checked before touching the database at all (M2 hardening audit
    # Section 10) — an IP that's already over budget shouldn't get a free
    # password-hash-verification's worth of server work per attempt.
    login_rate_limiter.check(client_ip)
    user, access_token, raw_refresh = service.login(
        db,
        username=payload.username,
        password=payload.password,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    # A successful login clears this IP's failure count — the limiter is
    # defending against sustained guessing, not penalizing a real user who
    # fat-fingered their password twice before getting it right.
    login_rate_limiter.reset(client_ip)
    _set_refresh_cookie(response, raw_refresh)
    settings = get_settings()
    return AccessTokenResponse(
        access_token=access_token,
        expires_in_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AccessTokenResponse:
    refresh_rate_limiter.check(_client_ip(request))
    raw_refresh = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_refresh is None:
        raise UnauthorizedError("Missing refresh token")
    user, access_token, new_raw_refresh = service.refresh_access_token(
        db,
        raw_refresh_token=raw_refresh,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, new_raw_refresh)
    settings = get_settings()
    return AccessTokenResponse(
        access_token=access_token,
        expires_in_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    raw_refresh = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_refresh is not None:
        service.logout(db, raw_refresh_token=raw_refresh)
    response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)


@router.get("/me", response_model=CurrentUserResponse)
def me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=current_user.id,
        username=current_user.username,
        full_name=current_user.full_name,
        store_id=current_user.store_id,
        permissions=sorted(current_user.permissions),
    )
