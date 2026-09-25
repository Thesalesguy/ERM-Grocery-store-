"""Fiscal configuration and submission endpoints (M20).

`fiscal.read` gates viewing a store's config/submissions; `fiscal.
config.write` (Admin-only in the seed RBAC matrix) gates enabling/
configuring fiscalization for a store; `fiscal.retry` gates manually
re-running one failed/exhausted submission. See docs/M20_DESIGN.md
Section 8.

Every route is store-scoped exactly like `stores.py`'s settings
endpoints: a store-scoped user (`current_user.store_id is not None`)
can only ever see/act on their own store's fiscal data — mirrors the
pervasive `store_id` isolation check used by every other module.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import FISCAL_CONFIG_WRITE, FISCAL_READ, FISCAL_RETRY
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.fiscal import service
from app.modules.fiscal.schemas import FiscalConfigRead, FiscalConfigUpdate, FiscalSubmissionRead

router = APIRouter(prefix="/fiscal", tags=["fiscal"])

_read_permission = require_permission(FISCAL_READ)
_config_write_permission = require_permission(FISCAL_CONFIG_WRITE)
_retry_permission = require_permission(FISCAL_RETRY)


def _check_store_access(current_user: CurrentUser, store_id: int) -> None:
    if current_user.store_id is not None and current_user.store_id != store_id:
        raise NotFoundError(f"Store {store_id} not found")


@router.get("/config", response_model=FiscalConfigRead | None)
def get_fiscal_config(
    store_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> FiscalConfigRead | None:
    _check_store_access(current_user, store_id)
    config = service.get_config(db, store_id)
    return FiscalConfigRead.model_validate(config) if config is not None else None


@router.put("/config", response_model=FiscalConfigRead)
def update_fiscal_config(
    payload: FiscalConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_config_write_permission),
) -> FiscalConfigRead:
    _check_store_access(current_user, payload.store_id)
    config = service.upsert_config(
        db,
        store_id=payload.store_id,
        is_enabled=payload.is_enabled,
        provider_name=payload.provider_name,
        credential_reference=payload.credential_reference,
        submission_endpoint=payload.submission_endpoint,
        retry_max_attempts=payload.retry_max_attempts,
        updated_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(config)
    return FiscalConfigRead.model_validate(config)


@router.get("/submissions", response_model=list[FiscalSubmissionRead])
def list_fiscal_submissions(
    store_id: int = Query(...),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[FiscalSubmissionRead]:
    _check_store_access(current_user, store_id)
    submissions = service.list_submissions(
        db, store_id=store_id, status=status, limit=limit, offset=offset
    )
    return [FiscalSubmissionRead.model_validate(s) for s in submissions]


@router.post("/submissions/{submission_id}/retry", response_model=FiscalSubmissionRead)
def retry_fiscal_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_retry_permission),
) -> FiscalSubmissionRead:
    submission = service.get_submission(db, submission_id)
    _check_store_access(current_user, submission.store_id)
    submission = service.submit_fiscal_transaction(db, submission_id)
    db.commit()
    db.refresh(submission)
    return FiscalSubmissionRead.model_validate(submission)
