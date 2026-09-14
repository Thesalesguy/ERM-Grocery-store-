"""M13 Phase 3: reverse proxy and TLS — live-fire tests against a real
nginx process fronting a real uvicorn process, both started by this
file (not mocked, not merely "the config parses").

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

import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEPLOY_DIR = REPO_ROOT / "deploy"
SUDO = ["sudo", "-n"]

BASE_URL = "https://127.0.0.1"
HTTP_URL = "http://127.0.0.1"
DB_URL = "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev"

sys.path.insert(0, str(BACKEND_DIR))


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (
        f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _wait_for(url: str, *, verify: bool = True, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, verify=verify, timeout=2.0)
            if response.status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001 -- retry loop, re-raised below on timeout
            last_exc = exc
        time.sleep(0.3)
    raise RuntimeError(f"{url} never became ready: {last_exc}")


@pytest.fixture(scope="module")
def production_stack() -> Generator[dict, None, None]:
    """Stands up: a self-signed TLS cert, nginx configured with this
    repo's real deploy/nginx/ files, and a production-shaped uvicorn
    process (no --reload, loopback-only) — then tears both down."""
    cert_dir = Path(tempfile.mkdtemp(prefix="erp_m13_tls_"))
    _run([str(DEPLOY_DIR / "tls" / "generate_self_signed_cert.sh"), str(cert_dir)])

    _run([*SUDO, "mkdir", "-p", "/etc/nginx/tls", "/var/www/certbot", "/srv/erp/frontend-dist"])
    _run([*SUDO, "cp", str(cert_dir / "fullchain.pem"), "/etc/nginx/tls/fullchain.pem"])
    _run([*SUDO, "cp", str(cert_dir / "privkey.pem"), "/etc/nginx/tls/privkey.pem"])
    _run([*SUDO, "cp", str(DEPLOY_DIR / "nginx" / "nginx.conf"), "/etc/nginx/nginx.conf"])
    _run([*SUDO, "rm", "-f", "/etc/nginx/sites-enabled/default"])
    _run(
        [
            *SUDO,
            "bash",
            "-c",
            f"rm -f /etc/nginx/conf.d/*.conf /etc/nginx/conf.d/*.inc && "
            f"cp {DEPLOY_DIR}/nginx/conf.d/erp.conf /etc/nginx/conf.d/erp.conf && "
            f"cp {DEPLOY_DIR}/nginx/conf.d/proxy_params.inc /etc/nginx/conf.d/proxy_params.inc",
        ]
    )

    frontend_dist = REPO_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir() and any(frontend_dist.iterdir()):
        _run(
            [
                *SUDO,
                "bash",
                "-c",
                f"cp -r {frontend_dist}/* /srv/erp/frontend-dist/",
            ]
        )
    else:
        # A placeholder is fine for this file's purposes (proxy/TLS
        # mechanics, not frontend content) -- frontend build itself is
        # covered by the ordinary `npm run build` gate.
        _run([*SUDO, "bash", "-c", "echo '<html>placeholder</html>' > /srv/erp/frontend-dist/index.html"])

    _run([*SUDO, "nginx", "-t"])

    # stdout/stderr go to a real log file, never subprocess.PIPE: a PIPE
    # nothing reads fills its OS buffer once uvicorn logs enough lines
    # and then blocks uvicorn itself -- and under pytest's own output
    # capturing, a child inheriting a captured fd can hang the whole
    # test session at teardown. A plain file has neither problem.
    uvicorn_log = open(cert_dir / "uvicorn.log", "w")
    uvicorn_proc = subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(BACKEND_DIR),
        stdout=uvicorn_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for("http://127.0.0.1:8000/health", verify=False)

        # nginx daemonizes (forks and detaches) and its long-lived
        # worker processes inherit any pipe passed as stdout/stderr --
        # `capture_output=True` here would make `.communicate()` block
        # forever waiting for EOF on a pipe those workers hold open for
        # as long as nginx keeps running (found the hard way: a hung
        # fixture, diagnosed with `py-spy dump`). DEVNULL avoids the
        # pipe entirely; nginx's own access/error logs (nginx.conf) are
        # the real place to look if startup fails.
        subprocess.run(
            [*SUDO, "nginx", "-s", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(0.3)
        start = subprocess.run(
            [*SUDO, "nginx"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        assert start.returncode == 0, "nginx failed to start -- see /var/log/nginx/error.log"
        _wait_for(f"{BASE_URL}/health", verify=False)

        yield {"cert_dir": cert_dir}
    finally:
        subprocess.run(
            [*SUDO, "nginx", "-s", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        uvicorn_proc.terminate()
        try:
            uvicorn_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            uvicorn_proc.kill()
        uvicorn_log.close()
        shutil.rmtree(cert_dir, ignore_errors=True)


def test_https_health_check_succeeds(production_stack: dict) -> None:
    response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_redirects_to_https(production_stack: dict) -> None:
    response = httpx.get(
        f"{HTTP_URL}/health", follow_redirects=False, timeout=5.0
    )
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
    result = subprocess.run(
        ["ss", "-tlnp"], capture_output=True, text=True
    )
    backend_lines = [line for line in result.stdout.splitlines() if ":8000" in line]
    assert backend_lines, "expected the backend to be listening on port 8000"
    for line in backend_lines:
        assert "127.0.0.1:8000" in line, (
            f"backend must bind loopback only, found: {line}"
        )
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
