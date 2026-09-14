"""M12 Phase 7: application-layer HTTP hardening middleware.

Two independent, minimal pieces (see docs/M12_DESIGN.md Sections 13-14):

- `MaxBodySizeMiddleware`: rejects any request whose `Content-Length`
  exceeds `settings.MAX_REQUEST_BODY_BYTES` with a clean 413, before the
  body is ever read into memory. This is the application-layer half of
  the M2-deferred request-size gap; the proxy-layer half
  (`client_max_body_size`) is a separate production control for whenever
  a reverse proxy is actually stood up in front of this app. Checking
  `Content-Length` up front is a fast, cheap rejection for a well-behaved
  (even if malicious) client that sets the header honestly; it is not a
  substitute for a proxy-level cap against a client that lies about the
  header and streams an oversized body anyway — ASGI/Starlette itself
  has no built-in body-size enforcement at the transport layer, which is
  exactly why the proxy-layer control remains the complete defense in a
  real deployment.
- `add_security_headers`: sets `X-Content-Type-Options`,
  `X-Frame-Options`, and `Referrer-Policy` on every response. Standard,
  zero-config, zero-dependency headers. No `Content-Security-Policy` is
  set — the API serves JSON almost everywhere (the two HTML routes,
  `/docs`/`/redoc`, are already disabled in production), so a CSP policy
  would be untested, unused surface area for the one environment where
  it would matter most.
"""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            settings = get_settings()
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > settings.MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "REQUEST_ENTITY_TOO_LARGE",
                            "message": "Request body exceeds the maximum allowed size.",
                        }
                    },
                )
        return await call_next(request)


async def add_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
