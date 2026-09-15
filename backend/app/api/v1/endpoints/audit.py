"""Audit log read endpoint (M13 Phase 7).

A single, bounded, read-only endpoint over the existing append-only
audit_logs table -- not a generic admin UI. Gated by the AUDIT_READ
permission, which has existed unused since M2 (see
app.modules.auth.permissions and docs/M13_DESIGN.md Section 7 for why
this was judged worth building rather than deferred).

Every filter is a structured, typed FastAPI parameter -- there is no
free-text filter of any kind, so no client input is ever concatenated
into a query string.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.audit import service
from app.modules.audit.schemas import AuditLogRead
from app.modules.auth.permissions import AUDIT_READ
from app.modules.auth.service import CurrentUser, require_permission

router = APIRouter(prefix="/audit-log", tags=["audit"])

_read_permission = require_permission(AUDIT_READ)


@router.get("", response_model=list[AuditLogRead])
def list_audit_log(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[AuditLogRead]:
    # current_user.store_id is the CALLER's own store -- never a
    # client-supplied value. There is deliberately no store_id query
    # parameter: a store-scoped reader (e.g. Manager) always sees
    # exactly their own store's audit trail, resolved per entity_type
    # (see app.modules.audit.service for the resolution table); a
    # cross-store reader (Admin/Auditor, store_id is None) sees
    # everything, matching every other list endpoint's convention.
    entries = service.list_audit_logs(
        db,
        current_user_store_id=current_user.store_id,
        date_from=date_from,
        date_to=date_to,
        action=action,
        entity_type=entity_type,
        actor_id=actor_id,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(entry) for entry in entries]
