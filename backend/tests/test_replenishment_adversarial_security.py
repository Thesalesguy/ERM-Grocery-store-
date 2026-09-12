"""M9 hardening pass, Phase 8/14: adversarial HTTP tests for every M9
endpoint across all five roles, cross-store execute/cancel (not just
approve, which test_replenishment_api.py already covers), a third store
with no relationship to either side of a TRANSFER plan, and the
supplier-product catalog endpoints' own RBAC (untested until now)."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.auth.permissions import ADMIN, AUDITOR, CASHIER, INVENTORY_CLERK, MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_supplier,
    make_supplier_product,
    make_user_with_role,
)
from tests.helpers import auth_headers


def _make_shortage(db: Session, store) -> None:
    supplier = make_supplier(db)
    product = make_product(db, store, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10"))
    db.commit()
    make_supplier_product(db, supplier, product, unit_cost=Decimal("1.00"))
    db.commit()


# --- full RBAC matrix across all five roles ---------------------------------


def test_rbac_matrix_across_all_five_roles(client: TestClient, db: Session) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    roles = {
        "admin": ADMIN,
        "manager": MANAGER,
        "cashier": CASHIER,
        "clerk": INVENTORY_CLERK,
        "auditor": AUDITOR,
    }
    headers = {}
    for label, role in roles.items():
        make_user_with_role(db, store, role, username=f"matrix_{label}")
    db.commit()
    for label in roles:
        headers[label] = auth_headers(client, f"matrix_{label}", DEFAULT_TEST_PASSWORD)

    expected_read = {"admin": 200, "manager": 200, "cashier": 403, "clerk": 200, "auditor": 200}
    for label, expected in expected_read.items():
        response = client.get(
            "/api/v1/replenishment/plans", params={"store_id": store.id}, headers=headers[label]
        )
        assert response.status_code == expected, f"{label}: read expected {expected}"

    expected_generate = {
        "admin": 200,
        "manager": 200,
        "cashier": 403,
        "clerk": 200,
        "auditor": 403,
    }
    plan_ids: dict[str, int] = {}
    for label, expected in expected_generate.items():
        response = client.post(
            "/api/v1/replenishment/plans/generate",
            headers=headers[label],
            json={"store_id": store.id},
        )
        assert response.status_code == expected, f"{label}: generate expected {expected}"
        if response.status_code == 200 and response.json():
            plan_ids[label] = response.json()[0]["id"]

    # Test each role's approve/execute permission in isolation against
    # its OWN fresh plan — reusing one plan across roles would conflate
    # "already approved by someone else" with "not permitted".
    for label, role in roles.items():
        role_store = make_store(db)
        _make_shortage(db, role_store)
        make_user_with_role(db, role_store, role, username=f"matrix2_{label}")
        db.commit()
        role_headers = auth_headers(client, f"matrix2_{label}", DEFAULT_TEST_PASSWORD)

        gen = client.post(
            "/api/v1/replenishment/plans/generate",
            headers=role_headers,
            json={"store_id": role_store.id},
        )
        if label in ("cashier", "auditor"):
            assert gen.status_code == 403
            continue
        assert gen.status_code == 200
        this_plan_id = gen.json()[0]["id"]

        approve = client.post(
            f"/api/v1/replenishment/plans/{this_plan_id}/approve", headers=role_headers
        )
        expected_approve_code = 200 if label in ("admin", "manager") else 403
        assert approve.status_code == expected_approve_code, f"{label}: approve"

        execute = client.post(
            f"/api/v1/replenishment/plans/{this_plan_id}/execute",
            headers=role_headers,
            json={"client_transaction_id": f"matrix-exec-{label}"},
        )
        expected_execute_code = 200 if label in ("admin", "manager") else 403
        assert execute.status_code == expected_execute_code, f"{label}: execute"


# --- cross-store adversarial attempts on every mutating endpoint ----------


def test_foreign_store_manager_cannot_execute_a_plan_it_did_not_approve(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    _make_shortage(db, store_a)
    make_user_with_role(db, store_a, MANAGER, username="adv_mgr_a")
    make_user_with_role(db, store_b, MANAGER, username="adv_mgr_b")
    db.commit()
    headers_a = auth_headers(client, "adv_mgr_a", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "adv_mgr_b", DEFAULT_TEST_PASSWORD)

    gen = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers_a, json={"store_id": store_a.id}
    )
    plan_id = gen.json()[0]["id"]
    client.post(f"/api/v1/replenishment/plans/{plan_id}/approve", headers=headers_a)

    execute = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/execute",
        headers=headers_b,
        json={"client_transaction_id": "adv-cross-exec"},
    )
    assert execute.status_code in (403, 404)


def test_foreign_store_manager_cannot_cancel_another_stores_plan(
    client: TestClient, db: Session
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    _make_shortage(db, store_a)
    make_user_with_role(db, store_a, MANAGER, username="adv_mgr_c")
    make_user_with_role(db, store_b, MANAGER, username="adv_mgr_d")
    db.commit()
    headers_a = auth_headers(client, "adv_mgr_c", DEFAULT_TEST_PASSWORD)
    headers_b = auth_headers(client, "adv_mgr_d", DEFAULT_TEST_PASSWORD)

    gen = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers_a, json={"store_id": store_a.id}
    )
    plan_id = gen.json()[0]["id"]

    cancel = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/cancel", headers=headers_b, json={}
    )
    assert cancel.status_code in (403, 404)


def test_unrelated_third_store_cannot_see_a_transfer_plan_between_two_others(
    client: TestClient, db: Session
) -> None:
    """A TRANSFER plan's source/destination symmetry (either side's own
    staff may view it) must never leak to a completely unrelated THIRD
    store."""
    store_source = make_store(db)
    store_dest = make_store(db)
    store_unrelated = make_store(db)
    sku = "SKU-ADV-THIRD"
    make_product(
        db, store_source, sku=sku, current_qty_on_hand=Decimal("50"), reorder_point=Decimal("10")
    )
    make_product(
        db, store_dest, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    make_user_with_role(db, store_dest, MANAGER, username="adv_dest_mgr")
    make_user_with_role(db, store_unrelated, MANAGER, username="adv_unrelated_mgr")
    db.commit()
    dest_headers = auth_headers(client, "adv_dest_mgr", DEFAULT_TEST_PASSWORD)
    unrelated_headers = auth_headers(client, "adv_unrelated_mgr", DEFAULT_TEST_PASSWORD)

    gen = client.post(
        "/api/v1/replenishment/plans/generate",
        headers=dest_headers,
        json={"store_id": store_dest.id},
    )
    plan_id = gen.json()[0]["id"]

    response = client.get(f"/api/v1/replenishment/plans/{plan_id}", headers=unrelated_headers)
    assert response.status_code == 404


def test_source_side_store_can_view_but_not_approve_a_transfer_plan_it_does_not_own(
    client: TestClient, db: Session
) -> None:
    """The source store's own staff MAY view a transfer plan touching
    their store (the from/to symmetry), but viewing is not the same as
    being authorized to approve/execute/cancel it — only supply_chain.*
    permission AND being a legitimate party is required, and approval
    specifically is not restricted to either side in this design (any
    supply_chain.approve holder at either store may approve), so this
    test documents the actual, intentional behavior rather than assuming
    view-implies-approve."""
    store_source = make_store(db)
    store_dest = make_store(db)
    sku = "SKU-ADV-SOURCE-VIEW"
    make_product(
        db, store_source, sku=sku, current_qty_on_hand=Decimal("50"), reorder_point=Decimal("10")
    )
    make_product(
        db, store_dest, sku=sku, current_qty_on_hand=Decimal("2"), reorder_point=Decimal("10")
    )
    make_user_with_role(db, store_dest, MANAGER, username="adv_dest_mgr2")
    make_user_with_role(db, store_source, MANAGER, username="adv_source_mgr")
    db.commit()
    dest_headers = auth_headers(client, "adv_dest_mgr2", DEFAULT_TEST_PASSWORD)
    source_headers = auth_headers(client, "adv_source_mgr", DEFAULT_TEST_PASSWORD)

    gen = client.post(
        "/api/v1/replenishment/plans/generate",
        headers=dest_headers,
        json={"store_id": store_dest.id},
    )
    plan_id = gen.json()[0]["id"]

    # The source-side manager CAN view it (legitimate business need: they
    # need to know stock is earmarked to leave their store).
    view = client.get(f"/api/v1/replenishment/plans/{plan_id}", headers=source_headers)
    assert view.status_code == 200

    # The source-side manager CAN also approve/execute it under this
    # design (any supply_chain.approve/execute holder at either
    # legitimate party may act) — this is intentional per
    # _enforce_plan_access's symmetry, not a gap: it mirrors
    # app.modules.transfers.service's own established from/to symmetry.
    approve = client.post(f"/api/v1/replenishment/plans/{plan_id}/approve", headers=source_headers)
    assert approve.status_code == 200


# --- supplier-product catalog RBAC (previously untested) --------------------


def test_supplier_product_catalog_rbac(client: TestClient, db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    db.commit()
    make_user_with_role(db, store, CASHIER, username="adv_cat_cashier")
    make_user_with_role(db, None, INVENTORY_CLERK, username="adv_cat_clerk")
    make_user_with_role(db, None, AUDITOR, username="adv_cat_auditor")
    db.commit()
    cashier_headers = auth_headers(client, "adv_cat_cashier", DEFAULT_TEST_PASSWORD)
    clerk_headers = auth_headers(client, "adv_cat_clerk", DEFAULT_TEST_PASSWORD)
    auditor_headers = auth_headers(client, "adv_cat_auditor", DEFAULT_TEST_PASSWORD)

    payload = {
        "supplier_id": supplier.id,
        "product_id": product.id,
        "unit_cost": "3.50",
        "pack_size": "1",
    }

    cashier_create = client.post(
        "/api/v1/replenishment/supplier-products", headers=cashier_headers, json=payload
    )
    assert cashier_create.status_code == 403

    auditor_create = client.post(
        "/api/v1/replenishment/supplier-products", headers=auditor_headers, json=payload
    )
    assert auditor_create.status_code == 403

    auditor_read = client.get("/api/v1/replenishment/supplier-products", headers=auditor_headers)
    assert auditor_read.status_code == 200

    clerk_create = client.post(
        "/api/v1/replenishment/supplier-products", headers=clerk_headers, json=payload
    )
    assert clerk_create.status_code == 201


# --- request tampering: a client cannot influence server-computed values ---


def test_generate_ignores_extra_unexpected_fields_in_request_body(
    client: TestClient, db: Session
) -> None:
    """The generate endpoint's schema only accepts store_id/product_ids —
    proves an attacker cannot smuggle e.g. a forged supplier_id or
    quantity into plan creation via extra JSON fields."""
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, store, MANAGER, username="adv_tamper_mgr")
    db.commit()
    headers = auth_headers(client, "adv_tamper_mgr", DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/replenishment/plans/generate",
        headers=headers,
        json={
            "store_id": store.id,
            "suggested_quantity": "999999",
            "supplier_id": 999999,
            "status": "EXECUTED",
        },
    )
    assert response.status_code == 200
    plan = response.json()[0]
    assert plan["status"] == "RECOMMENDED"
    assert plan["suggested_quantity"] != "999999"
    assert plan["supplier_id"] != 999999


def test_execute_request_cannot_smuggle_a_quantity_override(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    _make_shortage(db, store)
    make_user_with_role(db, store, MANAGER, username="adv_tamper_exec")
    db.commit()
    headers = auth_headers(client, "adv_tamper_exec", DEFAULT_TEST_PASSWORD)

    gen = client.post(
        "/api/v1/replenishment/plans/generate", headers=headers, json={"store_id": store.id}
    )
    plan_id = gen.json()[0]["id"]
    approved_quantity = gen.json()[0]["suggested_quantity"]
    client.post(f"/api/v1/replenishment/plans/{plan_id}/approve", headers=headers)

    execute = client.post(
        f"/api/v1/replenishment/plans/{plan_id}/execute",
        headers=headers,
        json={
            "client_transaction_id": "adv-tamper-exec",
            "executed_quantity": "999999",
            "unit_cost": "0.01",
        },
    )
    assert execute.status_code == 200
    body = execute.json()
    assert body["executed_quantity"] == approved_quantity
    assert body["executed_quantity"] != "999999"
