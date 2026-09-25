"""Deterministic fake fiscal-authority adapter for M20 tests only.

Per docs/M20_DESIGN.md Section 9 ("do not build a fake tax authority...
create a deterministic fake authority adapter"): this simulates
transport/protocol behavior (success, rejection, timeout, connection
failure, duplicate submission, malformed response, delayed response),
never any real authority's business rules. Lives in `tests/`, never
imported by `app/`.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from app.modules.fiscal.provider import FiscalProviderError, FiscalSubmissionOutcome

FakeBehavior = Literal[
    "success", "rejection", "timeout", "connection_failure", "malformed", "delayed"
]


class FakeFiscalProvider:
    """Configurable, deterministic. `behavior` picks what every call does
    once `fail_first_n_attempts` calls have already happened (which
    always raise a transient `FiscalProviderError`, simulating "fails a
    few times then recovers" for retry tests). Tracks every payload it
    was called with in `.calls`, and deduplicates ACKNOWLEDGED responses
    by the payload's own `idempotency_key` -- calling `submit` twice with
    the same key returns the SAME `fiscal_reference` both times, exactly
    as a real deduplicating authority would (docs/M20_DESIGN.md Section
    3's "IF the authority deduplicates by that key" case)."""

    def __init__(
        self,
        behavior: FakeBehavior = "success",
        fail_first_n_attempts: int = 0,
        delay_seconds: float = 0.0,
    ) -> None:
        self.behavior = behavior
        self.fail_first_n_attempts = fail_first_n_attempts
        self.delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []
        self._acknowledged_by_key: dict[str, str] = {}
        self._next_reference_number = 1

    def submit(self, payload: dict[str, Any]) -> FiscalSubmissionOutcome:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_first_n_attempts:
            raise FiscalProviderError(
                f"simulated transient failure (attempt {len(self.calls)} of "
                f"{self.fail_first_n_attempts} configured to fail)"
            )

        if self.behavior == "delayed":
            time.sleep(self.delay_seconds)
        elif self.behavior == "timeout":
            raise FiscalProviderError("simulated timeout waiting for the authority")
        elif self.behavior == "connection_failure":
            raise FiscalProviderError("simulated connection failure reaching the authority")
        elif self.behavior == "malformed":
            raise FiscalProviderError("simulated malformed/unparseable authority response")
        elif self.behavior == "rejection":
            return FiscalSubmissionOutcome(
                status="REJECTED",
                fiscal_reference=None,
                raw_response={"error_code": "SIMULATED_REJECTION_BY_FAKE_AUTHORITY"},
            )

        idempotency_key = payload.get("idempotency_key")
        if idempotency_key in self._acknowledged_by_key:
            return FiscalSubmissionOutcome(
                status="ACKNOWLEDGED",
                fiscal_reference=self._acknowledged_by_key[idempotency_key],
                raw_response={"duplicate_of_key": idempotency_key},
            )
        reference = f"FAKE-REF-{self._next_reference_number:06d}"
        self._next_reference_number += 1
        if idempotency_key is not None:
            self._acknowledged_by_key[idempotency_key] = reference
        return FiscalSubmissionOutcome(
            status="ACKNOWLEDGED", fiscal_reference=reference, raw_response={"ok": True}
        )
