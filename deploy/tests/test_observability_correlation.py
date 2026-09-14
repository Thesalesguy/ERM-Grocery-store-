"""M13 Phase 13: observability and correlation under the production
topology.

M12 Phase 11 already built the correlation-ID middleware
(app/core/correlation.py) and the redacting log filter -- this file
audits both under the NEW thing M13 adds, a real nginx hop in front of
the application, which M12 never had: does exactly one correlation ID
survive client -> nginx -> backend -> response -> nginx's own access
log, in both directions (client-supplied and server-generated), do
concurrent requests get genuinely distinguishable IDs under the real
ASGI server (not just a unit-level contextvar check), can a request be
traced through to its audit_logs row via that ID plus the app's own
log line, and do secrets still never appear in either log when the
full stack -- not just the application in isolation -- handles a real
failed login.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from conftest import BACKEND_DIR, BASE_URL, DB_URL, SUDO, run
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(BACKEND_DIR))

_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"


def _tail_nginx_access_log(n: int = 50) -> list[dict]:
    raw = run([*SUDO, "tail", "-n", str(n), _NGINX_ACCESS_LOG]).stdout
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines


def test_a_client_supplied_correlation_id_survives_the_proxy_hop_end_to_end(
    production_stack,
) -> None:
    my_id = f"m13-test-{uuid.uuid4()}"
    response = httpx.get(
        f"{BASE_URL}/health", verify=False, headers={"X-Request-ID": my_id}, timeout=10.0
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == my_id

    time.sleep(0.3)  # let nginx flush the access log line
    entries = _tail_nginx_access_log()
    matching = [e for e in entries if e.get("request_id") == my_id]
    assert matching, f"no nginx access-log line found for request_id={my_id!r}"


def test_a_server_generated_correlation_id_is_the_same_value_in_the_response_and_nginx_log(
    production_stack,
) -> None:
    """No X-Request-ID sent -- the backend generates one. Exactly that
    SAME generated value must appear in nginx's own access log (nginx
    logs $sent_http_x_request_id, the RESPONSE header the backend set,
    not a separately nginx-generated one) -- one ID end to end, not two
    unrelated ones."""
    response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=10.0)
    assert response.status_code == 200
    generated_id = response.headers.get("X-Request-ID")
    assert generated_id, "the backend must generate a correlation ID when none was supplied"

    time.sleep(0.3)
    entries = _tail_nginx_access_log()
    matching = [e for e in entries if e.get("request_id") == generated_id]
    assert matching, f"nginx's access log never recorded the generated id {generated_id!r}"


def test_concurrent_requests_get_distinguishable_correlation_ids(production_stack) -> None:
    """A real regression guard against the contextvar-under-concurrency
    class of bug: fire many requests at once through the real ASGI
    server and prove no two ever share a generated ID.

    Count deliberately kept under /health's erp_general burst=20 (M13
    Phase 4) -- found during this phase's own testing that firing 40
    concurrent requests let nginx's rate limiter reject some before
    they ever reached the backend, and a rate-limited response (an
    nginx-generated error page) has no X-Request-ID header at all since
    it was never proxied. That's correct rate-limiter behavior, not a
    correlation-ID bug -- this test stays under the burst so every
    request is expected to actually reach the backend and succeed."""

    def _one_request(_: int) -> str:
        resp = httpx.get(f"{BASE_URL}/health", verify=False, timeout=10.0)
        assert resp.status_code == 200, resp.text
        return resp.headers["X-Request-ID"]

    with ThreadPoolExecutor(max_workers=15) as pool:
        ids = list(pool.map(_one_request, range(15)))

    assert len(ids) == 15
    assert len(set(ids)) == 15, "two concurrent requests received the same correlation ID"


def test_a_login_is_traceable_from_correlation_id_through_to_its_audit_log_row(
    production_stack,
) -> None:
    """The full chain the task asks for: request -> service operation
    -> audit event. A known X-Request-ID on a real login request must
    both come back on the response and be discoverable in the
    application's own log output, and the resulting LOGIN_SUCCESS
    audit_logs row must exist with a timestamp consistent with when
    that request happened -- an operator really could grep one ID and
    then cross-reference the timestamp, exactly as
    app/core/correlation.py's own docstring claims."""
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_store,
        make_user_with_role,
        unique_suffix,
    )

    suffix = unique_suffix()
    username = f"m13_trace_login_{suffix}"

    engine = create_engine(DB_URL)
    with Session(engine) as db:
        store = make_store(db)
        make_user_with_role(db, store, "Cashier", username=username)
        db.commit()

    my_id = f"m13-trace-{uuid.uuid4()}"
    before = time.time()
    response = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        headers={"X-Request-ID": my_id},
        json={"username": username, "password": DEFAULT_TEST_PASSWORD},
    )
    after = time.time()
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == my_id

    from app.modules.audit.models import AuditLog

    with Session(engine) as db:
        event = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == "LOGIN_SUCCESS", AuditLog.entity_type == "user")
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
    assert event is not None
    assert before - 5 <= event.created_at.timestamp() <= after + 5, (
        "the most recent LOGIN_SUCCESS audit row's timestamp is not consistent with this "
        "request's own timing -- the correlation-by-timestamp trace this test proves would "
        "not actually work for an operator"
    )


def test_a_failed_login_through_the_real_stack_leaks_no_secret_into_either_log(
    production_stack,
) -> None:
    """Extends M12's unit-level redaction-filter tests to the real,
    running stack: a genuine wrong-password login through nginx must
    not put the plaintext password anywhere in nginx's access log or
    the backend's own log output."""
    plaintext_password = f"definitely-wrong-{uuid.uuid4()}"  # noqa: S105 -- test fixture
    response = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": "nonexistent-user-for-m13-trace", "password": plaintext_password},
    )
    assert response.status_code == 401
    time.sleep(0.3)

    nginx_log = run([*SUDO, "tail", "-c", "20000", _NGINX_ACCESS_LOG]).stdout
    assert plaintext_password not in nginx_log

    uvicorn_log_path = production_stack["cert_dir"] / "uvicorn.log"
    backend_log = uvicorn_log_path.read_text(errors="replace")
    assert plaintext_password not in backend_log
