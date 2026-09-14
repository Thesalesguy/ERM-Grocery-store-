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

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.modules.accounting.models import JournalEntry
from app.modules.ap.models import PurchaseInvoice, SupplierCreditNote, SupplierPayment
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.hr.models import AttendanceRecord, EmploymentAssignment
from app.modules.inventory.models import StockAdjustment, StockCount, StockCountLine
from app.modules.payroll.models import PayrollPeriod
from app.modules.products.models import Product
from app.modules.purchasing.models import GoodsReceipt, PurchaseOrder, PurchaseReturn
from app.modules.replenishment.models import ReplenishmentPlan
from app.modules.sales.models import Sale, SaleReturn
from app.modules.transfers.models import InterStoreTransfer, InterStoreTransferReceipt


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


# --- Store-scoped reading (M13 Phase 7) -------------------------------
#
# AuditLog itself has no store_id column (docs/M13_DESIGN.md Section 7 —
# adding one would mean touching every one of the ~35 existing
# log_event(...) call sites, exactly the kind of M0-M12 redesign this
# milestone must not do). Instead, for a store-scoped reader, each
# entity_type's allowed entity_ids are resolved via the store-bearing
# column that already exists on THAT entity's own table. Every entity_type
# actually used by a log_event(...) call site in this codebase (verified
# via `grep -rhn 'entity_type="' app/modules`) is classified below into
# exactly one of three buckets; anything not classified is excluded by
# default from a store-scoped reader's results -- never guessed, never
# leaked.

# entity_type -> model with its own store_id column. Typed as `Any` --
# mypy can't verify attribute access (.id, .store_id) against a plain
# `type`, and this dict deliberately spans many unrelated ORM classes.
_DIRECT_STORE_ENTITY_MODELS: dict[str, Any] = {
    "product": Product,
    "sale": Sale,
    "sale_return": SaleReturn,
    "purchase_order": PurchaseOrder,
    "purchase_invoice": PurchaseInvoice,
    "purchase_return": PurchaseReturn,
    "goods_receipt": GoodsReceipt,
    "stock_adjustment": StockAdjustment,
    "stock_count": StockCount,
    "payroll_period": PayrollPeriod,
    "attendance_record": AttendanceRecord,
    "supplier_payment": SupplierPayment,
    "supplier_credit_note": SupplierCreditNote,
    "journal_entry": JournalEntry,
    "inter_store_transfer_receipt": InterStoreTransferReceipt,
}

# entity_type -> (model, column_a, column_b): matches if EITHER column
# equals the caller's store (e.g. a transfer's origin or destination).
_OR_STORE_ENTITY_MODELS: dict[str, tuple[Any, str, str]] = {
    "inter_store_transfer": (InterStoreTransfer, "from_store_id", "to_store_id"),
    "replenishment_plan": (ReplenishmentPlan, "destination_store_id", "source_store_id"),
}

# entity_types resolved via a join rather than a column on their own
# table (stock_count_line -> stock_counts.store_id; employee -> the
# employee's current employment_assignments.store_id; user -> the
# user's own store_id, technically direct but modeled separately since
# User isn't a "business entity" table like the others).
_JOIN_RESOLVED_ENTITY_TYPES = frozenset({"stock_count_line", "employee", "user"})

# entity_types with NO store dimension at all -- always excluded from a
# store-scoped reader's results, regardless of filter.
_NO_STORE_DIMENSION_ENTITY_TYPES = frozenset(
    {"department", "position", "supplier", "supplier_product"}
)

_ALL_KNOWN_ENTITY_TYPES = (
    set(_DIRECT_STORE_ENTITY_MODELS)
    | set(_OR_STORE_ENTITY_MODELS)
    | _JOIN_RESOLVED_ENTITY_TYPES
    | _NO_STORE_DIMENSION_ENTITY_TYPES
)


def _entity_ids_for_store(entity_type: str, store_id: int) -> Select | None:
    """A scalar subquery of entity_ids of `entity_type` belonging to
    `store_id`, or None if `entity_type` has no store dimension (the
    caller must exclude it entirely rather than fall back to "no
    filter")."""
    if entity_type in _DIRECT_STORE_ENTITY_MODELS:
        model = _DIRECT_STORE_ENTITY_MODELS[entity_type]
        return select(model.id).where(model.store_id == store_id)

    if entity_type in _OR_STORE_ENTITY_MODELS:
        model, col_a, col_b = _OR_STORE_ENTITY_MODELS[entity_type]
        return select(model.id).where(
            or_(getattr(model, col_a) == store_id, getattr(model, col_b) == store_id)
        )

    if entity_type == "stock_count_line":
        return (
            select(StockCountLine.id)
            .join(StockCount, StockCountLine.stock_count_id == StockCount.id)
            .where(StockCount.store_id == store_id)
        )

    if entity_type == "employee":
        # "Current" employee-store assignment: the one open-ended row
        # (effective_to IS NULL) -- same convention as
        # hr.service.get_current_assignment.
        return select(EmploymentAssignment.employee_id).where(
            EmploymentAssignment.store_id == store_id,
            EmploymentAssignment.effective_to.is_(None),
        )

    if entity_type == "user":
        return select(User.id).where(User.store_id == store_id)

    return None


def list_audit_logs(
    db: Session,
    *,
    current_user_store_id: int | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditLog]:
    """List audit log entries, newest first. `current_user_store_id` is
    the CALLER's own store_id (None for a cross-store reader such as
    Admin/Auditor/unscoped Manager) -- never a client-supplied value, so
    a store-scoped caller cannot request a different store's audit
    trail (there is deliberately no store_id query parameter on this
    endpoint at all)."""
    stmt: Select = select(AuditLog)

    if date_from is not None:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(AuditLog.created_at <= date_to)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.user_id == actor_id)

    if current_user_store_id is not None:
        if entity_type is not None:
            if entity_type not in _ALL_KNOWN_ENTITY_TYPES or (
                entity_type in _NO_STORE_DIMENSION_ENTITY_TYPES
            ):
                # An unknown entity_type is treated the same as one with
                # no store dimension: excluded, not guessed.
                return []
            subquery = _entity_ids_for_store(entity_type, current_user_store_id)
            # Guaranteed non-None: entity_type was just confirmed to be
            # both known and not in the no-store-dimension set.
            assert subquery is not None
            stmt = stmt.where(AuditLog.entity_type == entity_type, AuditLog.entity_id.in_(subquery))
        else:
            allowed_entity_types = sorted(
                _ALL_KNOWN_ENTITY_TYPES - _NO_STORE_DIMENSION_ENTITY_TYPES
            )
            clauses = []
            for et in allowed_entity_types:
                et_subquery = _entity_ids_for_store(et, current_user_store_id)
                assert et_subquery is not None
                clauses.append(
                    and_(AuditLog.entity_type == et, AuditLog.entity_id.in_(et_subquery))
                )
            stmt = stmt.where(or_(*clauses))
    elif entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)

    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())
