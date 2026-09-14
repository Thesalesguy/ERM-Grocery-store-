"""Structured JSON logging configuration.

Logs are emitted as JSON to stdout (captured by the Docker logging driver in
deployment, see docs/TECHNICAL_BLUEPRINT.md Section L). A filter redacts
common sensitive field names so passwords, secrets, and tokens never reach a
log line even if a caller accidentally includes them in `extra`.
"""

import logging
import logging.config

from app.core.config import get_settings
from app.core.correlation import RequestIdLogFilter

_REDACTED_KEYS = {
    "password",
    "password_hash",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
}
_REDACTED_VALUE = "***REDACTED***"


class RedactSensitiveFieldsFilter(logging.Filter):
    """Strips known-sensitive keys out of a log record's `extra` payload."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__.keys()):
            if key.lower() in _REDACTED_KEYS:
                setattr(record, key, _REDACTED_VALUE)
        return True


def configure_logging() -> None:
    settings = get_settings()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "redact_sensitive": {"()": RedactSensitiveFieldsFilter},
                "request_id": {"()": RequestIdLogFilter},
            },
            "formatters": {
                "json": {
                    "()": "pythonjsonlogger.json.JsonFormatter",
                    "format": "%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "filters": ["redact_sensitive", "request_id"],
                },
            },
            "root": {
                "level": settings.LOG_LEVEL,
                "handlers": ["console"],
            },
        }
    )
