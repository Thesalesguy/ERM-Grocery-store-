"""Inventory ledger read endpoint.

Read-only in M1 — proving the movement ledger is queryable. There is no
write endpoint yet: movements are only produced by
app.modules.purchasing.service.receive_goods for now (the full adjustment
API arrives in Milestone M2).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.inventory import service
from app.modules.inventory.schemas import InventoryMovementRead

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("/movements", response_model=list[InventoryMovementRead])
def list_movements(
    product_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[InventoryMovementRead]:
    movements = service.list_movements(db, product_id=product_id, limit=limit, offset=offset)
    return [InventoryMovementRead.model_validate(m) for m in movements]
