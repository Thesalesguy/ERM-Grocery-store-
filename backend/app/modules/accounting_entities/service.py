"""Read-only helpers for the AccountingEntity foundation (M25 Phase 1).

No mutating function is defined here: this phase ships no API to create
an entity or reassign a store, per docs/M24D_TECHNICAL_CONTRACT.md Section
25's Phase 1 row ("API: None yet"). These two functions are the seam a
later M25 phase (the one that actually branches transfer accounting on
entity membership) will call -- defined now, against the real schema, so
that phase does not have to invent its own store/entity resolution.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.modules.accounting_entities.models import AccountingEntity
from app.modules.auth.models import Store


def get_default_accounting_entity(db: Session) -> AccountingEntity:
    """The single entity every pre-Phase-1 store was backfilled onto.
    Exactly one row has `is_default = true` (enforced by the partial
    unique index on `accounting_entities.is_default`), created once by
    the Phase 1 migration -- never created here."""
    entity = db.execute(
        select(AccountingEntity).where(AccountingEntity.is_default.is_(True))
    ).scalar_one_or_none()
    if entity is None:
        raise NotFoundError("No default accounting entity exists")
    return entity


def get_store_accounting_entity_id(db: Session, store_id: int) -> int:
    store = db.get(Store, store_id)
    if store is None:
        raise NotFoundError(f"Store {store_id} not found")
    return store.accounting_entity_id


def is_same_accounting_entity(db: Session, store_id_a: int, store_id_b: int) -> bool:
    """True when both stores belong to the same AccountingEntity --
    today, trivially true for every pair, since every store is backfilled
    onto the one default entity and nothing in this phase ever creates a
    second one."""
    return get_store_accounting_entity_id(db, store_id_a) == get_store_accounting_entity_id(
        db, store_id_b
    )
