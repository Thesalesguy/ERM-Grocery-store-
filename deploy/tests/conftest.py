"""Shared fixtures for M13's infrastructure tests (deploy/tests/) — a
real nginx process fronting a real uvicorn process, both started and
torn down per test module. See test_proxy_tls.py's module docstring
for why these live outside backend/tests/.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEPLOY_DIR = REPO_ROOT / "deploy"
SUDO = ["sudo", "-n"]

BASE_URL = "https://127.0.0.1"
HTTP_URL = "http://127.0.0.1"
DB_URL = "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev"

sys.path.insert(0, str(BACKEND_DIR))


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (
        f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def wait_for(url: str, *, verify: bool = True, timeout: float = 10.0) -> None:
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
    run([str(DEPLOY_DIR / "tls" / "generate_self_signed_cert.sh"), str(cert_dir)])

    run([*SUDO, "mkdir", "-p", "/etc/nginx/tls", "/var/www/certbot", "/srv/erp/frontend-dist"])
    run([*SUDO, "cp", str(cert_dir / "fullchain.pem"), "/etc/nginx/tls/fullchain.pem"])
    run([*SUDO, "cp", str(cert_dir / "privkey.pem"), "/etc/nginx/tls/privkey.pem"])
    run([*SUDO, "cp", str(DEPLOY_DIR / "nginx" / "nginx.conf"), "/etc/nginx/nginx.conf"])
    run([*SUDO, "rm", "-f", "/etc/nginx/sites-enabled/default"])
    run(
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
        run([*SUDO, "bash", "-c", f"cp -r {frontend_dist}/* /srv/erp/frontend-dist/"])
    else:
        # A placeholder is fine for this file's purposes (proxy/TLS/
        # rate-limit mechanics, not frontend content) -- the frontend
        # build itself is covered by the ordinary `npm run build` gate.
        run(
            [
                *SUDO,
                "bash",
                "-c",
                "echo '<html>placeholder</html>' > /srv/erp/frontend-dist/index.html",
            ]
        )

    run([*SUDO, "nginx", "-t"])

    # stdout/stderr go to a real log file, never subprocess.PIPE: a PIPE
    # nothing reads fills its OS buffer once uvicorn logs enough lines
    # and then blocks uvicorn itself -- and under pytest's own output
    # capturing, a child inheriting a captured fd can hang the whole
    # test session at teardown. A plain file has neither problem.
    uvicorn_log = open(cert_dir / "uvicorn.log", "w")
    uvicorn_proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        stdout=uvicorn_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for("http://127.0.0.1:8000/health", verify=False)

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
        wait_for(f"{BASE_URL}/health", verify=False)

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
