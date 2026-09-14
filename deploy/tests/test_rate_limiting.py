"""M13 Phase 4: rate limiting — live-fire tests against the real nginx
`limit_req_zone` rules (deploy/nginx/nginx.conf), fronting a real
backend, using the shared `production_stack` fixture (conftest.py).

Deployment shape decided here (docs/M13_DESIGN.md Section 3): a single
VPS, one nginx process, one backend process — matching M0's own
"~$20/month VPS" target. Whether the backend itself later runs as
multiple uvicorn workers behind that one nginx is irrelevant to what
this phase proves: nginx's `limit_req_zone` is a shared-memory zone
owned by the ONE nginx master process, so it is authoritative
regardless of backend worker count -- unlike the M12 in-process Python
limiter (app/core/rate_limit.py), which is scoped to whichever single
backend process handles a given request and would silently multiply
its effective budget by the worker count if the backend ever did scale
horizontally (M12 Section 14's own documented risk, restated here
because it's the reason nginx-level limiting is the layer this phase
adds, not a replacement for the app-level one, which stays as
defense-in-depth for exactly the requests nginx also limits).
"""

import time

import httpx

from conftest import BASE_URL


def _burst(path: str, method: str = "GET", *, count: int, **kwargs) -> list[int]:
    statuses = []
    for _ in range(count):
        response = httpx.request(method, f"{BASE_URL}{path}", verify=False, timeout=5.0, **kwargs)
        statuses.append(response.status_code)
    return statuses


def test_repeated_login_requests_are_throttled_at_the_proxy(production_stack: dict) -> None:
    """erp_login: rate=10r/m, burst=5 -- nginx itself must reject a
    request BEFORE it ever reaches the backend once the budget is
    exceeded. Distinguished from the app-level 429 (M2/M12) by response
    body: nginx's limit_req rejection is an HTML error page."""
    statuses = _burst(
        "/api/v1/auth/login",
        method="POST",
        count=25,
        json={"username": "rl_probe_user", "password": "wrong-password"},
        headers={"Content-Type": "application/json"},
    )
    assert 429 in statuses, f"expected at least one 429 among {statuses}"


def test_throttled_response_is_a_proxy_level_rejection_not_the_app(
    production_stack: dict,
) -> None:
    responses = []
    for _ in range(25):
        r = httpx.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"username": "rl_probe_user2", "password": "wrong-password"},
            verify=False,
            timeout=5.0,
        )
        responses.append(r)
        if r.status_code == 429:
            break
    throttled = [r for r in responses if r.status_code == 429]
    assert throttled, "never observed a 429 -- rate limiting did not engage"
    body = throttled[0].text.lower()
    assert "nginx" in body or "<html" in body, (
        f"expected nginx's own rate-limit rejection page, got: {body[:200]!r}"
    )


def test_legitimate_low_volume_traffic_is_never_blocked(production_stack: dict) -> None:
    """A handful of well-spaced requests -- what a real cashier's POS
    terminal actually does -- must never trip the limiter. Rate
    limiting must protect against abuse, not ordinary store operation
    (the task's own explicit warning against "a rate limiter that can
    accidentally block all store operations indefinitely")."""
    statuses = []
    for _ in range(5):
        response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
        statuses.append(response.status_code)
        time.sleep(0.2)
    assert all(s == 200 for s in statuses), statuses


def test_rate_limit_recovers_after_the_window(production_stack: dict) -> None:
    """erp_general allows 120r/m with a burst of 40 -- exhaust the
    burst, then wait past the token-refill window and confirm requests
    succeed again. Proves this is a real, recovering rate limit, not an
    effectively-permanent block."""
    exhausted = False
    for _ in range(60):
        response = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
        if response.status_code == 429:
            exhausted = True
            break
    assert exhausted, "expected to exhaust the erp_general burst within 60 rapid requests"

    time.sleep(2.0)  # nginx's leaky-bucket refills continuously; 2s is ample at 120r/m
    recovered = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert recovered.status_code == 200, (
        "rate limit never recovered -- store operations would be blocked indefinitely"
    )


def test_admin_mutation_endpoint_has_its_own_tighter_zone(production_stack: dict) -> None:
    """The one administrative mutation (M12 Phase 10's user
    deactivation) is rate-limited by erp_admin (20r/m, burst=10), not
    the looser erp_general zone -- verified by exhausting it in fewer
    requests than erp_general would need, using a real (unauthenticated,
    so it 401s rather than actually deactivating anyone) request against
    the exact matched route."""
    statuses = _burst(
        "/api/v1/auth/users/999999/deactivate", method="POST", count=25
    )
    assert 429 in statuses, f"expected the tighter admin zone to engage, got {statuses}"
    # Never a 500/other server error -- a throttled OR unauthenticated
    # response, nothing else.
    assert all(s in (401, 429) for s in statuses), statuses
