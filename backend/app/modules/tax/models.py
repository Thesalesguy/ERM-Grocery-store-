"""Tax rate model.

See docs/TECHNICAL_BLUEPRINT.md Section C.5. `rate_percent` is a plain
configurable value — nothing in this module or elsewhere hardcodes 18% (or
any other number) as a permanent business rule. The 18% figure that
appears in the original project specification is example/local-VAT
reference data only; it is never seeded by a migration (migrations are
schema-only) and never assumed by application code.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin


class TaxRate(TimestampMixin, Base):
    __tablename__ = "tax_rates"
    __table_args__ = (
        CheckConstraint(
            "rate_percent >= 0 AND rate_percent <= 100", name="ck_tax_rates_rate_bounds"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_tax_rates_effective_range",
        ),
        Index("ix_tax_rates_active_effective", "is_active", "effective_from"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    rate_percent: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
