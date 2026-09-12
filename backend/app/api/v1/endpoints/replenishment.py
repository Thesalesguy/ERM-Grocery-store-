"""Replenishment: a read-only suggested-reorder report, gated by the same
`inventory.read` permission as the rest of the inventory module — it is
decision support, not a write path (see
app.modules.replenishment.service's module docstring)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.auth.permissions import INVENTORY_READ
from app.modules.auth.service import CurrentUser, require_permission, scoped_store_filter
from app.modules.replenishment import service
from app.modules.replenishment.schemas import ReplenishmentSuggestionRead

router = APIRouter(prefix="/replenishment", tags=["replenishment"])

_read_permission = require_permission(INVENTORY_READ)


@router.get("/suggestions", response_model=list[ReplenishmentSuggestionRead])
def get_replenishment_suggestions(
    store_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[ReplenishmentSuggestionRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    suggestions = service.get_replenishment_suggestions(db, store_id=effective_store_id)
    return [ReplenishmentSuggestionRead(**s.__dict__) for s in suggestions]
