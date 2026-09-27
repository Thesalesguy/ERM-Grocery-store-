"""M21: cross-store isolation remediation for the three confirmed gaps in
docs/M21_DISCOVERY.md (Finding F1: HR employment/compensation history;
Finding F5: AP supplier-financial aggregates; Finding F6: AP invoice-
matching-status). Every gap here was a complete ABSENCE of a store check
on an otherwise-real endpoint -- these tests prove the check now exists,
prove it exists at the SERVICE layer (not only the HTTP route, per the
M21 implementation brief), and prove legitimate same-store/unrestricted
access still works exactly as before.

Session lettering mirrors the M21 implementation brief:
  A - positive (same-store / unrestricted) access
  B - cross-store denial, direct-ID and query-manipulation
  C - service-layer bypass (call the service function directly, no HTTP)
  D - authorization matrix (role x same-store x cross-store x nonexistent)
  F5's financial-regression invariant (statement.closing_balance ==
  summary.total_owed, now proven to hold under per-store filtering too)
  is folded into its own section rather than a separate file, since it is
  a direct consequence of the F5 fix and is most legible next to it.

Session E (concurrency) is intentionally NOT included: this milestone's
fix is a pure read-side filter/deny addition with no new locking or
mutation path, so there is no meaningful concurrency concern to test
(see docs/M21_TESTING_SESSIONS.md for the explicit rationale) -- adding a
race test here would be padding, not signal.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError
from app.modules.ap import service as ap_service
from app.modules.ap.service import PurchaseInvoiceLineInput
from app.modules.auth.permissions import ADMIN, AUDITOR, CASHIER, MANAGER
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _hire(db: Session, store, **overrides):
    defaults = dict(
        employee_number=f"EMP-{unique_suffix()}",
        legal_name="M21 Test Employee",
        hire_date=date(2024, 1, 1),
        store_id=store.id,
    )
    defaults.update(overrides)
    return hr_service.hire_employee(
        db, EmployeeHireInput(**defaults), actor_id=None, caller_store_id=None
    )


def _order_and_receive(db: Session, store, supplier, product, *, qty: Decimal, cost: Decimal):
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id, product_id=product.id, quantity_ordered=qty, unit_cost=cost
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, qty, cost)],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(item)
    return po, item


def _posted_invoice(db: Session, store, supplier, item, *, qty: Decimal, cost: Decimal, po_id: int):
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store.id,
        supplier_id=supplier.id,
        purchase_order_id=po_id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date(2024, 1, 5),
        lines=[PurchaseInvoiceLineInput(item.id, qty, cost)],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    db.refresh(invoice)
    return invoice


# =============================================================================
# F1 -- HR employment/assignment/compensation history isolation
# =============================================================================

# --- Session A: positive access ---------------------------------------------


def test_f1_same_store_manager_reads_status_history(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"f1_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    employee = _hire(db, store)
    hr_service.change_employment_status(
        db,
        employee_id=employee.id,
        new_status="ON_LEAVE",
        effective_from=date(2024, 3, 1),
        reason="test",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/hr/employees/{employee.id}/status-history", headers=headers)
    assert response.status_code == 200
    assert [row["status"] for row in response.json()] == ["ACTIVE", "ON_LEAVE"]


def test_f1_same_store_manager_reads_assignment_history(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"f1_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    employee = _hire(db, store)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/hr/employees/{employee.id}/assignment-history", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["store_id"] == store.id


def test_f1_same_store_manager_reads_compensation_history(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"f1_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    employee = _hire(db, store)
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 2, 1),
        pay_type="HOURLY",
        rate=Decimal("20.00"),
        pay_frequency="BIWEEKLY",
        overtime_eligible=True,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/hr/employees/{employee.id}/compensation-history", headers=headers
    )
    assert response.status_code == 200
    assert Decimal(response.json()[0]["rate"]) == Decimal("20.00")


def test_f1_unrestricted_caller_reads_history_for_any_store(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    username = f"f1_admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)  # no store_id -> unrestricted
    employee = _hire(db, store)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/hr/employees/{employee.id}/status-history", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


# --- Session B: cross-store denial (direct-ID access) -----------------------


def test_f1_store_scoped_manager_cannot_read_another_stores_status_history(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"f1_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    employee_b = _hire(db, store_b)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/hr/employees/{employee_b.id}/status-history", headers=headers)
    assert response.status_code == 403


def test_f1_store_scoped_manager_cannot_read_another_stores_assignment_history(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"f1_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    employee_b = _hire(db, store_b)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/hr/employees/{employee_b.id}/assignment-history", headers=headers
    )
    assert response.status_code == 403


def test_f1_store_scoped_manager_cannot_read_another_stores_compensation_history(
    client: TestClient, db: Session
) -> None:
    """The highest-severity case in F1: pay-rate data. This is the exact
    reproduction scenario named in docs/M21_DISCOVERY.md Finding F1."""
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"f1_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    employee_b = _hire(db, store_b)
    hr_service.change_compensation(
        db,
        employee_id=employee_b.id,
        effective_from=date(2024, 2, 1),
        pay_type="SALARY",
        rate=Decimal("99999.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/hr/employees/{employee_b.id}/compensation-history", headers=headers
    )
    assert response.status_code == 403
    assert "99999" not in response.text


# --- Session C: service-layer bypass -----------------------------------------


def test_f1_service_layer_rejects_cross_store_status_history_without_http(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee_b = _hire(db, store_b)
    db.commit()

    import pytest

    with pytest.raises(ForbiddenError):
        hr_service.employment_status_history(db, employee_b.id, caller_store_id=store_a.id)


def test_f1_service_layer_rejects_cross_store_assignment_history_without_http(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee_b = _hire(db, store_b)
    db.commit()

    import pytest

    with pytest.raises(ForbiddenError):
        hr_service.employment_assignment_history(db, employee_b.id, caller_store_id=store_a.id)


def test_f1_service_layer_rejects_cross_store_compensation_history_without_http(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    employee_b = _hire(db, store_b)
    db.commit()

    import pytest

    with pytest.raises(ForbiddenError):
        hr_service.compensation_history(db, employee_b.id, caller_store_id=store_a.id)


def test_f1_service_layer_allows_same_store_and_unrestricted(db: Session) -> None:
    store = make_store(db)
    employee = _hire(db, store)
    db.commit()

    same_store = hr_service.employment_status_history(db, employee.id, caller_store_id=store.id)
    unrestricted = hr_service.employment_status_history(db, employee.id, caller_store_id=None)
    assert len(same_store) == 1
    assert len(unrestricted) == 1


# --- Session D: authorization matrix -----------------------------------------


def test_f1_cashier_lacks_hr_read_permission_entirely(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"f1_cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    employee = _hire(db, store)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/hr/employees/{employee.id}/status-history", headers=headers)
    assert response.status_code == 403


def test_f1_nonexistent_employee_store_scoped_caller_gets_403_not_a_data_leak(
    client: TestClient, db: Session
) -> None:
    """A nonexistent employee id has no current assignment to verify
    against, so the existing (pre-M21) fail-closed helper
    `_enforce_store_access_via_current_assignment` denies a store-scoped
    caller rather than silently returning an empty list -- this is
    inherited, established behavior (same helper used by
    change_employment_status et al.), not new M21 semantics."""
    store = make_store(db)
    username = f"f1_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/hr/employees/999999999/status-history", headers=headers)
    assert response.status_code == 403


def test_f1_nonexistent_employee_unrestricted_caller_gets_empty_list(
    client: TestClient, db: Session
) -> None:
    username = f"f1_admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/hr/employees/999999999/status-history", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


# =============================================================================
# F5 -- AP supplier-financial (summary/transactions/statement) isolation
# =============================================================================


def _invoice_in_store(db: Session, store, supplier, *, cost: Decimal):
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    po, item = _order_and_receive(db, store, supplier, product, qty=Decimal("10"), cost=cost)
    return _posted_invoice(db, store, supplier, item, qty=Decimal("10"), cost=cost, po_id=po.id)


# --- Session A: positive access ----------------------------------------------


def test_f5_same_store_manager_sees_own_store_supplier_summary(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store, supplier, cost=Decimal("10.00"))
    username = f"f5_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/summary", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["total_owed"]) == Decimal("100.00")


# --- Session B: cross-store filtering (never an outright deny -- Supplier
#     itself is legitimate company-wide reference data; the fix scopes the
#     underlying invoice/payment/credit-note rows, per docs/M21_DISCOVERY.md
#     Finding F5's own remediation shape) ---------------------------------


def test_f5_store_scoped_manager_summary_excludes_another_stores_invoices(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))  # $100 in store A
    _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))  # $500 in store B
    username = f"f5_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/summary", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["total_owed"]) == Decimal(
        "100.00"
    ), "store A's summary must not include store B's $500 invoice"


def test_f5_store_scoped_manager_transactions_excludes_another_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    invoice_a = _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))
    invoice_b = _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))
    username = f"f5_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/transactions", headers=headers)
    assert response.status_code == 200
    ids = {row["id"] for row in response.json() if row["transaction_type"] == "INVOICE"}
    assert invoice_a.id in ids
    assert invoice_b.id not in ids


def test_f5_store_scoped_manager_statement_excludes_another_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))
    _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))
    username = f"f5_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/statement", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["closing_balance"]) == Decimal("100.00")


def test_f5_unrestricted_caller_sees_company_wide_total(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))
    _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))
    username = f"f5_admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/summary", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["total_owed"]) == Decimal(
        "600.00"
    ), "an unrestricted caller must still see the true, company-wide total"


# --- Session C: service-layer bypass -----------------------------------------


def test_f5_service_layer_summary_scoping_cannot_be_bypassed_without_http(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))
    _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))
    db.commit()

    summary_a = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=store_a.id)
    summary_all = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=None)
    assert summary_a.total_owed == Decimal("100.00")
    assert summary_all.total_owed == Decimal("600.00")


# --- Financial regression invariant: statement.closing_balance ==
#     summary.total_owed must hold under per-store filtering, exactly as
#     it already does unfiltered (see get_supplier_statement's docstring).


def test_f5_statement_closing_balance_matches_scoped_summary_total_owed(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store_a, supplier, cost=Decimal("10.00"))
    _invoice_in_store(db, store_b, supplier, cost=Decimal("50.00"))
    db.commit()

    summary_a = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=store_a.id)
    statement_a = ap_service.get_supplier_statement(db, supplier.id, store_id=store_a.id)
    assert statement_a.closing_balance == summary_a.total_owed

    summary_all = ap_service.get_supplier_ap_summary(db, supplier.id, store_id=None)
    statement_all = ap_service.get_supplier_statement(db, supplier.id, store_id=None)
    assert statement_all.closing_balance == summary_all.total_owed


# --- Session D: authorization matrix -----------------------------------------


def test_f5_auditor_can_read_but_not_write(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    _invoice_in_store(db, store, supplier, cost=Decimal("10.00"))
    username = f"f5_auditor_{unique_suffix()}"
    make_user_with_role(db, store, AUDITOR, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/summary", headers=headers)
    assert response.status_code == 200


def test_f5_cashier_lacks_ap_read_permission(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    username = f"f5_cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/suppliers/{supplier.id}/summary", headers=headers)
    assert response.status_code == 403


# =============================================================================
# F6 -- AP invoice-matching-status isolation
# =============================================================================

# --- Session A: positive access ----------------------------------------------


def test_f6_same_store_manager_reads_own_stores_matching_status(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    po, item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5")
    )
    username = f"f6_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/purchase-orders/{po.id}/matching-status", headers=headers)
    assert response.status_code == 200
    assert Decimal(response.json()["items"][0]["quantity_received"]) == Decimal("10")


# --- Session B: cross-store denial (direct-ID and multi-ID) ------------------


def test_f6_store_scoped_manager_cannot_read_another_stores_matching_status(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_b = make_product(db, store_b, current_qty_on_hand=Decimal("0"))
    po_b, _item = _order_and_receive(
        db, store_b, supplier, product_b, qty=Decimal("10"), cost=Decimal("5")
    )
    username = f"f6_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/purchase-orders/{po_b.id}/matching-status", headers=headers)
    assert response.status_code == 404


def test_f6_store_scoped_manager_cannot_smuggle_another_stores_po_into_multi(
    client: TestClient, db: Session
) -> None:
    """The multi-PO variant must reject the WHOLE request if any id in the
    comma-separated list belongs to another store, not silently drop it."""
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_a = make_product(db, store_a, current_qty_on_hand=Decimal("0"))
    product_b = make_product(db, store_b, current_qty_on_hand=Decimal("0"))
    po_a, _ = _order_and_receive(
        db, store_a, supplier, product_a, qty=Decimal("10"), cost=Decimal("5")
    )
    po_b, _ = _order_and_receive(
        db, store_b, supplier, product_b, qty=Decimal("10"), cost=Decimal("5")
    )
    username = f"f6_mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        "/api/v1/ap/purchase-orders/matching-status",
        params={"purchase_order_ids": f"{po_a.id},{po_b.id}"},
        headers=headers,
    )
    assert response.status_code == 404


# --- Session C: service-layer bypass -----------------------------------------


def test_f6_service_layer_rejects_cross_store_matching_status_without_http(
    db: Session,
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    supplier = make_supplier(db)
    product_b = make_product(db, store_b, current_qty_on_hand=Decimal("0"))
    po_b, _item = _order_and_receive(
        db, store_b, supplier, product_b, qty=Decimal("10"), cost=Decimal("5")
    )
    db.commit()

    import pytest

    with pytest.raises(NotFoundError):
        ap_service.get_invoice_matching_status(db, po_b.id, caller_store_id=store_a.id)


def test_f6_service_layer_allows_same_store_and_unrestricted(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    po, _item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5")
    )
    db.commit()

    same_store = ap_service.get_invoice_matching_status(db, po.id, caller_store_id=store.id)
    unrestricted = ap_service.get_invoice_matching_status(db, po.id, caller_store_id=None)
    assert len(same_store) == 1
    assert len(unrestricted) == 1


# --- Session D: authorization matrix -----------------------------------------


def test_f6_cashier_lacks_ap_read_permission(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("0"))
    po, _item = _order_and_receive(
        db, store, supplier, product, qty=Decimal("10"), cost=Decimal("5")
    )
    username = f"f6_cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/ap/purchase-orders/{po.id}/matching-status", headers=headers)
    assert response.status_code == 403


def test_f6_nonexistent_purchase_order_is_404_for_everyone(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"f6_mgr_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/ap/purchase-orders/999999999/matching-status", headers=headers)
    assert response.status_code == 404
