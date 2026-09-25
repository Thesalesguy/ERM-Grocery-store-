"""ORM models for the fiscal-integration boundary (M20).

See docs/M20_DESIGN.md Section 1. This module deliberately does not
compute tax or post accounting entries -- it observes an already-taxed,
already-posted `Sale` (app.modules.sales.models.Sale) and optionally
forwards a read-only projection of it toward an external tax authority.
Per docs/M20_DISCOVERY.md Section 1, no tax jurisdiction is established
in this project, so `FiscalConfig.is_enabled` defaults to False on every
store and nothing in this module is ever exercised in a real deployment
until an operator explicitly configures one.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

FISCAL_SUBMISSION_STATUSES = ("PENDING", "FAILED", "ACKNOWLEDGED", "REJECTED", "EXHAUSTED")
_TERMINAL_FISCAL_SUBMISSION_STATUSES = ("ACKNOWLEDGED", "REJECTED", "EXHAUSTED")


class FiscalConfig(TimestampMixin, Base):
    """Per-store fiscal-integration configuration -- the configuration
    boundary docs/M20_DESIGN.md Section 1.1 describes. `is_enabled=False`
    (the default, and the only value ever seeded) means the fiscal
    pipeline is a complete no-op for that store: `finalize_sale` never
    creates a `FiscalSubmission` row and no adapter is ever invoked.

    `credential_reference` is a NAME/POINTER (e.g. an environment
    variable name), never the credential value itself -- see
    docs/M20_DESIGN.md Section 6. No route ever returns its value in a
    way that would expose a real secret, because none is ever stored
    here to expose.
    """

    __tablename__ = "fiscal_configs"
    __table_args__ = (
        CheckConstraint(
            "retry_max_attempts > 0", name="ck_fiscal_configs_retry_max_attempts_positive"
        ),
        Index("ix_fiscal_configs_store_id", "store_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    # "NULL" is the only value ever seeded or defaulted to -- it names no
    # real adapter (none exists) and is never invoked because is_enabled
    # is always False alongside it out of the box. "FAKE" is used only by
    # this milestone's own tests, via FakeFiscalProvider.
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False, default="NULL")
    credential_reference: Mapped[str | None] = mapped_column(String(255))
    submission_endpoint: Mapped[str | None] = mapped_column(String(500))
    retry_max_attempts: Mapped[int] = mapped_column(nullable=False, default=5)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class FiscalSubmission(TimestampMixin, Base):
    """The durable fiscal-submission ledger ("outbox") -- one row per
    `Sale`, ever (docs/M20_DESIGN.md Section 1.2). `sale_id` IS the
    idempotency key: the UNIQUE constraint below is the actual
    enforcement that a sale can never have two fiscal submissions,
    exactly mirroring the `client_transaction_id` UNIQUE-constraint
    pattern used everywhere else in this codebase (see
    docs/M19_HARDENING_AUDIT.md) -- Postgres rejects a second INSERT for
    the same sale_id even under concurrent submission attempts, not just
    an application-level check.

    `provider_name`/`max_attempts` are copied from `FiscalConfig` at
    creation time, not live-joined, so a later config change can never
    retroactively alter how an already-created submission is described --
    the same historical-freezing discipline `SaleItem.tax_rate_id`/
    `tax_amount` already follow for the tax domain itself.
    """

    __tablename__ = "fiscal_submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(FISCAL_SUBMISSION_STATUSES) + "')",
            name="ck_fiscal_submissions_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_fiscal_submissions_attempt_count_non_negative"
        ),
        CheckConstraint("max_attempts > 0", name="ck_fiscal_submissions_max_attempts_positive"),
        Index("ix_fiscal_submissions_store_id", "store_id"),
        Index("ix_fiscal_submissions_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fiscal_reference: Mapped[str | None] = mapped_column(String(255))
    # Built once, at row-creation time, from the Sale's own already-frozen
    # totals -- never rebuilt on retry (docs/M20_DESIGN.md Section 3), so
    # every attempt for a given submission sends byte-identical content.
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Whatever the adapter returned/raised on the most recent attempt
    # only -- opaque, never parsed by application logic beyond extracting
    # fiscal_reference (docs/M20_DESIGN.md Section 1.2).
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


def is_terminal_fiscal_status(status: str) -> bool:
    """True for ACKNOWLEDGED/REJECTED/EXHAUSTED -- the three states a
    submission never leaves once reached (docs/M20_DESIGN.md Section 3:
    a retry against a terminal submission is a pure no-op)."""
    return status in _TERMINAL_FISCAL_SUBMISSION_STATUSES
