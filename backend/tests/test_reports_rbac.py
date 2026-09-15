"""M11 Phase 10: RBAC and multi-store security for the reports API.
Every report endpoint reuses an EXISTING read permission for its domain
(no new permission constants) — this file proves the actual grant
matrix behaves as app.modules.auth.permissions.ROLE_PERMISSIONS says,
and that store authorization is enforced at the route layer (a
store-scoped user can never see another store's data, with or without
an explicit store_id parameter), not merely assumed from the service-
layer tests in the other test_reports_*.py files.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import AUDITOR, CASHIER, HR_CLERK, INVENTORY_CLERK, MANAGER
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers

_TODAY = date.today().isoformat()

_REPORT_ENDPOINTS = [
    ("GET", "/api/v1/reports/sales/summary", {}),
    ("GET", "/api/v1/reports/sales/breakdown", {"dimension": "store"}),
    ("GET", "/api/v1/reports/sales/by-payment-method", {}),
    ("GET", "/api/v1/reports/financial/trial-balance", {}),
    ("GET", "/api/v1/reports/financial/profit-loss", {}),
    ("GET", "/api/v1/reports/financial/balance-sheet", {}),
    ("GET", "/api/v1/reports/inventory/value", {}),
    ("GET", "/api/v1/reports/inventory/stockouts", {}),
    ("GET", "/api/v1/reports/purchasing/spend", {}),
    ("GET", "/api/v1/reports/purchasing/po-fulfillment", {}),
    ("GET", "/api/v1/reports/payroll/headcount", {}),
    (
        "GET",
        "/api/v1/reports/dashboard",
        {"period_start": "2024-01-01", "period_end": "2024-01-31"},
    ),
]


def _seed_sale(db: Session, store, cashier) -> None:
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
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


# --- Role matrix: who can reach the reports API at all ----------------------


def test_cashier_cannot_access_any_report_endpoint(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    for method, path, params in _REPORT_ENDPOINTS:
        response = client.request(method, path, params=params, headers=headers)
        assert (
            response.status_code == 403
        ), f"{path} should be 403 for CASHIER, got {response.status_code}"


def test_inventory_clerk_can_reach_inventory_and_purchasing_but_not_sales_or_financial_or_payroll(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    username = f"invclerk_{unique_suffix()}"
    make_user_with_role(db, store, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    allowed = [
        ("GET", "/api/v1/reports/inventory/value", {}),
        ("GET", "/api/v1/reports/inventory/stockouts", {}),
        ("GET", "/api/v1/reports/purchasing/spend", {}),
        ("GET", "/api/v1/reports/purchasing/po-fulfillment", {}),
    ]
    denied = [
        ("GET", "/api/v1/reports/sales/summary", {}),
        ("GET", "/api/v1/reports/financial/trial-balance", {}),
        ("GET", "/api/v1/reports/financial/balance-sheet", {}),
        ("GET", "/api/v1/reports/payroll/headcount", {}),
    ]
    for method, path, params in allowed:
        response = client.request(method, path, params=params, headers=headers)
        assert response.status_code == 200, f"{path} should be 200 for INVENTORY_CLERK"
    for method, path, params in denied:
        response = client.request(method, path, params=params, headers=headers)
        assert response.status_code == 403, f"{path} should be 403 for INVENTORY_CLERK"


def test_hr_clerk_can_reach_payroll_reports_but_never_financial_or_sales(
    client: TestClient, db: Session
) -> None:
    """The highest-stakes role check in this file: an HR Clerk must be
    able to see payroll/labor reporting (their own scope) but must NEVER
    reach financial reports (accounting.read) or sales reports
    (reports.read) — those are not part of M10 decision #3's HR Clerk
    scope, and this is exactly the kind of accidental over-exposure this
    phase's brief warns against."""
    store = make_store(db)
    username = f"hrclerk_{unique_suffix()}"
    make_user_with_role(db, store, HR_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    payroll_response = client.get("/api/v1/reports/payroll/headcount", headers=headers)
    assert payroll_response.status_code == 200

    for path in [
        "/api/v1/reports/sales/summary",
        "/api/v1/reports/financial/trial-balance",
        "/api/v1/reports/financial/balance-sheet",
        "/api/v1/reports/inventory/value",
        "/api/v1/reports/purchasing/spend",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, f"{path} must be 403 for HR_CLERK"


def test_auditor_can_read_every_report_domain(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"auditor_{unique_suffix()}"
    make_user_with_role(db, store, AUDITOR, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    for method, path, params in _REPORT_ENDPOINTS:
        response = client.request(method, path, params=params, headers=headers)
        assert (
            response.status_code == 200
        ), f"{path} should be 200 for AUDITOR, got {response.status_code}: {response.text}"


def test_manager_can_read_every_report_domain(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    for method, path, params in _REPORT_ENDPOINTS:
        response = client.request(method, path, params=params, headers=headers)
        assert (
            response.status_code == 200
        ), f"{path} should be 200 for MANAGER, got {response.status_code}: {response.text}"


# --- Cross-store isolation at the route layer -------------------------------


def test_store_scoped_manager_cannot_request_another_stores_data_via_query_param(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        "/api/v1/reports/sales/summary", params={"store_id": store_b.id}, headers=headers
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STORE_ACCESS_DENIED"


def test_store_scoped_manager_requesting_multiple_stores_including_their_own_is_denied(
    client: TestClient, db: Session
) -> None:
    """A store-scoped caller supplying [own_store, other_store] must be
    rejected outright, not silently clamped to just their own store --
    the client-supplied list is validated, not filtered."""
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_b_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        "/api/v1/reports/sales/summary",
        params=[("store_id", store_a.id), ("store_id", store_b.id)],
        headers=headers,
    )
    assert response.status_code == 403


def test_store_scoped_manager_omitting_store_id_sees_only_their_own_store(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"mgr_c_{unique_suffix()}"
    manager = make_user_with_role(db, store_a, MANAGER, username=username)
    _seed_sale(db, store_a, manager)
    _seed_sale(db, store_b, manager)  # another store's data must never leak in
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/reports/sales/summary", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["gross_sales"] == "10.00"  # only store_a's one sale, never store_b's


def test_unrestricted_admin_company_wide_matches_sum_of_each_store(
    client: TestClient, db: Session
) -> None:
    """An unrestricted (company-wide) caller aggregating an EXPLICIT set
    of authorized stores must get exactly the sum of each store queried
    individually -- the same metric must aggregate correctly (this
    milestone's Phase 2 requirement).

    This deliberately queries an explicit store_id list for "company-wide"
    rather than omitting store_id entirely: `erp_dev` is a long-lived
    shared database that accumulates permanently-committed rows from
    other tests' real-commit concurrency scenarios (they bypass the `db`
    fixture's per-test rollback), so a genuinely unscoped "all stores in
    the universe" query is not a stable basis for an exact-equality
    assertion. Restricting to [store_a, store_b] still proves the
    aggregation-consistency property (sum of parts == whole) without
    depending on total database state.
    """
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"admin_{unique_suffix()}"
    from app.modules.auth.permissions import ADMIN

    admin = make_user_with_role(db, None, ADMIN, username=username)
    _seed_sale(db, store_a, admin)
    _seed_sale(db, store_b, admin)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    company_wide = client.get(
        "/api/v1/reports/sales/summary",
        params=[("store_id", store_a.id), ("store_id", store_b.id)],
        headers=headers,
    ).json()
    only_a = client.get(
        "/api/v1/reports/sales/summary", params={"store_id": store_a.id}, headers=headers
    ).json()
    only_b = client.get(
        "/api/v1/reports/sales/summary", params={"store_id": store_b.id}, headers=headers
    ).json()

    from decimal import Decimal as D

    assert D(company_wide["gross_sales"]) == D(only_a["gross_sales"]) + D(only_b["gross_sales"])


def test_unrestricted_admin_can_request_an_explicit_subset_of_stores(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    store_c = make_store(db)
    username = f"admin2_{unique_suffix()}"
    from app.modules.auth.permissions import ADMIN

    admin = make_user_with_role(db, None, ADMIN, username=username)
    _seed_sale(db, store_a, admin)
    _seed_sale(db, store_b, admin)
    _seed_sale(db, store_c, admin)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    subset = client.get(
        "/api/v1/reports/sales/summary",
        params=[("store_id", store_a.id), ("store_id", store_b.id)],
        headers=headers,
    ).json()
    from decimal import Decimal as D

    assert D(subset["gross_sales"]) == Decimal("20.00")  # a and b only, not c


def test_no_payroll_amount_leaks_into_a_403_error_body(client: TestClient, db: Session) -> None:
    """Sensitive-data leak check (Phase 13): a denied request's error
    body must never contain payroll figures, even accidentally."""
    store = make_store(db)
    username = f"cashier2_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        "/api/v1/reports/payroll/cost-summary",
        params={"period_start": "2024-01-01", "period_end": "2024-01-31"},
        headers=headers,
    )
    assert response.status_code == 403
    assert "gross_pay" not in response.text
    assert "net_pay" not in response.text
