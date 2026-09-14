"""M13 Phase 3: reverse proxy and TLS — live-fire tests against a real
nginx process fronting a real uvicorn process, both started by
conftest.py's `production_stack` fixture (not mocked, not merely "the
config parses").

Deliberately kept OUT of backend/tests/ (which is invoked as the
"full backend suite" gate and runs on every ordinary change): this
file needs `sudo -n` to write into /etc/nginx and bind ports 80/443,
starts and stops real system services, and is specifically an
infrastructure/deployment test, not an application-logic test. Run
directly with: `python -m pytest deploy/tests/test_proxy_tls.py -q`
(from the repo root, with the same erp_dev Postgres the rest of the
suite already assumes).

Docker Compose itself is not used here (this sandbox's Docker daemon
cannot start — docs/M13_DESIGN.md Section 2) — nginx and uvicorn run as
native processes on the same host, which exercises identical proxy
behavior to the containerized topology (nginx does not know or care
whether the thing on 127.0.0.1:8000 is in a container).
"""

import subprocess
import uuid

import httpx
from sqlalchemy import create_engine, text

from conftest import BASE_URL, DB_URL, HTTP_URL


def test_https_health_check_succeeds(production_stack: dict) -> None:
    response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_redirects_to_https(production_stack: dict) -> None:
    response = httpx.get(f"{HTTP_URL}/health", follow_redirects=False, timeout=5.0)
    assert response.status_code == 301
    assert response.headers["location"].startswith("https://")


def test_security_headers_present_at_the_proxy_layer(production_stack: dict) -> None:
    response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert response.headers.get("strict-transport-security", "").startswith("max-age=")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_oversized_body_rejected_at_the_proxy_before_reaching_the_backend(
    production_stack: dict,
) -> None:
    """A body bigger than nginx's own client_max_body_size (2m) must be
    rejected by NGINX ITSELF -- distinguished from the app's own 413
    (M12 Phase 7) by response body: nginx's is an HTML error page, the
    app's is the JSON {"error": {"code": "REQUEST_ENTITY_TOO_LARGE"}}
    body."""
    oversized = b"x" * (3 * 1024 * 1024)
    response = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        content=oversized,
        headers={"Content-Type": "application/json"},
        verify=False,
        timeout=10.0,
    )
    assert response.status_code == 413
    assert "nginx" in response.text.lower()


def test_frontend_static_assets_are_served(production_stack: dict) -> None:
    response = httpx.get(f"{BASE_URL}/", verify=False, timeout=5.0)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_spa_fallback_serves_index_html_for_unknown_client_routes(
    production_stack: dict,
) -> None:
    response = httpx.get(f"{BASE_URL}/some/client/side/route", verify=False, timeout=5.0)
    assert response.status_code == 200


def test_backend_port_is_not_reachable_except_via_loopback_binding(
    production_stack: dict,
) -> None:
    """The actual network-segmentation control (docs/M13_DESIGN.md
    Section 3) is that uvicorn binds 127.0.0.1, not a public interface
    -- verified here via the OS's own listening-socket table, which is
    the ground truth regardless of what any firewall rule claims."""
    result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    backend_lines = [line for line in result.stdout.splitlines() if ":8000" in line]
    assert backend_lines, "expected the backend to be listening on port 8000"
    for line in backend_lines:
        assert "127.0.0.1:8000" in line, f"backend must bind loopback only, found: {line}"
        assert "0.0.0.0:8000" not in line
        assert "*:8000" not in line


def test_a_spoofed_forwarded_for_header_is_not_trusted(production_stack: dict) -> None:
    """The real proof, not just config inspection: a client presenting a
    forged X-Forwarded-For must not be able to make the backend believe
    the request came from anywhere but nginx's own observed peer
    address. proxy_params.inc sets X-Forwarded-For to nginx's own
    $remote_addr, discarding whatever the client sent (deploy/nginx/
    conf.d/proxy_params.inc's own comment explains why bare
    $remote_addr is used instead of the more common
    $proxy_add_x_forwarded_for, which would have appended to -- not
    replaced -- a spoofed value). Verified end to end via the one
    place a request's observed IP is actually persisted: the
    LOGIN_SUCCESS audit event (uvicorn trusts forwarded headers only
    from 127.0.0.1, which is exactly nginx's own vantage point)."""
    from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role

    from app.modules.auth.permissions import CASHIER

    engine = create_engine(DB_URL)
    username = f"m13_spoof_probe_{uuid.uuid4().hex[:8]}"
    with engine.connect() as conn:
        from sqlalchemy.orm import Session

        session = Session(bind=conn)
        store = make_store(session, name=f"M13 Proxy Test Store {uuid.uuid4().hex[:6]}")
        make_user_with_role(session, store, CASHIER, username=username)
        session.commit()

        response = httpx.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"username": username, "password": DEFAULT_TEST_PASSWORD},
            headers={
                "X-Forwarded-For": "66.66.66.66, 7.7.7.7",
                "X-Forwarded-Host": "evil.example.com",
            },
            verify=False,
            timeout=10.0,
        )
        assert response.status_code == 200

        row = conn.execute(
            text(
                "SELECT ip_address FROM audit_logs WHERE action = 'LOGIN_SUCCESS' "
                "AND user_id = (SELECT id FROM users WHERE username = :username) "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"username": username},
        ).fetchone()
        assert row is not None
        assert row[0] == "127.0.0.1", (
            f"expected the real proxy-observed address, got spoofed/wrong value: {row[0]!r}"
        )
        assert row[0] != "66.66.66.66"

        # erp_user (the schema owner, unlike erp_app) can actually
        # clean these rows up -- do so, this file may run many times.
        user_id = conn.execute(
            text("SELECT id FROM users WHERE username = :username"), {"username": username}
        ).scalar_one()
        conn.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM refresh_tokens WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM user_roles WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM stores WHERE id = :sid"), {"sid": store.id})
        conn.commit()
    engine.dispose()
