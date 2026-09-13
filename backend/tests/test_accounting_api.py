"""Accounting API: RBAC (which roles can read/reverse), multi-store
isolation (a store-scoped user cannot read or reverse another store's
journal entries, and report totals respect store filtering), and basic
report-endpoint behavior.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, AUDITOR, CASHIER, MANAGER
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
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


def _post_a_sale(db: Session, store, cashier) -> None:
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("50"),
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()


# --- RBAC --------------------------------------------------------------------


def test_cashier_forbidden_from_accounting_endpoints(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/accounting/accounts", headers=headers)
    assert response.status_code == 403

    response = client.get("/api/v1/accounting/journals", headers=headers)
    assert response.status_code == 403


def test_manager_can_read_accounts_and_journals(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=username)
    _post_a_sale(db, store, manager)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/accounting/accounts", headers=headers)
    assert response.status_code == 200
    # 13 from M4 + 5 new M6 AP accounts (Bank Account, Accounts Payable,
    # Purchase Price Variance, Purchase Discounts, Purchase Tax Expense)
    # + 1 new M8 account (Inventory In Transit) + 6 new M10 payroll
    # accounts (Payroll Payable, Statutory Withholding Payable, Benefit/
    # Other Deduction Payable, Employer Contribution Payable, Wage &
    # Salary Expense, Employer Contribution Expense).
    assert len(response.json()) == 25

    response = client.get(f"/api/v1/accounting/journals?store_id={store.id}", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_manager_cannot_reverse_an_automated_sale_journal_via_the_api(
    client: TestClient, db: Session
) -> None:
    """docs/M4_HARDENING_AUDIT.md Section 1 (CRITICAL, fixed): even a
    permission-holding Manager must not be able to reverse an
    automatically-posted SALE journal — the API must surface the same
    409 OPERATIONAL_REVERSAL_REQUIRED the service layer raises, not a
    200. This is the regression test proving the fix actually reaches
    the HTTP boundary, not just the service function."""
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=username)
    _post_a_sale(db, store, manager)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/accounting/journals?store_id={store.id}", headers=headers)
    journal_id = response.json()[0]["id"]

    response = client.post(
        f"/api/v1/accounting/journals/{journal_id}/reverse",
        json={"reason": "manager attempts to reverse a sale"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OPERATIONAL_REVERSAL_REQUIRED"

    # No reversal exists and the entry is unchanged.
    response = client.get(f"/api/v1/accounting/journals/{journal_id}", headers=headers)
    assert response.json()["is_reversed"] is False


def test_auditor_can_read_but_not_reverse(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"auditor_{unique_suffix()}"
    auditor = make_user_with_role(db, store, AUDITOR, username=username)
    _post_a_sale(db, store, auditor)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/accounting/journals?store_id={store.id}", headers=headers)
    assert response.status_code == 200
    journal_id = response.json()[0]["id"]

    response = client.post(
        f"/api/v1/accounting/journals/{journal_id}/reverse",
        json={"reason": "should be denied"},
        headers=headers,
    )
    assert response.status_code == 403


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/accounting/accounts")
    assert response.status_code == 401


# --- Multi-store isolation ----------------------------------------------------


def test_store_scoped_manager_cannot_read_another_stores_journal(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username_a = f"manager_a_{unique_suffix()}"
    manager_a = make_user_with_role(db, store_a, MANAGER, username=username_a)
    username_b = f"manager_b_{unique_suffix()}"
    make_user_with_role(db, store_b, MANAGER, username=username_b)
    _post_a_sale(db, store_a, manager_a)

    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)
    response = client.get(f"/api/v1/accounting/journals?store_id={store_a.id}", headers=headers_a)
    journal_id = response.json()[0]["id"]

    headers_b = auth_headers(client, username_b, DEFAULT_TEST_PASSWORD)
    response = client.get(f"/api/v1/accounting/journals/{journal_id}", headers=headers_b)
    assert response.status_code == 404  # existence not leaked


def test_store_scoped_manager_cannot_reverse_another_stores_journal(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username_a = f"manager_a_{unique_suffix()}"
    manager_a = make_user_with_role(db, store_a, MANAGER, username=username_a)
    username_b = f"manager_b_{unique_suffix()}"
    make_user_with_role(db, store_b, MANAGER, username=username_b)
    _post_a_sale(db, store_a, manager_a)

    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)
    response = client.get(f"/api/v1/accounting/journals?store_id={store_a.id}", headers=headers_a)
    journal_id = response.json()[0]["id"]

    headers_b = auth_headers(client, username_b, DEFAULT_TEST_PASSWORD)
    response = client.post(
        f"/api/v1/accounting/journals/{journal_id}/reverse",
        json={"reason": "cross-store attempt"},
        headers=headers_b,
    )
    assert response.status_code == 403

    # Confirm no reversal was created and the manipulated store_id in the
    # URL cannot be used to post into another store's ledger either.
    response = client.get(f"/api/v1/accounting/journals?store_id={store_b.id}", headers=headers_b)
    assert response.json() == []


def test_store_scoped_manager_cannot_reverse_another_stores_manual_entry(
    client: TestClient, db: Session
) -> None:
    """The cross-store attempt above targets a SALE-sourced entry, which
    the automated-source block (Section 1) also refuses — masking
    whether the store-isolation check on its own is doing anything. This
    test targets a MANUAL entry instead (which the automated-source
    block does NOT refuse), isolating the store check as the only
    possible defense — and confirming (via a deliberate, reverted
    mutation during the M4 hardening audit that temporarily disabled
    _enforce_store_access) that removing it makes this exact test fail."""
    from datetime import date
    from decimal import Decimal

    from app.modules.accounting.service import _credit, _debit, _post_journal

    store_a = make_store(db)
    store_b = make_store(db)
    username_a = f"manager_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username_a)
    username_b = f"manager_b_{unique_suffix()}"
    make_user_with_role(db, store_b, MANAGER, username=username_b)
    manual_entry = _post_journal(
        db,
        store_id=store_a.id,
        posting_date=date(2024, 1, 1),
        source_type="MANUAL",
        source_id=None,
        memo="manual entry for store isolation test",
        created_by=None,
        lines=[_debit("1000", Decimal("5.00")), _credit("4000", Decimal("5.00"))],
    )
    db.commit()

    headers_b = auth_headers(client, username_b, DEFAULT_TEST_PASSWORD)
    response = client.post(
        f"/api/v1/accounting/journals/{manual_entry.id}/reverse",
        json={"reason": "cross-store attempt on a manual entry"},
        headers=headers_b,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_store_scoped_manager_only_sees_own_store_in_list_and_reports(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username_a = f"manager_a_{unique_suffix()}"
    manager_a = make_user_with_role(db, store_a, MANAGER, username=username_a)
    username_b = f"manager_b_{unique_suffix()}"
    manager_b = make_user_with_role(db, store_b, MANAGER, username=username_b)
    _post_a_sale(db, store_a, manager_a)
    _post_a_sale(db, store_b, manager_b)

    headers_a = auth_headers(client, username_a, DEFAULT_TEST_PASSWORD)
    response = client.get("/api/v1/accounting/journals", headers=headers_a)
    assert response.status_code == 200
    assert all(entry["store_id"] == store_a.id for entry in response.json())

    response = client.get("/api/v1/accounting/reports/profit-loss", headers=headers_a)
    assert response.status_code == 200
    pnl = response.json()
    assert Decimal(pnl["net_sales"]) == Decimal("10.00")  # only store_a's sale, not store_b's


def test_admin_cross_store_access_still_works(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    username_a = f"manager_a_{unique_suffix()}"
    manager_a = make_user_with_role(db, store_a, MANAGER, username=username_a)
    _post_a_sale(db, store_a, manager_a)

    admin_username = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=admin_username)
    db.commit()

    headers = auth_headers(client, admin_username, DEFAULT_TEST_PASSWORD)
    response = client.get(f"/api/v1/accounting/journals?store_id={store_a.id}", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


# --- Reports -------------------------------------------------------------


def test_trial_balance_report_endpoint(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=username)
    _post_a_sale(db, store, manager)
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/accounting/reports/trial-balance?store_id={store.id}", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_debit"] == body["total_credit"]


def test_inventory_reconciliation_report_endpoint(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    username = f"manager_{unique_suffix()}"
    manager = make_user_with_role(db, store, MANAGER, username=username)
    # Stock must originate through a real receipt (which posts a journal
    # entry) for reconciliation to be meaningful — a product whose
    # initial quantity was seeded directly (bypassing record_movement,
    # as _post_a_sale's helper does for RBAC-only tests) has no journal
    # history behind it and would show a false discrepancy; that is
    # correct behavior, not a bug (see docs/M4_ACCOUNTING_CORE.md Section 11).
    product = make_product(db, store, current_price=Decimal("10.00"))
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("20"),
        unit_cost=Decimal("3.000000"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("20"), Decimal("3.000000"))],
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=manager.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("2"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
    )
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        f"/api/v1/accounting/reports/inventory-reconciliation?store_id={store.id}", headers=headers
    )
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["discrepancy"] == "0.000000"
