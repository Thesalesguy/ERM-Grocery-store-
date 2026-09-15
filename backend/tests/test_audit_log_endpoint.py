"""M13 Phase 7: GET /api/v1/audit-log.

Tests the READ side's authorization and store-scoping only -- that
audit events themselves are correctly recorded is already covered
per-domain elsewhere (test_audit_hardening.py and others; see that
file's module docstring). Audit rows here are created directly via
audit_service.log_event against real entities (real products in real
stores) so the store-resolution joins in app.modules.audit.service
are exercised against real data, not fabricated entity_ids.
"""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.audit import service as audit_service
from app.modules.auth.permissions import AUDITOR, CASHIER, INVENTORY_CLERK, MANAGER
from app.modules.hr.models import Employee, EmploymentAssignment
from app.modules.transfers.models import InterStoreTransfer
from tests.factories import make_product, make_store, make_supplier, make_user_with_role
from tests.helpers import auth_headers


def test_cashier_cannot_access_the_audit_log(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, CASHIER, username="audit_ep_cashier")
    db.commit()

    headers = auth_headers(client, "audit_ep_cashier", "Test-Password-123!")
    response = client.get("/api/v1/audit-log", headers=headers)

    assert response.status_code == 403


def test_inventory_clerk_cannot_access_the_audit_log(client: TestClient, db: Session) -> None:
    store = make_store(db)
    make_user_with_role(db, store, INVENTORY_CLERK, username="audit_ep_clerk")
    db.commit()

    headers = auth_headers(client, "audit_ep_clerk", "Test-Password-123!")
    response = client.get("/api/v1/audit-log", headers=headers)

    assert response.status_code == 403


def test_store_scoped_manager_sees_only_their_own_store_product_events(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    product_a = make_product(db, store_a)
    product_b = make_product(db, store_b)
    manager = make_user_with_role(
        db, store_a, MANAGER, username="audit_ep_manager_a", store_id=store_a.id
    )
    db.commit()

    event_a = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="product",
        entity_id=product_a.id,
    )
    event_b = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="product",
        entity_id=product_b.id,
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_a", "Test-Password-123!")
    response = client.get("/api/v1/audit-log", params={"entity_type": "product"}, headers=headers)

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert event_a.id in ids
    assert event_b.id not in ids


def test_store_scoped_manager_sees_no_store_dimension_events_excluded(
    client: TestClient, db: Session
) -> None:
    """department/position/supplier/supplier_product have no store
    dimension at all -- a store-scoped reader must never see them, even
    without an entity_type filter (never guessed, never leaked)."""
    store = make_store(db)
    supplier = make_supplier(db)
    manager = make_user_with_role(
        db, store, MANAGER, username="audit_ep_manager_nostoredim", store_id=store.id
    )
    db.commit()

    supplier_event = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="supplier",
        entity_id=supplier.id,
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_nostoredim", "Test-Password-123!")

    # Explicit filter for the excluded entity_type: must come back empty.
    filtered = client.get("/api/v1/audit-log", params={"entity_type": "supplier"}, headers=headers)
    assert filtered.status_code == 200
    assert filtered.json() == []

    # No filter at all: the excluded-type event must still never appear.
    unfiltered = client.get("/api/v1/audit-log", headers=headers)
    assert unfiltered.status_code == 200
    ids = {row["id"] for row in unfiltered.json()}
    assert supplier_event.id not in ids


def test_store_scoped_manager_gets_empty_result_for_unknown_entity_type(
    client: TestClient, db: Session
) -> None:
    """An entity_type this codebase doesn't recognize at all must be
    treated the same as one with no store dimension: excluded, not a
    500 and not a guess."""
    store = make_store(db)
    make_user_with_role(db, store, MANAGER, username="audit_ep_manager_unknown", store_id=store.id)
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_unknown", "Test-Password-123!")
    response = client.get(
        "/api/v1/audit-log",
        params={"entity_type": "totally_made_up_entity_type"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == []


def test_auditor_sees_events_across_stores(client: TestClient, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    product_a = make_product(db, store_a)
    product_b = make_product(db, store_b)
    # Auditor is cross-store: store_id=None, matching every other
    # unscoped role's convention in this codebase.
    auditor = make_user_with_role(db, None, AUDITOR, username="audit_ep_auditor")
    db.commit()

    event_a = audit_service.log_event(
        db, user_id=auditor.id, action="TEST_EVENT", entity_type="product", entity_id=product_a.id
    )
    event_b = audit_service.log_event(
        db, user_id=auditor.id, action="TEST_EVENT", entity_type="product", entity_id=product_b.id
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_auditor", "Test-Password-123!")
    response = client.get("/api/v1/audit-log", params={"entity_type": "product"}, headers=headers)

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert event_a.id in ids
    assert event_b.id in ids


def test_action_filter_narrows_results(client: TestClient, db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    manager = make_user_with_role(
        db, store, MANAGER, username="audit_ep_manager_action", store_id=store.id
    )
    db.commit()

    matching = audit_service.log_event(
        db,
        user_id=manager.id,
        action="AUDIT_ENDPOINT_TEST_ACTION_ONE",
        entity_type="product",
        entity_id=product.id,
    )
    other = audit_service.log_event(
        db,
        user_id=manager.id,
        action="AUDIT_ENDPOINT_TEST_ACTION_TWO",
        entity_type="product",
        entity_id=product.id,
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_action", "Test-Password-123!")
    response = client.get(
        "/api/v1/audit-log",
        params={"action": "AUDIT_ENDPOINT_TEST_ACTION_ONE"},
        headers=headers,
    )

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert matching.id in ids
    assert other.id not in ids


def test_pagination_parameters_cannot_bypass_store_scope(client: TestClient, db: Session) -> None:
    """A store-scoped caller asking for a huge page can never surface
    another store's rows -- the scope filter applies before pagination,
    not after."""
    store_a = make_store(db)
    store_b = make_store(db)
    product_b = make_product(db, store_b)
    manager = make_user_with_role(
        db, store_a, MANAGER, username="audit_ep_manager_paginate", store_id=store_a.id
    )
    db.commit()

    other_store_event = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="product",
        entity_id=product_b.id,
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_paginate", "Test-Password-123!")
    response = client.get(
        "/api/v1/audit-log",
        params={"entity_type": "product", "limit": 500, "offset": 0},
        headers=headers,
    )

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert other_store_event.id not in ids


def test_employee_events_resolve_store_through_the_current_assignment(
    client: TestClient, db: Session
) -> None:
    """entity_type="employee" has no store_id of its own -- the resolver
    must join through the employee's CURRENT (effective_to IS NULL)
    employment_assignments row, exactly like hr.service.get_current_
    assignment does."""
    store_a = make_store(db)
    store_b = make_store(db)
    employee_a = Employee(
        employee_number="EMP-A-1", legal_name="Employee A", hire_date=date(2024, 1, 1)
    )
    employee_b = Employee(
        employee_number="EMP-B-1", legal_name="Employee B", hire_date=date(2024, 1, 1)
    )
    db.add_all([employee_a, employee_b])
    db.flush()
    db.add_all(
        [
            EmploymentAssignment(
                employee_id=employee_a.id,
                store_id=store_a.id,
                effective_from=date(2024, 1, 1),
                effective_to=None,
            ),
            EmploymentAssignment(
                employee_id=employee_b.id,
                store_id=store_b.id,
                effective_from=date(2024, 1, 1),
                effective_to=None,
            ),
        ]
    )
    manager = make_user_with_role(db, store_a, MANAGER, username="audit_ep_manager_employee")
    db.commit()

    event_a = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="employee",
        entity_id=employee_a.id,
    )
    event_b = audit_service.log_event(
        db,
        user_id=manager.id,
        action="TEST_EVENT",
        entity_type="employee",
        entity_id=employee_b.id,
    )
    db.commit()

    headers = auth_headers(client, "audit_ep_manager_employee", "Test-Password-123!")
    response = client.get("/api/v1/audit-log", params={"entity_type": "employee"}, headers=headers)

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert event_a.id in ids
    assert event_b.id not in ids


def test_inter_store_transfer_events_visible_from_either_side_of_the_transfer(
    client: TestClient, db: Session
) -> None:
    """entity_type="inter_store_transfer" matches if EITHER
    from_store_id or to_store_id equals the caller's store -- a manager
    at the receiving store must see it, not only one at the origin."""
    store_from = make_store(db)
    store_to = make_store(db)
    store_unrelated = make_store(db)
    transfer = InterStoreTransfer(
        from_store_id=store_from.id,
        to_store_id=store_to.id,
        transfer_number="XFER-TEST-1",
        status="DRAFT",
        requested_date=date(2024, 1, 1),
    )
    db.add(transfer)
    db.flush()
    manager_to = make_user_with_role(db, store_to, MANAGER, username="audit_ep_manager_xfer_to")
    make_user_with_role(db, store_unrelated, MANAGER, username="audit_ep_manager_xfer_unrelated")
    db.commit()

    event = audit_service.log_event(
        db,
        user_id=manager_to.id,
        action="TEST_EVENT",
        entity_type="inter_store_transfer",
        entity_id=transfer.id,
    )
    db.commit()

    headers_to = auth_headers(client, "audit_ep_manager_xfer_to", "Test-Password-123!")
    response_to = client.get(
        "/api/v1/audit-log", params={"entity_type": "inter_store_transfer"}, headers=headers_to
    )
    assert response_to.status_code == 200
    assert event.id in {row["id"] for row in response_to.json()}

    headers_unrelated = auth_headers(
        client, "audit_ep_manager_xfer_unrelated", "Test-Password-123!"
    )
    response_unrelated = client.get(
        "/api/v1/audit-log",
        params={"entity_type": "inter_store_transfer"},
        headers=headers_unrelated,
    )
    assert response_unrelated.status_code == 200
    assert event.id not in {row["id"] for row in response_unrelated.json()}
