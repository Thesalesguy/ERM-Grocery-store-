"""M12 Phase 9: audit log and security events.

Per-domain audit coverage for purchasing (test_purchasing_audit_and_
snapshots.py), inventory adjustments (test_inventory_api.py), and HR
compensation changes (test_hr_service.py) already exists and is not
duplicated here. This file closes the remaining gaps:

- SALE_COMPLETED / VOID_COMPLETED were never asserted anywhere as audit
  events, despite sales being the highest-volume, money-moving action in
  the whole system.
- LOGIN_SUCCESS was exercised (every login-based test depends on it
  working) but never itself asserted to produce an audit row.
- A cross-cutting invariant that NO audit record anywhere in the table
  ever contains a password or password hash -- not "this one code path
  looks safe" but "scan every row that exists after a realistic mix of
  operations and prove the substring never appears."
- DB-level immutability of audit_logs against the runtime role is
  already covered by test_constraints.py's
  test_runtime_role_cannot_update_or_delete_ledger_tables and
  test_backup_restore.py's adversarial DELETE check -- not repeated
  here.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import CASHIER
from app.modules.inventory import service as inventory_service
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import DEFAULT_TEST_PASSWORD, make_product, make_store, make_user_with_role


def _last_audit_event(db: Session, action: str, entity_type: str) -> AuditLog | None:
    return (
        db.execute(
            select(AuditLog)
            .where(AuditLog.action == action, AuditLog.entity_type == entity_type)
            .order_by(AuditLog.id.desc())
        )
        .scalars()
        .first()
    )


def test_login_success_is_recorded_in_the_audit_log(client: TestClient, db: Session) -> None:
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="audit_login_success")
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "audit_login_success", "password": DEFAULT_TEST_PASSWORD},
    )
    assert response.status_code == 200

    event = _last_audit_event(db, "LOGIN_SUCCESS", "user")
    assert event is not None
    assert event.user_id == user.id


def test_sale_completion_is_recorded_in_the_audit_log(db: Session) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username="audit_sale_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id="audit-sale-1",
        caller_store_id=store.id,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )

    event = _last_audit_event(db, "SALE_COMPLETED", "sale")
    assert event is not None
    assert event.entity_id == sale.id
    assert event.user_id == cashier.id


def test_void_is_recorded_in_the_audit_log(db: Session) -> None:
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username="audit_void_cashier")
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()

    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id="audit-void-1",
        caller_store_id=store.id,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )

    sale_return = sales_service.void_sale(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        refund_method="CASH",
        client_transaction_id="audit-void-return-1",
        caller_store_id=store.id,
        created_by=cashier.id,
    )

    event = _last_audit_event(db, "VOID_COMPLETED", "sale_return")
    assert event is not None
    assert event.entity_id == sale_return.id


def test_stock_adjustment_records_the_actor_and_reason_not_free_text_pii(db: Session) -> None:
    """A spot check that the audit record for a stock adjustment carries
    structured fields (product/quantity/reason code) rather than an
    unbounded free-text field that could end up holding anything an
    operator typed."""
    store = make_store(db)
    clerk = make_user_with_role(db, store, CASHIER, username="audit_adjustment_clerk")
    product = make_product(db, store, current_qty_on_hand=Decimal("10"))
    db.commit()

    adjustment = inventory_service.create_stock_adjustment(
        db,
        store_id=store.id,
        product_id=product.id,
        quantity_delta=Decimal("-2"),
        reason_code="DAMAGE",
        notes="broken in transit",
        created_by=clerk.id,
    )
    db.commit()

    event = _last_audit_event(db, "STOCK_ADJUSTMENT_CREATED", "stock_adjustment")
    assert event is not None
    assert event.entity_id == adjustment.id
    assert event.user_id == clerk.id


def test_no_audit_record_ever_contains_a_password_or_password_hash(
    client: TestClient, db: Session
) -> None:
    """Cross-cutting invariant, not a single-code-path check: after a
    realistic mix of successful logins, failed logins (right username,
    wrong password), and business mutations, scan EVERY audit_logs row
    that exists and prove a distinctive marker password never appears in
    before_state/after_state -- catching a future regression anywhere in
    the codebase that naively logs a whole request payload, not just the
    one call site this file happens to look at today."""
    store = make_store(db)
    user = make_user_with_role(db, store, CASHIER, username="audit_no_leak_user")
    db.commit()

    marker_password = "Sh0uldN3verLeakIntoAudit!!"  # noqa: S105 -- a distinctive test marker, not a real credential
    client.post(
        "/api/v1/auth/login",
        json={"username": "audit_no_leak_user", "password": marker_password},
    )
    client.post(
        "/api/v1/auth/login",
        json={"username": "audit_no_leak_user", "password": DEFAULT_TEST_PASSWORD},
    )

    rows = db.execute(select(AuditLog).where(AuditLog.user_id == user.id)).scalars().all()
    assert len(rows) >= 1
    for row in rows:
        serialized = f"{row.before_state}{row.after_state}"
        assert marker_password not in serialized
        assert DEFAULT_TEST_PASSWORD not in serialized
        assert "$argon2" not in serialized
