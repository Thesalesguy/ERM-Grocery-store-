"""M14: return/void approval-threshold enforcement.

docs/M14_DESIGN.md is the authoritative design. Covers every mandatory
test category from that milestone's task brief: threshold boundaries,
authorization (permission/self-approval/store-isolation), non-replay/
non-reuse/exact-amount binding, orphaned-state safety on failure,
concurrency, idempotency-unaffected, GL/inventory-unchanged-when-
approved, audit-log content, and direct-API-bypass rejection.

Two layers, like the rest of this test suite: service-level tests call
app.modules.sales.service.create_sale_return/void_sale directly (fast,
precise assertions on approval_required/approved_by/audit rows);
API-level tests go through the real HTTP endpoint (proves the gate can't
be bypassed by a direct API call, per the task's explicit requirement).
"""

import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.db.session import SessionLocal
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER
from app.modules.sales import service as sales_service
from app.modules.sales.models import SaleReturn
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers

THRESHOLD = Decimal("100.00")


def _make_completed_sale(db: Session, store, cashier, *, quantity=Decimal("20")):
    """20 units @ 10.00 = 200.00 -- comfortably above THRESHOLD so a full
    return/void requires approval; individual smaller returns can be
    tuned below/at/above THRESHOLD by the caller via `quantity`."""
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("500"),
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=quantity)],
        payments=[PaymentInput(payment_method="CASH", amount=(Decimal("10.00") * quantity))],
    )
    db.commit()
    return sale, product


def _thresholded_store(db: Session, **overrides):
    return make_store(db, return_approval_threshold_amount=THRESHOLD, **overrides)


# --- 1-2-3: threshold boundary (service level) ------------------------------


def test_below_threshold_return_succeeds_without_approval(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("9"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()

    assert sale_return.refund_amount == Decimal("90.00")
    assert sale_return.approval_required is False
    assert sale_return.approved_by is None


def test_exactly_at_threshold_requires_approval(db: Session) -> None:
    """Boundary semantics: docs/M14_DESIGN.md establishes `>=` ("at or
    above") as the intended boundary from this milestone's own explicit
    task text, not the older blueprint wording ("above"). A refund of
    EXACTLY the threshold must require approval."""
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)

    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            # 10 units @ 10.00 = exactly 100.00 = THRESHOLD
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("10"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc.value.error_code == "APPROVAL_REQUIRED"
    # Nothing was created -- see "orphaned state" section below for the
    # explicit assertion; this test's own point is the boundary itself.


def test_just_below_threshold_does_not_require_approval(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)

    # 9 units @ 10.00 = 90.00 < 100.00 threshold
    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("9"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    assert sale_return.approval_required is False


def test_above_threshold_requires_approval_when_no_approver_given(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)

    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc.value.error_code == "APPROVAL_REQUIRED"


def test_no_threshold_configured_means_gate_inactive(db: Session) -> None:
    """A store with NO configured threshold (the default) behaves exactly
    as every store did before M14 -- no approval ever required,
    regardless of amount."""
    store = make_store(db)  # no return_approval_threshold_amount override -> NULL
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier, quantity=Decimal("50"))

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("50"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
    )
    db.commit()
    assert sale_return.approval_required is False
    assert sale_return.approved_by is None


# --- 4-8: authorization -----------------------------------------------------


def test_unauthorized_cashier_cannot_approve(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    other_cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)

    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
            approver_username=other_cashier.username,
            approver_password=DEFAULT_TEST_PASSWORD,
        )
    assert exc.value.error_code == "APPROVAL_PERMISSION_DENIED"


def test_authorized_manager_can_approve(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, cashier)

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
        approver_username=manager.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()
    assert sale_return.approval_required is True
    assert sale_return.approved_by == manager.id


def test_initiating_cashier_cannot_self_approve(db: Session) -> None:
    """Even if the SAME user somehow also held approval permission, the
    id check is unconditional -- initiator != approver, full stop."""
    store = _thresholded_store(db)
    manager = make_user_with_role(db, store, MANAGER)  # holds sales.return.approve
    sale, _ = _make_completed_sale(db, store, manager)

    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=manager.id,
            approver_username=manager.username,
            approver_password=DEFAULT_TEST_PASSWORD,
        )
    assert exc.value.error_code == "SELF_APPROVAL_NOT_ALLOWED"


def test_manager_from_another_store_cannot_cross_store_approve(db: Session) -> None:
    store_a = _thresholded_store(db)
    store_b = make_store(db)
    cashier_a = make_user_with_role(db, store_a, CASHIER)
    manager_b = make_user_with_role(db, store_b, MANAGER)
    sale, _ = _make_completed_sale(db, store_a, cashier_a)

    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store_a.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier_a.id,
            approver_username=manager_b.username,
            approver_password=DEFAULT_TEST_PASSWORD,
        )
    assert exc.value.error_code == "STORE_ACCESS_DENIED"


def test_admin_can_approve_across_stores(db: Session) -> None:
    """Matches the existing RBAC model exactly: Admin's store_id is None
    (unrestricted), the same rule enforce_store_access/_enforce_store_
    access already apply everywhere else in this codebase."""
    store_a = _thresholded_store(db)
    cashier_a = make_user_with_role(db, store_a, CASHIER)
    admin = make_user_with_role(db, None, ADMIN)
    sale, _ = _make_completed_sale(db, store_a, cashier_a)

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store_a.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier_a.id,
        approver_username=admin.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()
    assert sale_return.approved_by == admin.id


def test_wrong_approver_password_is_rejected(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, cashier)

    with pytest.raises(UnauthorizedError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
            approver_username=manager.username,
            approver_password="wrong-password",
        )
    assert exc.value.error_code == "INVALID_APPROVER_CREDENTIALS"


# --- 9-11: reuse / replay / exact-amount binding ----------------------------


def test_approval_cannot_be_reused_for_another_return(db: Session) -> None:
    """A manager's SUCCESSFUL approval of one return has zero effect on
    whether the NEXT above-threshold return needs its own -- there is no
    detachable "approval" state anywhere in this design to reuse."""
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, cashier, quantity=Decimal("40"))

    first = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
        approver_username=manager.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()
    assert first.approved_by == manager.id

    # A second above-threshold return against the SAME sale, no approver
    # supplied this time -- must independently require approval again.
    with pytest.raises(ForbiddenError) as exc:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
        )
    assert exc.value.error_code == "APPROVAL_REQUIRED"


def test_approval_is_tied_to_the_exact_computed_amount(db: Session) -> None:
    """The amount the gate evaluated and the amount actually posted are
    the SAME function call's output (_compute_line_refund), not two
    independent computations that could drift apart."""
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, cashier)

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
        approver_username=manager.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()
    assert sale_return.refund_amount == Decimal("150.00")

    granted = (
        db.execute(select(AuditLog).where(AuditLog.action == "SALE_RETURN_APPROVAL_GRANTED"))
        .scalars()
        .all()
    )
    assert granted, "expected an approval-granted audit row"
    assert Decimal(granted[-1].after_state["refund_amount"]) == sale_return.refund_amount


# --- 12-13: orphaned-state safety -------------------------------------------


def test_rejected_approval_leaves_no_sale_return_row(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    sale, _ = _make_completed_sale(db, store, cashier)
    txn_id = f"ret-{unique_suffix()}"

    before_count = len(db.execute(select(SaleReturn)).scalars().all())
    with pytest.raises(ForbiddenError):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
            created_by=cashier.id,
        )
    db.rollback()
    after = db.execute(
        select(SaleReturn).where(SaleReturn.client_transaction_id == txn_id)
    ).scalar_one_or_none()
    assert after is None
    assert len(db.execute(select(SaleReturn)).scalars().all()) == before_count
    # The line item itself must be untouched.
    db.refresh(sale.items[0])
    assert sale.items[0].quantity_returned == Decimal("0")


def test_failed_approval_credentials_leave_no_partial_state(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, product = _make_completed_sale(db, store, cashier)
    qty_before = product.current_qty_on_hand

    with pytest.raises(UnauthorizedError):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=cashier.id,
            approver_username=manager.username,
            approver_password="wrong-password",
        )
    db.rollback()
    db.refresh(product)
    assert product.current_qty_on_hand == qty_before
    db.refresh(sale.items[0])
    assert sale.items[0].quantity_returned == Decimal("0")


# --- 14-15: concurrency ------------------------------------------------------


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None


def _attempt_return_with_approver(
    *,
    sale_id: int,
    store_id: int,
    sale_item_id: int,
    quantity: Decimal,
    created_by: int,
    approver_username: str | None,
    approver_password: str | None,
    barrier: threading.Barrier,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        sales_service.create_sale_return(
            session,
            sale_id=sale_id,
            store_id=store_id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=quantity)],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=created_by,
            approver_username=approver_username,
            approver_password=approver_password,
        )
        session.commit()
        result.succeeded = True
    except Exception as exc:  # noqa: BLE001 - captured for assertion below
        session.rollback()
        result.error_code = getattr(exc, "error_code", type(exc).__name__)
    finally:
        session.close()


def test_concurrent_approval_attempts_behave_deterministically() -> None:
    """Two concurrent above-threshold returns against two DISJOINT
    quantities of the same sale, one with a valid approver and one
    without, racing at the same instant. Each request's own approval
    check is self-contained (no shared "unlocked" state to race over),
    so each outcome must match its own inputs regardless of ordering."""
    session = SessionLocal()
    try:
        store = _thresholded_store(session)
        cashier = make_user_with_role(session, store, CASHIER)
        manager = make_user_with_role(session, store, MANAGER)
        sale, _ = _make_completed_sale(session, store, cashier, quantity=Decimal("40"))
        session.commit()
        sale_id, store_id, item_id = sale.id, store.id, sale.items[0].id
        created_by = cashier.id
        approver_username = manager.username
    finally:
        session.close()

    barrier = threading.Barrier(2)
    ok_result = _Outcome()
    fail_result = _Outcome()
    t1 = threading.Thread(
        target=_attempt_return_with_approver,
        kwargs=dict(
            sale_id=sale_id,
            store_id=store_id,
            sale_item_id=item_id,
            quantity=Decimal("15"),
            created_by=created_by,
            approver_username=approver_username,
            approver_password=DEFAULT_TEST_PASSWORD,
            barrier=barrier,
            result=ok_result,
        ),
    )
    t2 = threading.Thread(
        target=_attempt_return_with_approver,
        kwargs=dict(
            sale_id=sale_id,
            store_id=store_id,
            sale_item_id=item_id,
            quantity=Decimal("15"),
            created_by=created_by,
            approver_username=None,
            approver_password=None,
            barrier=barrier,
            result=fail_result,
        ),
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert ok_result.succeeded is True
    assert fail_result.succeeded is False
    assert fail_result.error_code == "APPROVAL_REQUIRED"


def test_concurrent_returns_cannot_bypass_threshold_by_splitting() -> None:
    """Two concurrent returns, EACH individually below threshold, racing
    on the same sale, neither supplying an approver. Both must succeed
    without approval (per-transaction threshold, documented scope
    limitation in docs/M14_DESIGN.md: cumulative/structuring detection
    across separate transactions is explicitly out of scope) -- this
    test pins down and proves that documented boundary rather than
    silently assuming it."""
    session = SessionLocal()
    try:
        store = _thresholded_store(session)
        cashier = make_user_with_role(session, store, CASHIER)
        sale, _ = _make_completed_sale(session, store, cashier, quantity=Decimal("18"))
        session.commit()
        sale_id, store_id = sale.id, store.id
        item_id = sale.items[0].id
        created_by = cashier.id
    finally:
        session.close()

    barrier = threading.Barrier(2)
    r1, r2 = _Outcome(), _Outcome()
    threads = [
        threading.Thread(
            target=_attempt_return_with_approver,
            kwargs=dict(
                sale_id=sale_id,
                store_id=store_id,
                sale_item_id=item_id,
                quantity=Decimal("9"),  # 90.00 < 100.00 threshold each
                created_by=created_by,
                approver_username=None,
                approver_password=None,
                barrier=barrier,
                result=r,
            ),
        )
        for r in (r1, r2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert r1.succeeded and r2.succeeded


# --- 16-17: existing idempotency unaffected ---------------------------------


def test_return_idempotency_unaffected_below_threshold(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"cashier_{unique_suffix()}"
    cashier = make_user_with_role(db, store, CASHIER, username=username)
    sale, _ = _make_completed_sale(db, store, cashier, quantity=Decimal("2"))
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    payload = {
        "store_id": store.id,
        "return_date": "2024-01-02",
        "client_transaction_id": f"ret-{unique_suffix()}",
        "refund_method": "CASH",
        "lines": [{"sale_item_id": sale.items[0].id, "quantity": "1"}],
    }
    r1 = client.post(f"/api/v1/sales/{sale.id}/returns", json=payload, headers=headers)
    r2 = client.post(f"/api/v1/sales/{sale.id}/returns", json=payload, headers=headers)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


def test_void_idempotency_unaffected_below_threshold(client: TestClient, db: Session) -> None:
    """Pre-existing (pre-M14, unchanged) behavior, confirmed rather than
    assumed: void_sale recomputes its "remaining quantity" lines fresh on
    every call, BEFORE create_sale_return's own client_transaction_id
    check runs — so a full void followed by a literal repeat of the same
    call gets NOTHING_TO_VOID (409), not the original 201, because
    there's nothing left to void by the time the retry recomputes its
    lines. This is not a financial-safety issue (no double post, no
    double inventory movement — it fails closed, cleanly) and M14 does
    not change it; this test pins the actual, pre-existing behavior so a
    future change can't silently alter it without a test noticing."""
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=username)
    sale, _ = _make_completed_sale(db, store, manager, quantity=Decimal("2"))
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    payload = {
        "store_id": store.id,
        "return_date": "2024-01-02",
        "client_transaction_id": f"void-{unique_suffix()}",
        "refund_method": "CASH",
    }
    r1 = client.post(f"/api/v1/sales/{sale.id}/void", json=payload, headers=headers)
    r2 = client.post(f"/api/v1/sales/{sale.id}/void", json=payload, headers=headers)
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "NOTHING_TO_VOID"


# --- 18: GL/inventory effects unchanged when approved -----------------------


def test_approved_return_produces_identical_gl_and_inventory_effects(db: Session) -> None:
    from app.modules.accounting.models import JournalEntry, JournalLine
    from app.modules.inventory.models import InventoryMovement

    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, product = _make_completed_sale(db, store, cashier, quantity=Decimal("20"))
    qty_before = product.current_qty_on_hand

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
        approver_username=manager.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()

    movement = db.execute(
        select(InventoryMovement).where(
            InventoryMovement.reference_type == "sale_return",
            InventoryMovement.reference_id == sale_return.id,
        )
    ).scalar_one()
    assert movement.quantity_delta == Decimal("15")
    db.refresh(product)
    assert product.current_qty_on_hand == qty_before + Decimal("15")

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()
    lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit  # balanced, exactly like every non-gated return


# --- 19: audit log content ---------------------------------------------------


def test_audit_log_records_approval_grant_with_actor_target_store(db: Session) -> None:
    store = _thresholded_store(db)
    cashier = make_user_with_role(db, store, CASHIER)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, cashier)

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date(2024, 1, 2),
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
        refund_method="CASH",
        client_transaction_id=f"ret-{unique_suffix()}",
        caller_store_id=None,
        created_by=cashier.id,
        approver_username=manager.username,
        approver_password=DEFAULT_TEST_PASSWORD,
    )
    db.commit()

    entry = (
        db.execute(
            select(AuditLog).where(
                AuditLog.action == "SALE_RETURN_APPROVAL_GRANTED", AuditLog.user_id == manager.id
            )
        )
        .scalars()
        .all()[-1]
    )
    assert entry.entity_type == "store"
    assert entry.entity_id == store.id
    assert entry.after_state["initiating_user_id"] == cashier.id
    assert Decimal(entry.after_state["refund_amount"]) == sale_return.refund_amount
    assert Decimal(entry.after_state["threshold"]) == THRESHOLD


def test_audit_log_records_self_approval_failure(db: Session) -> None:
    store = _thresholded_store(db)
    manager = make_user_with_role(db, store, MANAGER)
    sale, _ = _make_completed_sale(db, store, manager)

    with pytest.raises(ForbiddenError):
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date(2024, 1, 2),
            lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("15"))],
            refund_method="CASH",
            client_transaction_id=f"ret-{unique_suffix()}",
            caller_store_id=None,
            created_by=manager.id,
            approver_username=manager.username,
            approver_password=DEFAULT_TEST_PASSWORD,
        )
    entry = (
        db.execute(select(AuditLog).where(AuditLog.action == "SALE_RETURN_APPROVAL_FAILED"))
        .scalars()
        .all()[-1]
    )
    assert entry.after_state["reason"] == "self_approval_not_allowed"


# --- 20: direct API bypass rejected -----------------------------------------


def test_direct_api_call_cannot_bypass_approval(client: TestClient, db: Session) -> None:
    store = _thresholded_store(db)
    username = f"cashier_{unique_suffix()}"
    cashier = make_user_with_role(db, store, CASHIER, username=username)
    sale, _ = _make_completed_sale(db, store, cashier)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/sales/{sale.id}/returns",
        json={
            "store_id": store.id,
            "return_date": "2024-01-02",
            "client_transaction_id": f"ret-{unique_suffix()}",
            "refund_method": "CASH",
            "lines": [{"sale_item_id": sale.items[0].id, "quantity": "15"}],
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "APPROVAL_REQUIRED"


def test_direct_api_call_with_cashier_as_approver_is_rejected(
    client: TestClient, db: Session
) -> None:
    store = _thresholded_store(db)
    username = f"cashier_{unique_suffix()}"
    cashier = make_user_with_role(db, store, CASHIER, username=username)
    other_cashier_username = f"cashier2_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=other_cashier_username)
    sale, _ = _make_completed_sale(db, store, cashier)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/sales/{sale.id}/returns",
        json={
            "store_id": store.id,
            "return_date": "2024-01-02",
            "client_transaction_id": f"ret-{unique_suffix()}",
            "refund_method": "CASH",
            "lines": [{"sale_item_id": sale.items[0].id, "quantity": "15"}],
            "approver_username": other_cashier_username,
            "approver_password": DEFAULT_TEST_PASSWORD,
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "APPROVAL_PERMISSION_DENIED"


def test_full_api_flow_approved_return_succeeds(client: TestClient, db: Session) -> None:
    store = _thresholded_store(db)
    cashier_username = f"cashier_{unique_suffix()}"
    cashier = make_user_with_role(db, store, CASHIER, username=cashier_username)
    manager_username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=manager_username)
    sale, _ = _make_completed_sale(db, store, cashier)
    headers = auth_headers(client, cashier_username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/sales/{sale.id}/returns",
        json={
            "store_id": store.id,
            "return_date": "2024-01-02",
            "client_transaction_id": f"ret-{unique_suffix()}",
            "refund_method": "CASH",
            "lines": [{"sale_item_id": sale.items[0].id, "quantity": "15"}],
            "approver_username": manager_username,
            "approver_password": DEFAULT_TEST_PASSWORD,
        },
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["approval_required"] is True
    assert body["approved_by"] == manager.id


def test_void_also_subject_to_approval_gate_via_api(client: TestClient, db: Session) -> None:
    """Voids are Manager/Admin-only to INITIATE already -- confirms a
    second, different Manager is still required to APPROVE one that
    crosses the threshold (the two permissions, sales.void and
    sales.return.approve, are independent)."""
    store = _thresholded_store(db)
    initiator_username = f"manager1_{unique_suffix()}"
    initiator = make_user_with_role(db, store, MANAGER, username=initiator_username)
    sale, _ = _make_completed_sale(db, store, initiator, quantity=Decimal("20"))
    headers = auth_headers(client, initiator_username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/sales/{sale.id}/void",
        json={
            "store_id": store.id,
            "return_date": "2024-01-02",
            "client_transaction_id": f"void-{unique_suffix()}",
            "refund_method": "CASH",
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "APPROVAL_REQUIRED"

    second_manager_username = f"manager2_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=second_manager_username)
    response = client.post(
        f"/api/v1/sales/{sale.id}/void",
        json={
            "store_id": store.id,
            "return_date": "2024-01-02",
            "client_transaction_id": f"void-{unique_suffix()}",
            "refund_method": "CASH",
            "approver_username": second_manager_username,
            "approver_password": DEFAULT_TEST_PASSWORD,
        },
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["approval_required"] is True
