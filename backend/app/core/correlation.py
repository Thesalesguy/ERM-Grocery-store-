"""M12 Phase 11 (observability): a per-request correlation ID.

Generates a UUID4 (or reuses an inbound `X-Request-ID` if a future proxy
sets one), binds it into the logging context for the duration of the
request via a `contextvars.ContextVar` (the standard way to thread a
value through async call stacks without passing it explicitly
everywhere), and returns it in the response's `X-Request-ID` header.

Combined with the existing audit log (which already records user_id,
action, entity_type/id, timestamp) and each service function's own log
lines, an operator can grep one request ID across app logs and cross-
reference the timestamp against the audit table -- closing the
"correlate a transaction across request/service/DB/audit" gap without
standing up a tracing backend.
"""

import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID_HEADER = "X-Request-ID"
_MAX_INBOUND_REQUEST_ID_LENGTH = 200

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


async def add_correlation_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    inbound = request.headers.get(_REQUEST_ID_HEADER)
    # An inbound value is client-supplied input reaching this process at
    # its boundary -- trusted only as a label to echo back and log
    # alongside, never used for any security or routing decision, and
    # bounded so a client can't use it to bloat every log line for the
    # request.
    if inbound and len(inbound) <= _MAX_INBOUND_REQUEST_ID_LENGTH:
        request_id = inbound
    else:
        request_id = str(uuid.uuid4())

    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers[_REQUEST_ID_HEADER] = request_id
    return response


class RequestIdLogFilter(logging.Filter):
    """Stamps every log record with the current request's correlation ID
    (or None outside of a request, e.g. startup/shutdown logs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
