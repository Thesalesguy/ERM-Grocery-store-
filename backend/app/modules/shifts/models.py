"""ORM models for cashier/till shift sessions (M15).

See docs/M15_DESIGN.md for the full design and the reasoning behind every
decision documented inline below.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

SHIFT_STATUSES = ("OPEN", "CLOSED")
CASH_MOVEMENT_TYPES = ("PAID_IN", "PAID_OUT")


class CashierShift(TimestampMixin, Base):
    """A cashier's bounded cash-handling session at one store, from an
    opening float to a closing physical cash count.

    Financial-fact freezing (BR-2, the same convention `Sale`/`SaleReturn`
    follow): `expected_cash_amount`, `closing_counted_amount`, and
    `variance_amount` are all written EXACTLY ONCE, inside the single
    atomic close transaction (app.modules.shifts.service.close_shift), and
    never again — a CLOSED shift is never re-opened or otherwise mutated
    (enforced in application code, the same discipline `Sale`/
    `PurchaseInvoice` already rely on for their own post-creation status/
    cache fields, not by a DB-level UPDATE revoke like the pure ledger
    tables `journal_entries`/`inventory_movements`/`audit_logs` use — this
    row is closer in kind to a Sale/PurchaseInvoice than to a ledger line).
    `expected_cash_amount` is NOT a second, independently-maintained
    source of truth: it is derived exactly once, at close time, by a pure
    function (`_compute_expected_cash`) over the shift's own authoritative
    `Sale`/`SaleReturn`/`CashMovement` rows — the same "freeze a derived
    fact at the moment of completion" pattern `Sale.grand_total` already
    uses, not an ongoing balance that could drift out of sync the way
    `PurchaseInvoice.balance_due` deliberately avoids being (that field is
    NOT stored precisely because `amount_paid` keeps changing after
    creation; `expected_cash_amount` cannot change after a CLOSED shift's
    close transaction commits, because no further Sale/SaleReturn/
    CashMovement can ever attach to a CLOSED shift — see the CHECK
    constraint below tying `variance_amount` to its own stored inputs).
    """

    __tablename__ = "cashier_shifts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(SHIFT_STATUSES) + "')",
            name="ck_cashier_shifts_status",
        ),
        CheckConstraint("opening_float >= 0", name="ck_cashier_shifts_opening_float_non_negative"),
        CheckConstraint(
            "closing_counted_amount IS NULL OR closing_counted_amount >= 0",
            name="ck_cashier_shifts_closing_counted_non_negative",
        ),
        # Every closing field is set together, atomically, or not at all —
        # never a half-closed row (mirrors sale_returns'
        # ck_sale_returns_refund_amount_non_negative-style discipline:
        # the DB is the backstop, not just application code).
        CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL AND closing_counted_amount IS NULL "
            "AND expected_cash_amount IS NULL AND variance_amount IS NULL AND closed_by IS NULL) "
            "OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL AND closing_counted_amount IS NOT NULL "
            "AND expected_cash_amount IS NOT NULL AND variance_amount IS NOT NULL "
            "AND closed_by IS NOT NULL)",
            name="ck_cashier_shifts_closing_fields_consistent",
        ),
        # Ties the frozen variance to the arithmetic of this row's own
        # other stored columns (docs/M15_DESIGN.md "Over/short sign
        # convention") — belt-and-suspenders against a future bug writing
        # an inconsistent value, the same pattern
        # ck_sales_grand_total_consistent uses on Sale.
        CheckConstraint(
            "status = 'OPEN' OR " "variance_amount = closing_counted_amount - expected_cash_amount",
            name="ck_cashier_shifts_variance_consistent",
        ),
        # The one active-shift-per-cashier-per-store invariant (docs/
        # M15_DESIGN.md "Shift opening rules" — "one active cash shift per
        # cashier per store" is this milestone's deliberate bounded scope,
        # not a general multi-till model). A plain UNIQUE constraint can't
        # express "at most one OPEN row per cashier" directly since
        # multiple CLOSED rows for the same cashier must remain allowed —
        # a partial unique index is the exact tool for this, same
        # technique `journal_entries.uq_journal_entries_source` already
        # uses for its own "at most one X per Y, under a condition"
        # requirement.
        Index(
            "uq_cashier_shifts_one_open_per_cashier",
            "cashier_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_cashier_shifts_store_id", "store_id"),
        Index("ix_cashier_shifts_cashier_id", "cashier_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    cashier_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    opening_float: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # M15 idempotency key (mirrors Sale.client_transaction_id exactly —
    # same UNIQUE-constraint enforcement, same fast-path/IntegrityError-
    # recovery pattern in the service layer). Required, not optional, for
    # the same reason SaleCreate's is: "disable the button after one
    # click" is not itself protection against a network-level retry.
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_counted_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected_cash_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    variance_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Who actually closed it — the shift's own cashier (a normal
    # self-close) or a different user exercising shift.override (a
    # manager-override close). Comparing this to cashier_id is how a
    # reader answers "was this closed by the cashier or an authorized
    # manager?" without a redundant boolean flag.
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # M15 idempotency key for the close operation — a SEPARATE key from
    # the shift's own client_transaction_id (that one identifies the OPEN
    # request; a retried CLOSE request is a different client action and
    # needs its own key, exactly like Sale.client_transaction_id and
    # SaleReturn.client_transaction_id are two separate columns on two
    # separate rows rather than one key reused across a sale and its
    # later return).
    close_client_transaction_id: Mapped[str | None] = mapped_column(String(100), unique=True)


class CashMovement(TimestampMixin, Base):
    """A paid-in or paid-out cash movement recorded during an OPEN shift
    (docs/M15_DESIGN.md "Cash movements"). Append-only by application-code
    discipline — never updated or deleted after creation, the same
    convention `InventoryMovement` and `Sale`/`SaleReturn` line items
    follow (no route or service function in this codebase ever edits one
    once created).

    Deliberately NOT posted to the GL on its own (docs/M15_DESIGN.md
    "What M15 deliberately did not build") — what specific expense/asset
    category a paid-out serves is not evidenced anywhere in this
    repository (a petty-cash disbursement, a courier payment, a bank
    deposit are all plausible and would each need a different GL
    treatment), and inventing one would be inventing an unspecified
    business rule. Its cash effect is instead folded directly into
    `CashierShift.expected_cash_amount` at close time — the amount is
    never lost, just not yet double-posted to a guessed GL account.
    """

    __tablename__ = "cash_movements"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_cash_movements_amount_positive"),
        CheckConstraint(
            "movement_type IN ('" + "', '".join(CASH_MOVEMENT_TYPES) + "')",
            name="ck_cash_movements_type",
        ),
        Index("ix_cash_movements_shift_id", "shift_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("cashier_shifts.id"), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # M15 idempotency key — mirrors CashierShift.client_transaction_id.
    client_transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
