"""ORM model for AccountingEntity.

See docs/M24D_TECHNICAL_CONTRACT.md Section 3.1/4 for the full design.
Every `Store` belongs to exactly one `AccountingEntity` (the FK column
lives on `Store`, app.modules.auth.models). The migration that introduces
this table backfills every existing store onto a single default
`CORPORATE_DIVISION` entity, so this phase changes no existing accounting
behavior -- `entity_type` is read by a later M25 phase to decide whether a
transfer between two stores is an intra-entity reclassification (today's
unchanged `Inventory In Transit` mechanics) or an inter-entity transaction
(a future AP/AR treatment). Nothing in this phase reads or branches on
`entity_type` yet.
"""

from sqlalchemy import Boolean, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

ENTITY_TYPES = ("CORPORATE_DIVISION", "INDEPENDENT")


class AccountingEntity(TimestampMixin, Base):
    __tablename__ = "accounting_entities"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('" + "', '".join(ENTITY_TYPES) + "')",
            name="ck_accounting_entities_entity_type",
        ),
        # At most one entity may be the default (the one every existing
        # store is backfilled onto) -- a partial unique index rather than
        # a plain UNIQUE constraint, since `is_default` is false for every
        # other row and a plain UNIQUE would wrongly forbid more than one
        # non-default entity.
        Index(
            "uq_accounting_entities_one_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    entity_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="CORPORATE_DIVISION"
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
