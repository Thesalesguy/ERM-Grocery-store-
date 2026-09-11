"""Application-wide exception types and their FastAPI handlers.

Business modules should raise these instead of ad-hoc HTTPException calls so
error responses stay consistent (BR-1 in docs/TECHNICAL_BLUEPRINT.md: every
error surfaced to a client comes from server-side validation, never a raw
unhandled exception leaking internals).
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for expected, handled application errors."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "APP_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if error_code is not None:
            self.error_code = error_code


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "CONFLICT"


class ValidationAppError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "VALIDATION_ERROR"


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error_code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's own automatic Pydantic validation (bad JSON shape, a
        # field failing its Field(...) constraints) otherwise returns a
        # differently-shaped {"detail": [...]} body — normalized here so
        # every 422, ours or FastAPI's, looks the same to a client.
        first_error = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first_error.get("loc", ()) if part != "body")
        message = first_error.get("msg", "Invalid request")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_body("VALIDATION_ERROR", f"{field}: {message}" if field else message),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Anything that raises a plain HTTPException (routing 404s, 405s,
        # FastAPI security dependencies) — same consistent shape.
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception while processing %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("INTERNAL_ERROR", "An unexpected error occurred."),
        )
