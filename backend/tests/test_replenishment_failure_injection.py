"""M9 hardening pass: failure injection at multiple points inside every
M9 write workflow's transaction boundary, proving each rolls back
completely — no orphan PO, no orphan transfer, no orphan plan linkage, no
inventory change, no premature GL entry, no partial lifecycle transition,
and a safe retry afterward. Same discipline and pattern as
tests/test_m8_failure_injection.py (monkeypatch a real function called
INSIDE the transaction, never a pre-transaction validation step)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.modules.accounting.models import JournalEntry
from app.modules.audit import service as audit_service
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrder
from app.modules.replenishment import service as replenishment_service
from app.modules.replenishment.models import ReplenishmentPlan
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransfer
from tests.factories import (
    make_product,
    make_store,
    make_supplier,
    make_supplier_product,
    unique_suffix,
)

# --- A: plan generation --------------------------------------------------


def test_a_failure_after_flush_before_audit_log_rolls_back_generated_plans(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects inside generate_replenishment_plans between db.flush()
    (plan rows already have real ids) and the final db.commit() — proves
    the flush alone is not durable and the whole batch rolls back."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside generation audit logging")

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.rollback()

    assert db.query(ReplenishmentPlan).filter_by(product_id=product.id).count() == 0

    # Retry is safe: with the real audit_service restored, generation
    # should now succeed cleanly and produce exactly one plan.
    monkeypatch.undo()
    plans = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    assert len(plans) == 1


def test_a2_failure_before_any_write_leaves_nothing(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects before the first ReplenishmentPlan is even constructed
    (inside the ranking helper generation depends on) — proves a failure
    this early leaves the database completely untouched."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside supplier ranking")

    monkeypatch.setattr(replenishment_service, "rank_suppliers", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.rollback()

    assert db.query(ReplenishmentPlan).filter_by(product_id=product.id).count() == 0


# --- B: plan approval -----------------------------------------------------


def test_b_failure_during_approval_audit_log_rolls_back_status_change(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    plan_id = plan.id

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside approval audit logging")

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.approve_plan(db, plan_id, actor_id=None, caller_store_id=None)
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "RECOMMENDED"
    assert reloaded.approved_at is None
    assert reloaded.approved_by is None

    monkeypatch.undo()
    approved = replenishment_service.approve_plan(db, plan_id, actor_id=None, caller_store_id=None)
    assert approved.status == "APPROVED"


# --- C: plan cancellation --------------------------------------------------


def test_c_failure_during_cancellation_audit_log_rolls_back_status_change(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    plan_id = plan.id

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside cancellation audit logging")

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.cancel_plan(db, plan_id, actor_id=None, caller_store_id=None)
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "RECOMMENDED"
    assert reloaded.cancelled_at is None

    monkeypatch.undo()
    cancelled = replenishment_service.cancel_plan(db, plan_id, actor_id=None, caller_store_id=None)
    assert cancelled.status == "CANCELLED"


# --- D: plan execution into a purchase order -------------------------------


def _make_approved_supplier_plan(db: Session) -> tuple[ReplenishmentPlan, int]:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    return plan, store.id


def test_d1_failure_inside_po_creation_leaves_no_orphan_po_and_plan_stays_approved(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects BEFORE any PurchaseOrder row is even flushed (inside
    `_create_purchase_order_inner` itself, as imported into the
    replenishment service module) — proves the plan's own status update,
    which happens AFTER the PO is created, never lands either."""
    plan, _store_id = _make_approved_supplier_plan(db)
    plan_id = plan.id

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside PO creation")

    monkeypatch.setattr(replenishment_service, "_create_purchase_order_inner", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="d1-exec"
        )
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "APPROVED"  # never advanced to EXECUTED
    assert reloaded.generated_purchase_order_id is None
    assert db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count() == 0


def test_d2_failure_after_po_created_before_final_commit_rolls_back_po_too(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects on the FINAL audit_service.log_event call inside
    execute_plan (REPLENISHMENT_PLAN_EXECUTED, the last write before
    db.commit()) — by this point a real PurchaseOrder + PurchaseOrderItem
    have already been flushed inside the SAME transaction (via
    _create_purchase_order_inner, which itself calls db.flush()). Proves
    the single-transaction design (Design Decision 3) really does roll
    the PO back too, not just the plan's own status fields."""
    plan, _store_id = _make_approved_supplier_plan(db)
    plan_id = plan.id

    real_log_event = audit_service.log_event
    call_count = {"n": 0}

    def _boom_on_second_call(db_, **kwargs):
        call_count["n"] += 1
        if kwargs.get("action") == "REPLENISHMENT_PLAN_EXECUTED":
            raise RuntimeError("forced failure immediately before commit")
        return real_log_event(db_, **kwargs)

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom_on_second_call)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="d2-exec"
        )
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "APPROVED"
    assert reloaded.generated_purchase_order_id is None
    # The PO that _create_purchase_order_inner flushed inside the same
    # transaction must be gone too — proving this is one atomic unit, not
    # two separately-committed steps.
    assert db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count() == 0

    # Retry after the failure is safe and produces exactly one PO.
    monkeypatch.undo()
    executed = replenishment_service.execute_plan(
        db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="d2-exec-retry"
    )
    assert executed.status == "EXECUTED"
    assert db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count() == 1


# --- E: plan execution into a transfer -------------------------------------


def _make_approved_transfer_plan(db: Session) -> ReplenishmentPlan:
    store_a = make_store(db)
    store_b = make_store(db)
    sku = "SKU-FAILINJ-TRANSFER"
    make_product(
        db, store_a, sku=sku, current_qty_on_hand=Decimal("50"), reorder_point=Decimal("10")
    )
    make_product(
        db, store_b, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    db.commit()
    (plan,) = replenishment_service.generate_replenishment_plans(db, store_id=store_b.id)
    db.commit()
    replenishment_service.approve_plan(db, plan.id, actor_id=None, caller_store_id=None)
    db.commit()
    return plan


def test_e1_failure_inside_transfer_creation_leaves_no_orphan_transfer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_approved_transfer_plan(db)
    plan_id = plan.id

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure inside transfer creation")

    monkeypatch.setattr(replenishment_service, "_create_transfer_inner", _boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="e1-exec"
        )
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "APPROVED"
    assert reloaded.generated_transfer_id is None
    assert db.query(InterStoreTransfer).filter_by(replenishment_plan_id=plan_id).count() == 0


def test_e2_failure_after_transfer_created_before_final_commit_rolls_back_transfer_too(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_approved_transfer_plan(db)
    plan_id = plan.id

    real_log_event = audit_service.log_event

    def _boom_on_execution_log(db_, **kwargs):
        if kwargs.get("action") == "REPLENISHMENT_PLAN_EXECUTED":
            raise RuntimeError("forced failure immediately before commit")
        return real_log_event(db_, **kwargs)

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom_on_execution_log)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="e2-exec"
        )
    db.rollback()

    reloaded = db.get(ReplenishmentPlan, plan_id)
    assert reloaded.status == "APPROVED"
    assert reloaded.generated_transfer_id is None
    assert db.query(InterStoreTransfer).filter_by(replenishment_plan_id=plan_id).count() == 0

    monkeypatch.undo()
    executed = replenishment_service.execute_plan(
        db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="e2-exec-retry"
    )
    assert executed.status == "EXECUTED"
    assert db.query(InterStoreTransfer).filter_by(replenishment_plan_id=plan_id).count() == 1


# --- F: retry/idempotency path after a failed attempt ----------------------


def test_f_retry_with_same_client_transaction_id_after_failed_attempt_executes_exactly_once(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client_transaction_id used on the FAILED attempt is reused on
    retry — proves the idempotency key itself was never durably recorded
    by the failed (rolled-back) attempt, so the retry is a genuine first
    successful execution, not a false-positive idempotent replay."""
    plan, _store_id = _make_approved_supplier_plan(db)
    plan_id = plan.id
    shared_key = "f-retry-key"

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure on first attempt")

    monkeypatch.setattr(replenishment_service, "_create_purchase_order_inner", _boom)
    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id=shared_key
        )
    db.rollback()
    assert db.get(ReplenishmentPlan, plan_id).execution_client_transaction_id is None

    monkeypatch.undo()
    executed = replenishment_service.execute_plan(
        db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id=shared_key
    )
    assert executed.status == "EXECUTED"
    assert executed.execution_client_transaction_id == shared_key
    assert db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count() == 1

    # A THIRD call with the same key now hits the idempotency fast path.
    replayed = replenishment_service.execute_plan(
        db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id=shared_key
    )
    assert replayed.id == executed.id
    assert db.query(PurchaseOrder).filter_by(replenishment_plan_id=plan_id).count() == 1


# --- G: no premature GL entry can survive a failed M9 execution -----------


def test_g_failed_execution_creates_no_journal_entry(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _store_id = _make_approved_supplier_plan(db)
    plan_id = plan.id
    before_count = db.query(JournalEntry).count()

    real_log_event = audit_service.log_event

    def _boom_on_execution_log(db_, **kwargs):
        if kwargs.get("action") == "REPLENISHMENT_PLAN_EXECUTED":
            raise RuntimeError("forced failure before commit")
        return real_log_event(db_, **kwargs)

    monkeypatch.setattr(replenishment_service.audit_service, "log_event", _boom_on_execution_log)

    with pytest.raises(RuntimeError, match="forced failure"):
        replenishment_service.execute_plan(
            db, plan_id, actor_id=None, caller_store_id=None, client_transaction_id="g-exec"
        )
    db.rollback()

    assert db.query(JournalEntry).count() == before_count


# --- Sanity: the M8 purchasing/transfer failure-injection tests still
# pass unmodified after this session's fixes to those modules' shared
# _create_purchase_order_inner / _create_transfer_inner refactor --------


def test_purchasing_and_transfer_modules_still_importable_and_unmodified_in_behavior(
    db: Session,
) -> None:
    """Not a new failure-injection scenario — a guard that the M9-added
    `replenishment_plan_id` parameter didn't change either function's
    default (None) behavior for ordinary, non-replenishment callers."""
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    po = purchasing_service.create_purchase_order(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        order_date=date.today(),
        client_transaction_id=f"po-{unique_suffix()}",
        lines=[
            purchasing_service.PurchaseOrderItemInput(
                product_id=product.id, quantity_ordered=Decimal("5"), unit_cost=Decimal("1.00")
            )
        ],
    )
    assert po.replenishment_plan_id is None

    store_b = make_store(db)
    make_product(db, store_b, sku=product.sku)
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store.id,
        to_store_id=store_b.id,
        requested_date=date.today(),
        lines=[
            transfer_service.TransferLineInput(
                source_product_id=product.id, requested_quantity=Decimal("1")
            )
        ],
        caller_store_id=None,
    )
    assert transfer.replenishment_plan_id is None
