"""Store settings endpoints (M16).

`store.settings.read` gates the GET; `store.settings.write` gates the
PUT. Exposes exactly the Store columns that already existed with
documented semantics before this milestone (attendance_day_boundary_hour,
M10; return_approval_threshold_amount, M14) plus the store's basic
identity fields — see app.modules.auth.service.update_store_settings's
docstring for why `is_active` is deliberately not editable here.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.auth import service
from app.modules.auth.permissions import STORE_SETTINGS_READ, STORE_SETTINGS_WRITE
from app.modules.auth.schemas import StoreSettingsRead, StoreSettingsUpdateRequest
from app.modules.auth.service import CurrentUser, require_permission

router = APIRouter(prefix="/stores", tags=["stores"])

_read_permission = require_permission(STORE_SETTINGS_READ)
_write_permission = require_permission(STORE_SETTINGS_WRITE)


def _to_read(store) -> StoreSettingsRead:  # type: ignore[no-untyped-def]
    return StoreSettingsRead(
        id=store.id,
        name=store.name,
        address=store.address,
        timezone=store.timezone,
        is_active=store.is_active,
        attendance_day_boundary_hour=store.attendance_day_boundary_hour,
        return_approval_threshold_amount=store.return_approval_threshold_amount,
    )


@router.get("/{store_id}/settings", response_model=StoreSettingsRead)
def get_store_settings(
    store_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> StoreSettingsRead:
    if current_user.store_id is not None and current_user.store_id != store_id:
        raise NotFoundError(f"Store {store_id} not found")
    store = service.get_store(db, store_id)
    return _to_read(store)


@router.put("/{store_id}/settings", response_model=StoreSettingsRead)
def update_store_settings(
    store_id: int,
    payload: StoreSettingsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_write_permission),
) -> StoreSettingsRead:
    store = service.update_store_settings(
        db,
        store_id=store_id,
        name=payload.name,
        address=payload.address,
        timezone=payload.timezone,
        attendance_day_boundary_hour=payload.attendance_day_boundary_hour,
        return_approval_threshold_amount=payload.return_approval_threshold_amount,
        clear_return_approval_threshold=payload.clear_return_approval_threshold,
        actor_id=current_user.id,
        caller_store_id=current_user.store_id,
    )
    return _to_read(store)
