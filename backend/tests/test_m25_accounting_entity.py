"""M25 Phase 1: AccountingEntity foundation.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3/4/23/25-29 for the full
design and docs/M24D_TECHNICAL_CONTRACT.md Section 20 Scenarios 1/13 for
why this phase must leave every existing accounting/inventory flow
byte-identical. Migration upgrade/downgrade/backfill correctness is
covered separately in tests/test_migrations.py (which runs against a
dedicated migrations database); this file exercises the resulting schema
and the new read-only service helpers against the ordinary test database.

Session A: migration/backfill (data-level, against the live test DB --
the from-scratch/dedicated-DB cycle lives in test_migrations.py).
Session B: corporate model (one shared entity). Session C: independent
entity model + isolation. Session D: cross-store/entity security.
Session E: accounting regression (sale, transfer, journal, P&L, trial
balance all unaffected). Session F: concurrency (the one-default-entity
invariant under a race). Session G: failure injection (rollback on a bad
insert). Session H: adversarial validation (invalid FK, duplicate
default, duplicate name). Session I: mutation testing is performed live
against copies per this codebase's established methodology and reported
in the final report, not encoded as a permanent test that asserts wrong
behavior as its expected outcome.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import ACCOUNT_CASH_ON_HAND, ACCOUNT_SALES_REVENUE
from app.modules.accounting_entities.models import AccountingEntity
from app.modules.accounting_entities.service import (
    get_default_accounting_entity,
    get_store_accounting_entity_id,
    is_same_accounting_entity,
)
from app.modules.auth.models import Store
from app.modules.auth.permissions import ADMIN, MANAGER
from tests.factories import DEFAULT_TEST_PASSWORD, make_store, make_user_with_role, unique_suffix
from tests.helpers import auth_headers

# --- Session A: migration/backfill (live test DB) --------------------------


def test_session_a_default_entity_exists_and_is_unique(db: Session) -> None:
    entity = get_default_accounting_entity(db)
    assert entity.entity_type == "CORPORATE_DIVISION"
    assert entity.is_default is True

    # Exactly one default row, even though this test DB already has
    # whatever the migration backfilled before this test started.
    default_rows = (
        db.execute(select(AccountingEntity).where(AccountingEntity.is_default.is_(True)))
        .scalars()
        .all()
    )
    assert len(default_rows) == 1


def test_session_a_every_store_has_a_non_null_entity(db: Session) -> None:
    store = make_store(db)
    assert store.accounting_entity_id is not None

    null_count = db.execute(
        text("SELECT COUNT(*) FROM stores WHERE accounting_entity_id IS NULL")
    ).scalar_one()
    assert null_count == 0


def test_session_a_new_store_without_override_lands_on_default_entity(db: Session) -> None:
    store = make_store(db)
    default_entity = get_default_accounting_entity(db)
    assert store.accounting_entity_id == default_entity.id


# --- Session B: corporate model (one shared entity) -------------------------


def test_session_b_multiple_stores_share_the_default_entity(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    default_entity = get_default_accounting_entity(db)

    assert store_a.accounting_entity_id == default_entity.id
    assert store_b.accounting_entity_id == default_entity.id
    assert is_same_accounting_entity(db, store_a.id, store_b.id) is True


def test_session_b_existing_accounting_behavior_unaffected_by_shared_entity(db: Session) -> None:
    """A plain MANUAL journal entry posts and balances exactly as before
    -- the AccountingEntity foundation introduces no new posting rule."""
    store = make_store(db)
    amount = Decimal("25.00")
    entry = accounting_service._post_journal(
        db,
        store_id=store.id,
        posting_date=date.today(),
        source_type="MANUAL",
        source_id=None,
        memo="session B regression",
        created_by=None,
        lines=[
            accounting_service._debit(ACCOUNT_CASH_ON_HAND, amount),
            accounting_service._credit(ACCOUNT_SALES_REVENUE, amount),
        ],
    )
    db.flush()
    rows = accounting_service.trial_balance(db, store_id=store.id)
    total_debit = sum(r.total_debit for r in rows)
    total_credit = sum(r.total_credit for r in rows)
    assert total_debit == total_credit
    assert entry.store_id == store.id


# --- Session C: independent entity model + isolation ------------------------


def test_session_c_store_can_be_assigned_to_a_separate_entity(db: Session) -> None:
    independent_entity = AccountingEntity(
        name=f"Independent {unique_suffix()}", entity_type="INDEPENDENT", is_default=False
    )
    db.add(independent_entity)
    db.flush()

    store_a = make_store(db)  # default entity
    store_b = make_store(db, accounting_entity_id=independent_entity.id)

    assert get_store_accounting_entity_id(db, store_a.id) != get_store_accounting_entity_id(
        db, store_b.id
    )
    assert is_same_accounting_entity(db, store_a.id, store_b.id) is False


def test_session_c_reassignment_does_not_contaminate_other_stores(db: Session) -> None:
    independent_entity = AccountingEntity(
        name=f"Independent {unique_suffix()}", entity_type="INDEPENDENT", is_default=False
    )
    db.add(independent_entity)
    db.flush()

    default_entity = get_default_accounting_entity(db)
    store_a = make_store(db)
    store_b = make_store(db, accounting_entity_id=independent_entity.id)
    store_c = make_store(db)  # created after the reassignment; must still land on default

    assert store_a.accounting_entity_id == default_entity.id
    assert store_c.accounting_entity_id == default_entity.id
    assert store_b.accounting_entity_id == independent_entity.id


def test_session_c_reassigned_store_accounting_still_unaffected(db: Session) -> None:
    """Phase 1 requirement: 'do not alter existing financial postings
    merely because the new field exists.' A store on a non-default
    entity still posts an ordinary single-store journal exactly as
    before -- nothing in this phase reads entity_type for any posting
    decision."""
    independent_entity = AccountingEntity(
        name=f"Independent {unique_suffix()}", entity_type="INDEPENDENT", is_default=False
    )
    db.add(independent_entity)
    db.flush()
    store_b = make_store(db, accounting_entity_id=independent_entity.id)

    amount = Decimal("10.00")
    entry = accounting_service._post_journal(
        db,
        store_id=store_b.id,
        posting_date=date.today(),
        source_type="MANUAL",
        source_id=None,
        memo="session C regression",
        created_by=None,
        lines=[
            accounting_service._debit(ACCOUNT_CASH_ON_HAND, amount),
            accounting_service._credit(ACCOUNT_SALES_REVENUE, amount),
        ],
    )
    assert entry.store_id == store_b.id
    rows = accounting_service.trial_balance(db, store_id=store_b.id)
    assert sum(r.total_debit for r in rows) == sum(r.total_credit for r in rows)


# --- Session D: cross-store/entity security ---------------------------------


def test_session_d_store_settings_endpoint_does_not_expose_entity_id(db, client) -> None:
    store = make_store(db)
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    headers = auth_headers(client, admin.username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/stores/{store.id}/settings", headers=headers)
    assert response.status_code == 200
    assert "accounting_entity_id" not in response.json()


def test_session_d_store_settings_write_cannot_set_or_change_entity_id(db, client) -> None:
    """Session D / H: an adversarial payload that includes
    accounting_entity_id must be silently ignored by the one existing
    store-mutating endpoint -- there is no field on
    StoreSettingsUpdateRequest for it, so pydantic drops the extra key
    and the store's real assignment is untouched."""
    store = make_store(db)
    original_entity_id = store.accounting_entity_id
    other_entity = AccountingEntity(
        name=f"Attacker Target {unique_suffix()}", entity_type="INDEPENDENT", is_default=False
    )
    db.add(other_entity)
    db.flush()
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    headers = auth_headers(client, admin.username, DEFAULT_TEST_PASSWORD)

    response = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={
            "name": store.name,
            "address": None,
            "timezone": "UTC",
            "attendance_day_boundary_hour": 0,
            "accounting_entity_id": other_entity.id,
        },
    )
    assert response.status_code == 200

    db.expire_all()
    refreshed = db.get(Store, store.id)
    assert refreshed.accounting_entity_id == original_entity_id


def test_session_d_cross_store_user_cannot_read_another_stores_entity_assignment(
    db, client
) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    manager = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    headers = auth_headers(client, manager.username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/stores/{store_b.id}/settings", headers=headers)
    assert response.status_code == 404  # existing 404-not-403 convention, unchanged


def test_session_d_id_substitution_against_a_nonexistent_store_is_rejected(db: Session) -> None:
    with pytest.raises(NotFoundError):
        get_store_accounting_entity_id(db, 999_999_999)


def test_session_d_id_substitution_against_a_nonexistent_entity_in_comparison(
    db: Session,
) -> None:
    store = make_store(db)
    with pytest.raises(NotFoundError):
        is_same_accounting_entity(db, store.id, 999_999_999)


# --- Session E: accounting regression ---------------------------------------


def test_session_e_transfer_ship_and_receive_unaffected(db: Session) -> None:
    """A full inter-store transfer ship/receive cycle, between two stores
    that both sit on the (shared) default entity, must produce exactly
    the same In-Transit/GL/inventory result as before this phase."""
    from app.modules.accounting.constants import ACCOUNT_INVENTORY_IN_TRANSIT
    from app.modules.products.models import Product, ProductCategory
    from app.modules.transfers import service as transfers_service
    from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput

    store_a = make_store(db)
    store_b = make_store(db)
    category = ProductCategory(name=f"Cat {unique_suffix()}", is_active=True)
    db.add(category)
    db.flush()
    source_product = Product(
        store_id=store_a.id,
        category_id=category.id,
        sku=f"SKU-{unique_suffix()}",
        name="Transfer Test Product",
        unit_of_measure="each",
        is_weighed=False,
        current_price=Decimal("9.99"),
        current_cost=Decimal("4.00"),
        current_qty_on_hand=Decimal("50.000"),
        allow_negative_stock=False,
        is_active=True,
    )
    dest_product = Product(
        store_id=store_b.id,
        category_id=category.id,
        sku=source_product.sku,
        name="Transfer Test Product (dest)",
        unit_of_measure="each",
        is_weighed=False,
        current_price=Decimal("9.99"),
        current_cost=Decimal("0"),
        current_qty_on_hand=Decimal("0"),
        allow_negative_stock=False,
        is_active=True,
    )
    db.add_all([source_product, dest_product])
    db.flush()

    transfer = transfers_service._create_transfer_inner(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date.today(),
        lines=[
            TransferLineInput(source_product_id=source_product.id, requested_quantity=Decimal("10"))
        ],
    )
    db.flush()
    line = transfer.lines[0]

    transfers_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line.id, quantity_to_ship=Decimal("10"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    reconciliation_after_ship = transfers_service.inventory_in_transit_reconciliation(db)
    assert reconciliation_after_ship.discrepancy == Decimal("0")

    transfers_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date.today(),
        lines=[ReceiveLineInput(transfer_line_id=line.id, quantity_received=Decimal("10"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    reconciliation_after_receive = transfers_service.inventory_in_transit_reconciliation(db)
    assert reconciliation_after_receive.discrepancy == Decimal("0")
    # This transfer's own line is now fully received -- the company-wide
    # outstanding total may still be non-zero from other, unrelated
    # transfers already present in this database; only the GL-vs-subledger
    # *discrepancy* (asserted above, both before and after) is the
    # invariant this phase must preserve.

    rows = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])
    in_transit_row = next((r for r in rows if r.account_code == ACCOUNT_INVENTORY_IN_TRANSIT), None)
    assert in_transit_row is None or in_transit_row.total_debit == in_transit_row.total_credit


def test_session_e_profit_and_loss_still_computes(db: Session) -> None:
    store = make_store(db)
    pl = accounting_service.profit_and_loss(
        db, store_id=store.id, date_from=date(2000, 1, 1), date_to=date(2100, 1, 1)
    )
    assert pl is not None


# --- Session F: concurrency --------------------------------------------------


def test_session_f_concurrent_second_default_entity_attempts_both_rejected() -> None:
    """Two concurrent transactions both try to create a second
    'is_default = true' row. Because the migration's seeded default row
    is permanent (no reopen/delete path exists -- Section 23), BOTH
    attempts must be rejected by the partial unique index
    (uq_accounting_entities_one_default), regardless of timing: there is
    no window in which a second default could ever win a race against
    the first. This is a stronger, deterministic form of the
    at-most-one-default invariant, not a 1-winner-1-loser race."""
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[str] = []

    def _attempt(name: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            session.execute(
                text(
                    "INSERT INTO accounting_entities (name, entity_type, is_default, created_at) "
                    "VALUES (:name, 'CORPORATE_DIVISION', true, now())"
                ),
                {"name": name},
            )
            session.commit()
            results.append("WON")
        except IntegrityError:
            session.rollback()
            results.append("LOST")
        except Exception as exc:  # pragma: no cover - diagnostic only
            session.rollback()
            results.append("LOST")
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    suffix = unique_suffix()
    threads = [
        threading.Thread(target=_attempt, args=(f"Race Default A {suffix}",)),
        threading.Thread(target=_attempt, args=(f"Race Default B {suffix}",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == ["LOST", "LOST"], f"errors={errors}"

    remaining = SessionLocal()
    try:
        count = remaining.execute(
            text("SELECT COUNT(*) FROM accounting_entities WHERE name LIKE :pattern"),
            {"pattern": f"Race Default%{suffix}"},
        ).scalar_one()
        assert count == 0  # neither attempt left any row behind
    finally:
        remaining.close()


def test_session_f_concurrent_store_creation_against_the_shared_default_entity() -> None:
    """A genuinely racy scenario for this phase: many concurrent store
    creations, each independently resolving
    get_default_accounting_entity() on its own session and inserting a
    new store referencing it. All must succeed, all must land on the
    SAME entity id, and no duplicate entity row is ever created as a
    side effect of the concurrent reads."""
    barrier = threading.Barrier(5)
    suffix = unique_suffix()
    created_entity_ids: list[int] = []
    errors: list[str] = []

    def _create(i: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            entity = get_default_accounting_entity(session)
            session.execute(
                text(
                    "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                    "created_at) VALUES (:name, 'UTC', true, :eid, now())"
                ),
                {"name": f"Concurrent Store {suffix}-{i}", "eid": entity.id},
            )
            session.commit()
            created_entity_ids.append(entity.id)
        except Exception as exc:  # pragma: no cover - diagnostic only
            session.rollback()
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(created_entity_ids) == 5
    assert len(set(created_entity_ids)) == 1  # every store landed on the one shared entity

    cleanup = SessionLocal()
    try:
        cleanup.execute(
            text("DELETE FROM stores WHERE name LIKE :pattern"),
            {"pattern": f"Concurrent Store {suffix}-%"},
        )
        cleanup.commit()
    finally:
        cleanup.close()
    default_count = SessionLocal()
    try:
        count = default_count.execute(
            text("SELECT COUNT(*) FROM accounting_entities WHERE is_default = true")
        ).scalar_one()
        assert count == 1  # the concurrent reads never created a duplicate default
    finally:
        default_count.close()


# --- Session G: failure injection --------------------------------------------


def test_session_g_invalid_entity_fk_rolls_back_cleanly(db: Session) -> None:
    bad_store = Store(
        name=f"Bad Store {unique_suffix()}",
        timezone="UTC",
        is_active=True,
        accounting_entity_id=999_999_999,
    )
    db.add(bad_store)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # The session must be usable again after the rollback -- an ordinary
    # store creation immediately afterward must succeed.
    store = make_store(db)
    assert store.id is not None


def test_session_g_partial_failure_during_entity_creation_does_not_leave_orphan(
    db: Session,
) -> None:
    duplicate_name = f"Dup {unique_suffix()}"
    db.add(AccountingEntity(name=duplicate_name, entity_type="INDEPENDENT", is_default=False))
    db.flush()

    db.add(AccountingEntity(name=duplicate_name, entity_type="INDEPENDENT", is_default=False))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    count = db.execute(
        text("SELECT COUNT(*) FROM accounting_entities WHERE name = :n"), {"n": duplicate_name}
    ).scalar_one()
    assert count == 0  # the whole savepoint-scoped attempt rolled back, including the first add


# --- Session H: adversarial validation ---------------------------------------


def test_session_h_duplicate_entity_name_rejected(db: Session) -> None:
    name = f"Adversarial {unique_suffix()}"
    db.add(AccountingEntity(name=name, entity_type="INDEPENDENT", is_default=False))
    db.flush()
    db.add(AccountingEntity(name=name, entity_type="INDEPENDENT", is_default=False))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_session_h_invalid_entity_type_rejected(db: Session) -> None:
    db.add(
        AccountingEntity(name=f"Bad Type {unique_suffix()}", entity_type="BOGUS", is_default=False)
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_session_h_second_default_entity_rejected(db: Session) -> None:
    db.add(
        AccountingEntity(
            name=f"Second Default {unique_suffix()}",
            entity_type="CORPORATE_DIVISION",
            is_default=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_session_h_malformed_store_update_request_ignores_unknown_entity_switch_field(
    db, client
) -> None:
    """Same adversarial payload as Session D's targeted test, phrased as
    a generic malformed-input check: an entirely bogus/unexpected field
    name must not cause a 500 or silently change unrelated columns."""
    store = make_store(db)
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    headers = auth_headers(client, admin.username, DEFAULT_TEST_PASSWORD)

    response = client.put(
        f"/api/v1/stores/{store.id}/settings",
        headers=headers,
        json={
            "name": store.name,
            "address": None,
            "timezone": "UTC",
            "attendance_day_boundary_hour": 0,
            "switch_accounting_entity_to": 999_999_999,
        },
    )
    assert response.status_code == 200
    db.expire_all()
    refreshed = db.get(Store, store.id)
    assert refreshed.accounting_entity_id == store.accounting_entity_id
