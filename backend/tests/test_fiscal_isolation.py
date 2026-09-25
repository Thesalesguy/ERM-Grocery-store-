"""M20 Session H: multi-store isolation for fiscal config/submissions,
at both the service layer and the real HTTP API (docs/M20_DESIGN.md
Section 7) -- mirrors tests/test_store_isolation.py's exact pattern.
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user,
    make_user_with_role,
    unique_suffix,
)
from tests.fiscal_fakes import FakeFiscalProvider
from tests.helpers import auth_headers

# --- Service-layer isolation -------------------------------------------


def test_fiscal_submission_created_for_correct_store_only(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = make_user(db, store_a)
    product_a = make_product(
        db, store_a, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store_a.id, is_enabled=True, provider_name="FAKE"))
    db.add(FiscalConfig(store_id=store_b.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sales_service.finalize_sale(
        db,
        store_id=store_a.id,
        cashier_id=cashier_a.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product_a.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()

    assert len(fiscal_service.list_submissions(db, store_id=store_a.id)) == 1
    assert len(fiscal_service.list_submissions(db, store_id=store_b.id)) == 0


def test_disabled_store_config_lookup_never_falls_through_to_another_stores_enabled_config(
    db: Session,
) -> None:
    """A stronger isolation proof than the sibling test above: store_a's
    config is DISABLED and store_b's is ENABLED. If the config lookup
    were ever not scoped by store_id (e.g. an unscoped query that
    happens to return SOME row), a sale for store_a could wrongly pick
    up store_b's enabled config and get fiscalized under the wrong
    store's settings. It must not."""
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_a = make_user(db, store_a)
    product_a = make_product(
        db, store_a, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store_a.id, is_enabled=False, provider_name="FAKE"))
    db.add(FiscalConfig(store_id=store_b.id, is_enabled=True, provider_name="FAKE"))
    db.commit()

    sales_service.finalize_sale(
        db,
        store_id=store_a.id,
        cashier_id=cashier_a.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product_a.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()

    assert fiscal_service.list_submissions(db, store_id=store_a.id) == []


def test_store_a_config_change_does_not_affect_store_b(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    admin = make_user(db, store_a)
    fiscal_service.upsert_config(
        db,
        store_id=store_a.id,
        is_enabled=True,
        provider_name="FAKE",
        credential_reference="STORE_A_KEY",
        submission_endpoint=None,
        retry_max_attempts=3,
        updated_by=admin.id,
    )
    db.commit()

    config_b = fiscal_service.get_config(db, store_b.id)
    assert config_b is None


# --- API-layer isolation -------------------------------------------------


def test_store_scoped_manager_cannot_read_another_stores_fiscal_config(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    db.add(FiscalConfig(store_id=store_b.id, is_enabled=True, provider_name="FAKE"))
    username = f"fiscal_mgr_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/fiscal/config", params={"store_id": store_b.id}, headers=headers)
    assert response.status_code == 404


def test_store_scoped_manager_cannot_configure_another_stores_fiscal_setup(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"fiscal_admin_{unique_suffix()}"
    # Admin permission set is required for config.write; scope it to
    # store_a to prove store scoping applies regardless of permission tier.
    make_user_with_role(db, store_a, ADMIN, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.put(
        "/api/v1/fiscal/config",
        json={
            "store_id": store_b.id,
            "is_enabled": True,
            "provider_name": "FAKE",
            "retry_max_attempts": 5,
        },
        headers=headers,
    )
    assert response.status_code == 404
    assert fiscal_service.get_config(db, store_b.id) is None


def test_store_scoped_cashier_lacks_fiscal_read_permission(client: TestClient, db: Session) -> None:
    store = make_store(db)
    username = f"fiscal_cashier_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/fiscal/config", params={"store_id": store.id}, headers=headers)
    assert response.status_code == 403


def test_store_scoped_manager_cannot_list_another_stores_submissions(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    username = f"fiscal_mgr2_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(
        "/api/v1/fiscal/submissions", params={"store_id": store_b.id}, headers=headers
    )
    assert response.status_code == 404


def test_store_scoped_manager_cannot_retry_another_stores_submission(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    cashier_b = make_user(db, store_b)
    product_b = make_product(
        db, store_b, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("100")
    )
    fake = FakeFiscalProvider(behavior="success")
    fiscal_service.register_provider("FAKE", fake)
    db.add(FiscalConfig(store_id=store_b.id, is_enabled=True, provider_name="FAKE"))
    db.commit()
    sales_service.finalize_sale(
        db,
        store_id=store_b.id,
        cashier_id=cashier_b.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product_b.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    [submission] = fiscal_service.list_submissions(db, store_id=store_b.id)

    username = f"fiscal_mgr3_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(f"/api/v1/fiscal/submissions/{submission.id}/retry", headers=headers)
    assert response.status_code == 404
    assert len(fake.calls) == 0
