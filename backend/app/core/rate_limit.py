"""A minimal in-process rate limiter for authentication endpoints.

M2 hardening audit Section 10: login and refresh had no throttling at
all — an attacker could try passwords or replay refresh tokens as fast as
the network allows. This closes that specific gap with the simplest
mechanism that actually helps for the current deployment shape (a single
FastAPI process behind Docker Compose on one small VPS — see
docs/TECHNICAL_BLUEPRINT.md's deployment section): a fixed-window counter
per (client IP, endpoint), held in an in-process dict behind a lock.

What this deliberately is NOT: a distributed limiter. The moment this
application runs as more than one process/instance (a second Compose
replica, multiple app containers behind a load balancer), each instance
holds its own independent counters — an attacker spread across instances
would get `limit * instance_count` attempts, not `limit`. That is a real,
documented limitation, not an oversight: closing it needs a shared store
(Redis, or a table with row-level locking) keyed the same way, which is
appropriately a separate, deliberate infrastructure decision for whenever
this app is actually scaled horizontally — not something to bolt on
speculatively now. See docs/M2_HARDENING_AUDIT.md Section 10.

Keyed by client IP, not by username/account: keying by account would let
an attacker lock out a legitimate user's account by deliberately failing
login as them from anywhere (a denial-of-service on that specific user) —
keying by IP instead only ever throttles the source of the abuse.
"""

import threading
import time
from dataclasses import dataclass

from fastapi import status

from app.core.exceptions import AppError


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "RATE_LIMITED"


@dataclass
class _Window:
    started_at: float
    count: int


class FixedWindowRateLimiter:
    """`limit` requests per `window_seconds` per key. Thread-safe (FastAPI
    with uvicorn's default worker runs endpoint code in a thread pool for
    sync def routes, so concurrent requests from different clients do
    execute this concurrently)."""

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Raises RateLimitedError if `key` has exceeded its budget for
        the current window; otherwise records this call and returns."""
        now = time.monotonic()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started_at >= self._window_seconds:
                self._windows[key] = _Window(started_at=now, count=1)
                return
            if window.count >= self._limit:
                raise RateLimitedError(
                    "Too many attempts. Please wait before trying again.",
                    error_code="RATE_LIMITED",
                )
            window.count += 1

    def reset(self, key: str) -> None:
        """Called on a SUCCESSFUL login so a legitimate user who mistyped
        their password a few times isn't still throttled after getting it
        right — only sustained failure is what this defends against."""
        with self._lock:
            self._windows.pop(key, None)

    def clear(self) -> None:
        """Test-only: wipe all tracked windows. The limiter is a
        module-level singleton (by design — see the module docstring), so
        without this every test in the suite would share one counter and
        the 11th login attempt anywhere in a whole test run would start
        failing with 429 regardless of which test made it. See the
        `_reset_rate_limiters` autouse fixture in tests/conftest.py."""
        with self._lock:
            self._windows.clear()


# Thresholds (M2 hardening audit Section 10): 10 attempts per 5-minute
# window per source IP on the two credential-adjacent endpoints. Loose
# enough that a real user fumbling their password a few times, or a POS
# terminal's silent-refresh-on-every-page-load pattern, never trips it in
# normal use; tight enough to make brute-force/credential-stuffing and
# refresh-token-guessing meaningfully slower than an unthrottled endpoint.
login_rate_limiter = FixedWindowRateLimiter(limit=10, window_seconds=300)
refresh_rate_limiter = FixedWindowRateLimiter(limit=30, window_seconds=300)
