"""M13 Phase 12: store connectivity / edge failure.

No offline POS mode is built here -- not asked for, and would be a
business feature this milestone's scope explicitly excludes
(docs/M13_DESIGN.md Section 12). What this file proves is the existing
idempotency machinery's authority under REAL proxy-layer conditions it
had never been exercised against before: M12's test_idempotency.py and
test_sale_finalization_failure_injection.py already proved a retried
`client_transaction_id` is safe at the service-layer call boundary --
this file proves the same guarantee holds end to end through a real
nginx hop, and adds the two connectivity failure modes that only exist
at the proxy layer: the backend being genuinely unreachable, and a
real `proxy_read_timeout` firing against a genuinely slow upstream.

A note on "duplicate retry arriving after a connection reset": this
file does not attempt to literally sever a TCP connection mid-request
(inherently racy -- whether the backend's commit lands before or after
a forced socket reset depends on exact timing no test can control
deterministically). What actually matters for correctness is the
SERVER's behavior when the same client_transaction_id arrives twice in
separate requests -- that input is identical whether the client's first
attempt failed via a timeout, a reset, or any other transient network
condition, and is exactly what test_duplicate_checkout_retry_through_
the_real_proxy_is_not_double_booked below sends through a real nginx
hop, deterministically.
"""

from __future__ import annotations

import subprocess
import sys
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx
from conftest import BACKEND_DIR, BASE_URL, DB_URL, SUDO, run, wait_for
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(BACKEND_DIR))


def _uvicorn_pids() -> list[str]:
    result = subprocess.run(
        ["pgrep", "-f", "uvicorn app.main:app.*--port 8000"],
        capture_output=True,
        text=True,
    )
    return [pid for pid in result.stdout.split() if pid]


def _stop_backend() -> None:
    for pid in _uvicorn_pids():
        subprocess.run(["kill", "-TERM", pid])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _uvicorn_pids():
        time.sleep(0.2)


def _start_backend() -> subprocess.Popen:
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    wait_for("http://127.0.0.1:8000/health", verify=False)
    return proc


def test_nginx_returns_a_clean_gateway_error_when_the_backend_is_unreachable(
    production_stack,
) -> None:
    """API unavailable: the backend process is gone entirely (crashed,
    not merely slow). nginx must respond with a clean 502, not hang,
    not leak an internal error, and recover cleanly once the backend
    comes back."""
    _stop_backend()
    try:
        response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=10.0)
        assert response.status_code == 502
    finally:
        _start_backend()
        wait_for(f"{BASE_URL}/health", verify=False)


def test_nginx_proxy_read_timeout_fires_against_a_genuinely_slow_upstream(
    production_stack,
) -> None:
    """Network timeout: a real, deliberately slow upstream (a small
    stub HTTP server, not the application -- this proves nginx's OWN
    timeout mechanism fires correctly under a real, controllable delay,
    independent of how long any particular application endpoint happens
    to take) proxied through a temporary nginx location with a short
    proxy_read_timeout. The client must see a clean 504, not a hang."""
    slow_port = 9931

    class _SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            time.sleep(3)  # longer than the 1s proxy_read_timeout below
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"too late")

        def log_message(self, *args) -> None:  # noqa: D102
            pass

    server = HTTPServer(("127.0.0.1", slow_port), _SlowHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    snippet_path = Path("/etc/nginx/conf.d/zz-test-slow-upstream.conf")
    snippet = f"""
    server {{
        listen 8443 ssl;
        server_name _;
        ssl_certificate     /etc/nginx/tls/fullchain.pem;
        ssl_certificate_key /etc/nginx/tls/privkey.pem;
        location / {{
            proxy_pass http://127.0.0.1:{slow_port};
            proxy_read_timeout 1s;
        }}
    }}
    """
    try:
        run([*SUDO, "bash", "-c", f"cat > {snippet_path} << 'EOF'\n{snippet}\nEOF"])
        run([*SUDO, "nginx", "-t"])
        reload_result = subprocess.run(
            [*SUDO, "nginx", "-s", "reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        assert reload_result.returncode == 0
        time.sleep(0.3)

        response = httpx.get("https://127.0.0.1:8443/", verify=False, timeout=10.0)
        assert response.status_code == 504
    finally:
        server.shutdown()
        run([*SUDO, "rm", "-f", str(snippet_path)])
        subprocess.run(
            [*SUDO, "nginx", "-s", "reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(0.3)


def test_duplicate_checkout_retry_through_the_real_proxy_is_not_double_booked(
    production_stack,
) -> None:
    """The load-bearing test: two real HTTP checkout requests through
    nginx, same client_transaction_id -- proves the idempotency
    guarantee M12 already proved at the service layer holds end to end
    through a real proxy hop too, not just in a direct Python call."""
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_product,
        make_store,
        make_user_with_role,
        unique_suffix,
    )

    suffix = unique_suffix()
    username = f"conn_fail_duplicate_cashier_{suffix}"

    engine = create_engine(DB_URL)
    with Session(engine) as db:
        store = make_store(db)
        make_user_with_role(db, store, "Cashier", username=username)
        product = make_product(
            db, store, current_price=Decimal("9.99"), current_qty_on_hand=Decimal("50")
        )
        db.commit()
        store_id, product_id = store.id, product.id

    login_resp = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": username, "password": DEFAULT_TEST_PASSWORD},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    checkout_body = {
        "store_id": store_id,
        "client_transaction_id": f"conn-fail-dup-test-{suffix}",
        "lines": [{"product_id": product_id, "quantity": "1"}],
        "payments": [{"payment_method": "CASH", "amount": "9.99"}],
    }

    first = httpx.post(
        f"{BASE_URL}/api/v1/sales", verify=False, json=checkout_body, headers=headers
    )
    assert first.status_code in (200, 201), first.text
    first_sale_id = first.json()["id"]

    second = httpx.post(
        f"{BASE_URL}/api/v1/sales", verify=False, json=checkout_body, headers=headers
    )
    assert second.status_code in (200, 201), second.text
    assert second.json()["id"] == first_sale_id, "a retried checkout must return the SAME sale"

    with Session(engine) as db:
        from app.modules.sales.models import Sale

        count = db.execute(
            select(func.count()).select_from(Sale).where(Sale.store_id == store_id)
        ).scalar_one()
        assert count == 1, "exactly one sale must exist despite the duplicate request"
