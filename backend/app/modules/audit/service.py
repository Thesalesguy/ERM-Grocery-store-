"""Audit-log writer.

`log_event` only ever INSERTs (via db.add + db.flush) — it never commits,
so callers compose it into their own transaction boundary, and it never
updates or deletes an existing row, matching the append-only design
(docs/M1_DATABASE_DESIGN.md Section 1.C: UPDATE/DELETE on audit_logs are
also revoked from the application's runtime database role, so this isn't
just a code-level promise).

`before`/`after` accept plain dicts that may contain Decimal or datetime
values (both common in this codebase's business objects) — `_json_safe`
converts them to JSON-serializable primitives before they hit the
JSON-typed columns, since Python's json module can't serialize Decimal
directly.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def log_event(
    db: Session,
    *,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=_json_safe(before) if before is not None else None,
        after_state=_json_safe(after) if after is not None else None,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(entry)
    db.flush()
    return entry
