"""Confirms sensitive fields never reach an emitted log record."""

import logging

from app.core.logging import RedactSensitiveFieldsFilter


def test_password_field_is_redacted() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="login attempt",
        args=None,
        exc_info=None,
    )
    record.password = "super-secret"  # noqa: S105 - deliberately testing redaction
    record.username = "cashier1"

    RedactSensitiveFieldsFilter().filter(record)

    assert record.password == "***REDACTED***"
    assert record.username == "cashier1"
