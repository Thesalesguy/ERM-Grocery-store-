"""The fiscal-authority adapter interface (docs/M20_DESIGN.md Section 9).

Core sale/accounting logic never depends on a vendor SDK -- only on this
narrow interface. No implementation of a real tax authority exists in
this codebase (docs/M20_DISCOVERY.md Section 1: no jurisdiction is
established), and none is invented here. `NullFiscalProvider` is the only
production-reachable implementation, and it is never actually invoked:
`app.modules.fiscal.service.submit_fiscal_transaction` skips the whole
pipeline whenever `FiscalConfig.is_enabled` is False, which is the value
of every store's config today (see `provider_name` default "NULL" on
`FiscalConfig`). A deterministic fake adapter for tests lives in
`backend/tests/fiscal_fakes.py`, not here, so this module never imports
anything test-only.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

FiscalOutcomeStatus = Literal["ACKNOWLEDGED", "REJECTED"]


@dataclass(frozen=True)
class FiscalSubmissionOutcome:
    """What a provider's `submit` call resolved to. Only ACKNOWLEDGED and
    REJECTED are representable here -- a transient failure (timeout,
    connection error, malformed response) is a raised exception instead
    (see `FiscalProviderError` below), not a third outcome value, so the
    caller (`fiscal/service.py`) cannot forget to handle it."""

    status: FiscalOutcomeStatus
    fiscal_reference: str | None
    raw_response: dict[str, Any]


class FiscalProviderError(Exception):
    """Raised for any transient failure: timeout, connection failure, or
    a response the adapter could not make sense of. The caller records
    this as the submission's FAILED state and `last_error`; it never
    means "rejected by the authority" (that is `FiscalSubmissionOutcome`
    with status="REJECTED", a definitive, non-retried answer)."""


class FiscalProvider(Protocol):
    def submit(self, payload: dict[str, Any]) -> FiscalSubmissionOutcome:
        """Submit one fiscal payload. Returns a definitive outcome
        (ACKNOWLEDGED/REJECTED) or raises FiscalProviderError for
        anything transient/ambiguous. Must be safe to call more than
        once for logically-the-same submission (the caller always
        passes the same frozen payload, including its own idempotency
        key inside it -- docs/M20_DESIGN.md Section 1.2.1); a compliant
        provider is expected to deduplicate by that key, but this
        interface cannot enforce that a given implementation actually
        does."""
        ...


class NullFiscalProvider:
    """The default `provider_name="NULL"` adapter. Never actually called
    in practice -- see this module's docstring -- but defined so a
    `FiscalConfig` row's `provider_name` never points at something that
    doesn't exist. Calling it is a programming error, not a runtime
    condition to handle gracefully."""

    def submit(self, payload: dict[str, Any]) -> FiscalSubmissionOutcome:
        raise NotImplementedError(
            "NullFiscalProvider.submit was called; this should be unreachable, since "
            "app.modules.fiscal.service.submit_fiscal_transaction only invokes a "
            "provider when FiscalConfig.is_enabled is True, and no store is ever "
            "configured with provider_name='NULL' and is_enabled=True at the same time"
        )
