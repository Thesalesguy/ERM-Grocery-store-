"""M15: cashier/till shift session functional behavior, financial
invariants, and security/adversarial coverage.

See docs/M15_DESIGN.md for the full design. This file covers Testing
Sessions D (functional), E (financial invariants), and H (security/
adversarial) from docs/M15_TESTING_SESSIONS.md. Concurrency and failure
injection are covered separately in tests/test_shifts_concurrency.py and
tests/test_shifts_failure_injection.py, mirroring how M5/M9/M14 split
those concerns into their own files.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry, JournalLine
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import CASHIER, INVENTORY_CLERK, MANAGER
from app.modules.shifts import service as shifts_service
from app.modules.shifts.models import CashierShift
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _cashier(db: Session, store, username: str | None = None):
    username = username or f"cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    return username


def _manager(db: Session, store, username: str | None = None):
    username = username or f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    return username


def _open_shift(client: TestClient, headers: dict, store_id: int, opening_float: str = "100.00"):
    resp = client.post(
        "/api/v1/shifts",
        headers=headers,
        json={
            "store_id": store_id,
            "opening_float": opening_float,
            "client_transaction_id": f"shift-open-{unique_suffix()}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ring_cash_sale(
    client: TestClient, headers: dict, store_id: int, product_id: int, *, price: str, tendered: str
):
    resp = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store_id,
            "client_transaction_id": f"sale-{unique_suffix()}",
            "lines": [{"product_id": product_id, "quantity": "1"}],
            "payments": [{"payment_method": "CASH", "amount": tendered}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Session D: functional behavior ---------------------------------------


def test_open_shift_creates_active_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers, store.id, "50.00")
    assert shift["status"] == "OPEN"
    assert shift["opening_float"] == "50.00"
    assert shift["closed_at"] is None

    active = client.get("/api/v1/shifts/active", headers=headers)
    assert active.status_code == 200
    assert active.json()["id"] == shift["id"]


def test_open_shift_negative_float_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.post(
        "/api/v1/shifts",
        headers=headers,
        json={
            "store_id": store.id,
            "opening_float": "-1.00",
            "client_transaction_id": f"shift-{unique_suffix()}",
        },
    )
    assert resp.status_code == 422  # Pydantic Field(ge=0) rejects it


def test_active_shift_endpoint_returns_null_when_none_open(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.get("/api/v1/shifts/active", headers=headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_cannot_open_second_shift_while_one_active(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    _open_shift(client, headers, store.id)

    resp = client.post(
        "/api/v1/shifts",
        headers=headers,
        json={
            "store_id": store.id,
            "opening_float": "20.00",
            "client_transaction_id": f"shift-{unique_suffix()}",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SHIFT_ALREADY_OPEN"


def test_open_shift_duplicate_key_returns_same_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    key = f"shift-{unique_suffix()}"
    payload = {"store_id": store.id, "opening_float": "30.00", "client_transaction_id": key}
    first = client.post("/api/v1/shifts", headers=headers, json=payload)
    second = client.post("/api/v1/shifts", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_cash_sale_attributed_to_active_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers, store.id)
    sale = _ring_cash_sale(client, headers, store.id, product.id, price="10.00", tendered="10.00")

    from app.modules.sales.models import Sale

    db_sale = db.get(Sale, sale["id"])
    assert db_sale.shift_id == shift["id"]


def test_sale_without_active_shift_has_null_shift_id(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    sale = _ring_cash_sale(client, headers, store.id, product.id, price="10.00", tendered="10.00")

    from app.modules.sales.models import Sale

    db_sale = db.get(Sale, sale["id"])
    assert db_sale.shift_id is None


def test_cash_movement_paid_in_and_paid_out(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    paid_in = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers,
        json={
            "movement_type": "PAID_IN",
            "amount": "20.00",
            "reason": "manager top-up",
            "client_transaction_id": f"mv-{unique_suffix()}",
        },
    )
    assert paid_in.status_code == 201

    paid_out = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers,
        json={
            "movement_type": "PAID_OUT",
            "amount": "5.00",
            "reason": "courier payment",
            "client_transaction_id": f"mv-{unique_suffix()}",
        },
    )
    assert paid_out.status_code == 201

    movements = client.get(f"/api/v1/shifts/{shift['id']}/cash-movements", headers=headers)
    assert len(movements.json()) == 2


def test_cash_movement_audit_event_identifies_the_exact_movement_row(
    client: TestClient, db: Session
) -> None:
    """M16 Phase 0 item 6: the audit event for CASH_MOVEMENT_CREATED
    previously captured only movement_type/amount/reason, keyed to the
    parent shift's entity_id -- a shift's cash-movement history was
    reconstructable in substance from audit_logs alone, but not tied
    deterministically to one specific cash_movements row. Proves the
    movement's own id and client_transaction_id are now present."""
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    key = f"mv-{unique_suffix()}"
    paid_in = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers,
        json={
            "movement_type": "PAID_IN",
            "amount": "20.00",
            "reason": "manager top-up",
            "client_transaction_id": key,
        },
    )
    assert paid_in.status_code == 201
    movement_id = paid_in.json()["id"]

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "CASH_MOVEMENT_CREATED", AuditLog.entity_id == shift["id"]
        )
    ).scalar_one()
    assert audit_row.after_state["movement_id"] == movement_id
    assert audit_row.after_state["client_transaction_id"] == key


def test_cash_movement_rejected_on_closed_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers,
        json={
            "movement_type": "PAID_IN",
            "amount": "10.00",
            "reason": "too late",
            "client_transaction_id": f"mv-{unique_suffix()}",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SHIFT_NOT_OPEN"


def test_cash_movement_non_positive_amount_rejected(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers,
        json={
            "movement_type": "PAID_IN",
            "amount": "0",
            "reason": "invalid",
            "client_transaction_id": f"mv-{unique_suffix()}",
        },
    )
    assert resp.status_code == 422


def test_close_already_closed_shift_conflict(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    second = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "SHIFT_NOT_OPEN"


def test_shift_close_idempotent_retry_returns_same_result(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    key = f"close-{unique_suffix()}"
    payload = {"closing_counted_amount": shift["opening_float"], "client_transaction_id": key}
    first = client.post(f"/api/v1/shifts/{shift['id']}/close", headers=headers, json=payload)
    second = client.post(f"/api/v1/shifts/{shift['id']}/close", headers=headers, json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


# --- Session E: financial invariants ---------------------------------------


def test_close_shift_exact_reconciliation_zero_variance(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "100.00")

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "100.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert close.status_code == 200
    body = close.json()
    assert body["expected_cash_amount"] == "100.00"
    assert body["variance_amount"] == "0.00"


def test_close_shift_positive_variance_overage_posts_gl(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "100.00")

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "110.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert close.status_code == 200
    body = close.json()
    assert body["variance_amount"] == "10.00"

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "CASH_SHIFT_VARIANCE", JournalEntry.source_id == shift["id"]
        )
    ).scalar_one()
    lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) == Decimal("10.000000")


def test_cash_shift_variance_cannot_be_reversed_via_generic_journal_reversal(
    client: TestClient, db: Session
) -> None:
    """M16 Phase 0 item 2: the discovery audit claimed CASH_SHIFT_VARIANCE
    was missing from AUTOMATED_SOURCE_TYPES; re-validating against the
    current code (accounting/models.py) showed it was already present and
    the generic reverse_journal_entry gate already refused it correctly.
    This is the regression coverage proving that invariant directly,
    since no prior test exercised this specific path."""
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "100.00")

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "110.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert close.status_code == 200

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "CASH_SHIFT_VARIANCE", JournalEntry.source_id == shift["id"]
        )
    ).scalar_one()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=entry.id,
            reason="Attempting to reverse a shift variance entry directly",
            reversed_by=1,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


def test_close_shift_negative_variance_shortage_posts_gl(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "100.00")

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "92.50",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert close.status_code == 200
    body = close.json()
    assert body["variance_amount"] == "-7.50"

    entry = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "CASH_SHIFT_VARIANCE", JournalEntry.source_id == shift["id"]
        )
    ).scalar_one()
    lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    assert sum(line.debit for line in lines) == Decimal("7.500000")


def test_close_shift_no_gl_entry_on_exact_reconciliation(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "50.00")

    client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "50.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    count = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "CASH_SHIFT_VARIANCE",
                JournalEntry.source_id == shift["id"],
            )
        )
        .scalars()
        .all()
    )
    assert len(count) == 0


def test_expected_cash_handles_change_correctly(client: TestClient, db: Session) -> None:
    """The task's own worked example: a $100 sale tendered with $150
    cash increases physical cash by $100, not $150 — proven end to end
    through a real sale + shift close, not just the formula in
    isolation."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("100.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "0.00")

    sale = _ring_cash_sale(client, headers, store.id, product.id, price="100.00", tendered="150.00")
    assert sale["change_due"] == "50.00"

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "100.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert close.status_code == 200
    body = close.json()
    assert body["expected_cash_amount"] == "100.00"
    assert body["variance_amount"] == "0.00"


def test_expected_cash_excludes_noncash_tender(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("40.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "0.00")

    card_sale = client.post(
        "/api/v1/sales",
        headers=headers,
        json={
            "store_id": store.id,
            "client_transaction_id": f"sale-{unique_suffix()}",
            "lines": [{"product_id": product.id, "quantity": "1"}],
            "payments": [{"payment_method": "CARD", "amount": "40.00"}],
        },
    )
    assert card_sale.status_code == 201

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "0.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    body = close.json()
    # A card sale must not inflate expected physical cash at all.
    assert body["expected_cash_amount"] == "0.00"
    assert body["variance_amount"] == "0.00"


def test_expected_cash_includes_cash_refund_reduction(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "0.00")

    sale = _ring_cash_sale(client, headers, store.id, product.id, price="20.00", tendered="20.00")

    return_resp = client.post(
        f"/api/v1/sales/{sale['id']}/returns",
        headers=headers,
        json={
            "store_id": store.id,
            "return_date": "2024-01-01",
            "client_transaction_id": f"ret-{unique_suffix()}",
            "refund_method": "CASH",
            "lines": [{"sale_item_id": sale["items"][0]["id"], "quantity": "1"}],
        },
    )
    assert return_resp.status_code == 201

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "0.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    body = close.json()
    # +20 from the sale, -20 from the cash refund = net 0 expected.
    assert body["expected_cash_amount"] == "0.00"
    assert body["variance_amount"] == "0.00"


def test_expected_cash_still_reflects_original_cash_tender_when_refund_is_noncash(
    client: TestClient, db: Session
) -> None:
    """M16 Phase 0 item 7: the discovery audit flagged a plausible edge
    case -- a CASH sale later returned/voided via a NON-CASH refund_method
    leaves the original cash tender counted with nothing to subtract it,
    since _compute_expected_cash's cash_refunded term only fires for
    refund_method == 'CASH'.

    Traced end to end (docs/M16_DESIGN.md "Expected-cash/status
    investigation"): this is CORRECT, not a bug. `expected_cash_amount`
    models the physical till only -- when a customer paid $20 cash, that
    $20 genuinely entered the drawer. If the refund is later issued via
    bank transfer (not from the drawer), no cash physically leaves the
    till, so the till legitimately still holds that $20. The apparent
    "overstatement" is the till accurately reflecting reality: the
    physical cash was never given back. This test proves that intended
    behavior directly, rather than assuming the audit's suspicion was a
    confirmed defect."""
    store = make_store(db)
    product = make_product(
        db, store, current_price=Decimal("20.00"), current_qty_on_hand=Decimal("10")
    )
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id, "0.00")

    sale = _ring_cash_sale(client, headers, store.id, product.id, price="20.00", tendered="20.00")

    return_resp = client.post(
        f"/api/v1/sales/{sale['id']}/returns",
        headers=headers,
        json={
            "store_id": store.id,
            "return_date": "2024-01-01",
            "client_transaction_id": f"ret-{unique_suffix()}",
            "refund_method": "BANK_TRANSFER",
            "lines": [{"sale_item_id": sale["items"][0]["id"], "quantity": "1"}],
        },
    )
    assert return_resp.status_code == 201

    close = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": "20.00",
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    body = close.json()
    # +20 from the cash sale; the bank-transfer refund never touches the
    # till, so expected cash is still 20 -- and the physical count of
    # 20.00 (the cashier genuinely still has that $20 in the drawer)
    # matches it exactly, zero variance.
    assert body["expected_cash_amount"] == "20.00"
    assert body["variance_amount"] == "0.00"


def test_audit_log_records_shift_opened_and_closed(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = _cashier(db, store)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers, store.id)

    client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )

    events = (
        db.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "cashier_shift", AuditLog.entity_id == shift["id"]
            )
        )
        .scalars()
        .all()
    )
    actions = {e.action for e in events}
    assert "SHIFT_OPENED" in actions
    assert "SHIFT_CLOSE_INITIATED" in actions
    assert "SHIFT_CLOSED" in actions


# --- Session H: security / adversarial --------------------------------------


def test_cashier_cannot_record_movement_on_another_cashiers_shift(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    cashier_a = _cashier(db, store, "shift_movement_owner")
    cashier_b = _cashier(db, store, "shift_movement_other")
    headers_a = auth_headers(client, cashier_a, DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, cashier_b, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers_a, store.id)

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/cash-movements",
        headers=headers_b,
        json={
            "movement_type": "PAID_IN",
            "amount": "5.00",
            "reason": "not my till",
            "client_transaction_id": f"mv-{unique_suffix()}",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SHIFT_OVERRIDE_REQUIRED"


def test_unauthorized_role_cannot_open_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"clerk_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    resp = client.post(
        "/api/v1/shifts",
        headers=headers,
        json={
            "store_id": store.id,
            "opening_float": "10.00",
            "client_transaction_id": f"shift-{unique_suffix()}",
        },
    )
    assert resp.status_code == 403


def test_cashier_cannot_close_another_cashiers_shift(client: TestClient, db: Session) -> None:
    store = make_store(db)
    cashier_a = _cashier(db, store, "shift_owner_a")
    cashier_b = _cashier(db, store, "shift_other_b")
    headers_a = auth_headers(client, cashier_a, DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, cashier_b, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers_a, store.id)

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers_b,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SHIFT_OVERRIDE_REQUIRED"


def test_manager_can_close_other_cashiers_shift_with_override(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    cashier = _cashier(db, store)
    manager = _manager(db, store)
    cashier_headers = auth_headers(client, cashier, DEFAULT_TEST_PASSWORD)
    manager_headers = auth_headers(client, manager, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, cashier_headers, store.id)

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=manager_headers,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["closed_by"] is not None


def test_cross_store_cashier_cannot_view_another_stores_shift(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = _cashier(db, store_a)
    cashier_b = _cashier(db, store_b)
    headers_a = auth_headers(client, cashier_a, DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, cashier_b, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers_a, store_a.id)

    resp = client.get(f"/api/v1/shifts/{shift['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_direct_service_call_to_get_shift_and_list_shifts_enforces_store_isolation(
    client: TestClient, db: Session
) -> None:
    """M16 Phase 0 item 5: get_shift/list_shifts previously enforced
    store isolation only at their (still-present) endpoint layer, unlike
    open_shift/record_cash_movement/close_shift which all check in the
    service function itself. Proves the service layer now backstops a
    direct caller too, without changing either endpoint's observable
    behavior (both still 404, per the pre-existing test above)."""
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = _cashier(db, store_a)
    headers_a = auth_headers(client, cashier_a, DEFAULT_TEST_PASSWORD)
    shift = _open_shift(client, headers_a, store_a.id)

    with pytest.raises(NotFoundError):
        shifts_service.get_shift(db, shift["id"], caller_store_id=store_b.id)

    results = shifts_service.list_shifts(db, caller_store_id=store_b.id)
    assert all(s.id != shift["id"] for s in results)

    # A caller correctly scoped to the shift's own store still sees it.
    own_store_results = shifts_service.list_shifts(db, caller_store_id=store_a.id)
    assert any(s.id == shift["id"] for s in own_store_results)


def test_cross_store_manager_cannot_override_close(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = _cashier(db, store_a)
    manager_b = _manager(db, store_b)
    headers_a = auth_headers(client, cashier_a, DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, manager_b, DEFAULT_TEST_PASSWORD)

    shift = _open_shift(client, headers_a, store_a.id)

    resp = client.post(
        f"/api/v1/shifts/{shift['id']}/close",
        headers=headers_b,
        json={
            "closing_counted_amount": shift["opening_float"],
            "client_transaction_id": f"close-{unique_suffix()}",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_direct_service_call_cannot_bypass_override_check(db: Session) -> None:
    """Adversarial: call app.modules.shifts.service.close_shift directly
    (bypassing the HTTP endpoint's permission dependency entirely) — the
    self-vs-override authorization must still be enforced, proving it
    lives in the service layer, not merely at the route."""
    store = make_store(db)
    cashier = make_user_with_role(db, store, CASHIER, username=f"c_{unique_suffix()}")
    other_cashier = make_user_with_role(db, store, CASHIER, username=f"c2_{unique_suffix()}")
    db.commit()

    shift = shifts_service.open_shift(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        opening_float=Decimal("10.00"),
        client_transaction_id=f"shift-{unique_suffix()}",
        caller_store_id=store.id,
    )
    db.commit()

    with pytest.raises(Exception) as exc_info:
        shifts_service.close_shift(
            db,
            shift_id=shift.id,
            closing_counted_amount=Decimal("10.00"),
            actor_id=other_cashier.id,
            caller_store_id=store.id,
            client_transaction_id=f"close-{unique_suffix()}",
        )
    assert getattr(exc_info.value, "error_code", None) == "SHIFT_OVERRIDE_REQUIRED"

    db.rollback()
    refreshed = db.get(CashierShift, shift.id)
    assert refreshed.status == "OPEN"
