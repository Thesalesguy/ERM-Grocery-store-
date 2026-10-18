"""M25 Phase 4: approval-gated write-offs for investigated transfer
discrepancies.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/5/6.2/10/20 for the full
design. Phase 3's ReceivingDiscrepancy ledger stops at a RESOLVED finding
-- this phase adds the one economic-consequence action a RESOLVED,
non-vendor-caused discrepancy can drive: an approval-gated write-off that
posts Dr Inventory Shrinkage Expense (5900) / Cr Inventory In Transit
(1520) at the discrepancy's ATTRIBUTED (source) store, using the
discrepancy's own frozen unit_cost (never either store's current WAC).

Sessions A-O, matching the M25 Phase 4 brief exactly (Session P, mutation
testing, is performed live against backups per this codebase's
established methodology -- not encoded as a permanent test; results are
narrated in the final report). Session O (migration upgrade/downgrade/
re-upgrade and downgrade-with-data protection) is covered by the
dedicated additions to tests/test_migrations.py, mirroring Phase 3's own
"L migration ... covered by the dedicated additions to
tests/test_migrations.py" convention exactly -- not duplicated here.

Hostile self-review mapping (the ~15 attack scenarios the brief names,
each proven by a real test rather than merely asserted safe):
unauthorized/self-approved write-off -> Session C/D; duplicate write-off
-> Session G; nonexistent/unresolved discrepancy -> Session E;
exceeding remaining quantity -> Session F; client-manipulated cost/store
-> Session M (the create schema has no such fields to manipulate in the
first place); wrong cost center -> Session I; GL/in-transit mismatch ->
Session I's reconciliation assertion; double-posted loss -> Session G's
partial-unique-index test; unbalanced journal -> Session I; closed-period
posting -> Session J; partial commit after injected failure -> Session K;
concurrent approvals both succeeding -> Session N; reversal by an
ordinary manager -> Session L; journal deletion/rewrite -> Session L's
AUTOMATED_SOURCE_TYPES test; cross-store/entity access -> Session M;
destructive migration downgrade -> tests/test_migrations.py.
"""

import threading
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationAppError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import (
    ACCOUNT_INVENTORY_IN_TRANSIT,
    ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
)
from app.modules.accounting.models import Account, AccountingPeriod, JournalEntry, JournalLine
from app.modules.audit import service as audit_service
from app.modules.auth.models import Role, Store
from app.modules.auth.permissions import ADMIN, CASHIER, MANAGER, REGIONAL_MANAGER
from app.modules.discrepancies import service as discrepancy_service
from app.modules.discrepancies.models import ReceivingDiscrepancy
from app.modules.products.models import Product
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransferLine, InterStoreTransferReceiptItem
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from app.modules.writeoffs import service as write_off_service
from app.modules.writeoffs.models import ApprovalPolicyRule, TransferWriteOff, WriteOffApproval
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_product,
    make_store,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _ship_and_receive(
    db: Session,
    *,
    shipped_qty: Decimal = Decimal("100"),
    quantity_received: Decimal,
    quantity_damaged: Decimal = Decimal("0"),
    quantity_declared_short: Decimal = Decimal("0"),
    unit_cost: Decimal = Decimal("4.00"),
) -> tuple[int, int, Store, Store, Product, Product, int]:
    """Mirrors tests/test_m25_phase3_discrepancy_investigation.py's own
    _ship_and_receive exactly -- each test file in this chain keeps its
    own local copy rather than importing across test modules."""
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db,
        store_a,
        sku=f"SKU-{unique_suffix()}",
        current_qty_on_hand=Decimal("1000"),
        current_cost=unit_cost,
    )
    dest = make_product(db, store_b, sku=source.sku, current_qty_on_hand=Decimal("0"))
    db.commit()

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=shipped_qty)],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=shipped_qty)],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=quantity_received,
                quantity_damaged=quantity_damaged,
                quantity_declared_short=quantity_declared_short,
            )
        ],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
        received_by=None,
    )
    db.commit()
    return transfer.id, line_id, store_a, store_b, source, dest, receipt.id


def _discrepancy_for_receipt(db: Session, receipt_id: int) -> ReceivingDiscrepancy | None:
    item = db.execute(
        select(InterStoreTransferReceiptItem).where(
            InterStoreTransferReceiptItem.inter_store_transfer_receipt_id == receipt_id
        )
    ).scalar_one()
    return db.execute(
        select(ReceivingDiscrepancy).where(
            ReceivingDiscrepancy.source_type == "TRANSFER_RECEIPT",
            ReceivingDiscrepancy.source_id == item.id,
        )
    ).scalar_one_or_none()


def _resolved_discrepancy(
    db: Session,
    *,
    shipped_qty: Decimal = Decimal("100"),
    quantity_received: Decimal = Decimal("90"),
    quantity_damaged: Decimal = Decimal("10"),
    quantity_declared_short: Decimal = Decimal("0"),
    unit_cost: Decimal = Decimal("4.00"),
    resolution_type: str = "SOURCE_STORE_CAUSED",
) -> tuple[ReceivingDiscrepancy, int, int, Store, Store, Product, Product, int]:
    """Ships, receives (creating a discrepancy), investigates, and
    resolves it -- the eligible-for-write-off starting state every
    Session test below builds on."""
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        shipped_qty=shipped_qty,
        quantity_received=quantity_received,
        quantity_damaged=quantity_damaged,
        quantity_declared_short=quantity_declared_short,
        unit_cost=unit_cost,
    )
    discrepancy = _discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    db.commit()
    discrepancy_service.resolve_discrepancy(
        db,
        discrepancy.id,
        resolution_type=resolution_type,
        resolution_notes="investigated",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()
    db.refresh(discrepancy)
    return discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id


def _account_code(db: Session, account_id: int) -> str:
    return db.execute(select(Account.code).where(Account.id == account_id)).scalar_one()


def _role_id(db: Session, name: str) -> int:
    return db.execute(select(Role.id).where(Role.name == name)).scalar_one()


def _make_policy_rule(
    db: Session,
    *,
    min_amount: Decimal,
    max_amount: Decimal | None,
    role_name: str,
    scope_store_id: int | None = None,
) -> ApprovalPolicyRule:
    rule = ApprovalPolicyRule(
        action_type="TRANSFER_WRITE_OFF",
        scope_store_id=scope_store_id,
        min_amount=min_amount,
        max_amount=max_amount,
        required_role_id=_role_id(db, role_name),
        is_active=True,
    )
    db.add(rule)
    db.commit()
    return rule


# --- Session A: below-threshold write-off under configured policy -------


def test_session_a_below_threshold_approved_by_manager(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=Decimal("100"), role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()

    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged in transit",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    assert write_off.status == "PENDING_APPROVAL"
    assert write_off.required_role_id == _role_id(db, MANAGER)

    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()
    assert posted.status == "POSTED"
    assert posted.journal_entry_id is not None


# --- Session B: above-threshold requires the correct higher tier --------


def test_session_b_above_threshold_requires_regional_manager(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=Decimal("100"), role_name=MANAGER)
    _make_policy_rule(db, min_amount=Decimal("100"), max_amount=None, role_name=REGIONAL_MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(
            db,
            quantity_received=Decimal("50"),
            quantity_damaged=Decimal("50"),
            unit_cost=Decimal("4.00"),
        )
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    wrong_tier_approver = make_user_with_role(db, store_a, MANAGER)
    regional_manager = make_user_with_role(db, None, REGIONAL_MANAGER)
    db.commit()

    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("50"),
        reason="damaged in transit, high value",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    assert write_off.amount == Decimal("200.000000")
    assert write_off.required_role_id == _role_id(db, REGIONAL_MANAGER)

    with pytest.raises(ForbiddenError, match="INSUFFICIENT_APPROVAL_AUTHORITY|approval authority"):
        write_off_service.approve_write_off(
            db,
            write_off_id=write_off.id,
            decision_notes="trying anyway",
            approved_by=wrong_tier_approver.id,
        )

    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=regional_manager.id
    )
    db.commit()
    assert posted.status == "POSTED"


# --- Session C: self-approval rejected -----------------------------------


def test_session_c_self_approval_rejected_service_layer(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()

    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    with pytest.raises(ForbiddenError):
        write_off_service.approve_write_off(
            db,
            write_off_id=write_off.id,
            decision_notes="approving my own request",
            approved_by=initiator.id,
        )


def test_session_c_self_approval_blocked_at_db_level(db: Session) -> None:
    """The structural backstop: even a direct INSERT bypassing the
    service layer entirely is rejected by
    ck_write_off_approvals_no_self_approval."""
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    db.add(
        WriteOffApproval(
            write_off_id=write_off.id,
            initiated_by=initiator.id,
            approved_by=initiator.id,
            decision="APPROVED",
            decided_at=datetime.now(UTC),
            decision_notes="direct insert bypassing service layer",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


# --- Session D: unauthorized / insufficient-tier approval attempts ------


def test_session_d_api_unauthorized_user_cannot_approve(client: TestClient, db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    cashier_username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store_b, CASHIER, username=cashier_username)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    headers = auth_headers(client, cashier_username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/write-offs/{write_off.id}/approve",
        json={"decision_notes": "trying anyway"},
        headers=headers,
    )
    assert response.status_code == 403


def test_session_d_cross_store_manager_without_required_tier_rejected(db: Session) -> None:
    """A Manager from a THIRD store (uninvolved in the transfer, and
    holding the approve permission) is still rejected once the policy
    routes this amount to Regional Manager -- proves tier enforcement is
    role-based, not merely "any Manager from anywhere"."""
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=Decimal("1"), role_name=MANAGER)
    _make_policy_rule(db, min_amount=Decimal("1"), max_amount=None, role_name=REGIONAL_MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, quantity_damaged=Decimal("10"), unit_cost=Decimal("4.00"))
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    third_store = make_store(db)
    outside_manager = make_user_with_role(db, third_store, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    with pytest.raises(ForbiddenError):
        write_off_service.approve_write_off(
            db,
            write_off_id=write_off.id,
            decision_notes="trying anyway",
            approved_by=outside_manager.id,
        )


def test_session_d_cross_store_manager_with_required_tier_succeeds_by_design(
    db: Session,
) -> None:
    """The deliberate, documented counterpart to the test above: a peer
    Manager from a DIFFERENT, uninvolved store who DOES hold the
    required tier's role may approve -- docs/M24D_TECHNICAL_CONTRACT.md
    Section 10.3's own illustrative example. Approval authority is
    role-scoped, not store-scoped, by design (see
    app.modules.writeoffs.service._check_approval_authority)."""
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    third_store = make_store(db)
    peer_manager = make_user_with_role(db, third_store, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    posted = write_off_service.approve_write_off(
        db,
        write_off_id=write_off.id,
        decision_notes="approved cross-store",
        approved_by=peer_manager.id,
    )
    db.commit()
    assert posted.status == "POSTED"


# --- Session E: uninvestigated discrepancy rejected -----------------------


def test_session_e_recorded_discrepancy_rejected(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_damaged=Decimal("10")
    )
    discrepancy = _discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.status == "RECORDED"
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()

    with pytest.raises(ConflictError, match="DISCREPANCY_NOT_RESOLVED|not RESOLVED"):
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("10"),
            reason="too early",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )


def test_session_e_investigating_but_unresolved_discrepancy_rejected(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_damaged=Decimal("10")
    )
    discrepancy = _discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    db.commit()
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()

    with pytest.raises(ConflictError):
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("10"),
            reason="still investigating",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )


def test_session_e_vendor_caused_discrepancy_not_eligible(db: Session) -> None:
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, resolution_type="VENDOR_CAUSED")
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()

    with pytest.raises(ConflictError, match="VENDOR_CAUSED_NOT_ELIGIBLE|VENDOR_CAUSED"):
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("10"),
            reason="vendor's fault",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )


# --- Session F: quantity exceeding the unresolved discrepancy rejected --


def test_session_f_quantity_exceeding_discrepancy_rejected(db: Session) -> None:
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, quantity_damaged=Decimal("10"))
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()

    with pytest.raises(ValidationAppError) as exc_info:
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("11"),
            reason="too much",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )
    assert exc_info.value.error_code == "QUANTITY_EXCEEDS_REMAINING_DISCREPANCY"


def test_session_f_partial_write_off_then_second_exceeding_remainder_rejected(
    db: Session,
) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, quantity_damaged=Decimal("10"))
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()

    first = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("6"),
        reason="partial",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    write_off_service.approve_write_off(
        db, write_off_id=first.id, decision_notes="approved partial", approved_by=approver.id
    )
    db.commit()

    with pytest.raises(ValidationAppError) as exc_info:
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("5"),
            reason="remaining is only 4",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )
    assert exc_info.value.error_code == "QUANTITY_EXCEEDS_REMAINING_DISCREPANCY"


# --- Session G: repeated/retry/duplicate = one financial effect only ----


def test_session_g_idempotent_create_retry_returns_same_row(db: Session) -> None:
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()
    ctxn = f"wo-{unique_suffix()}"

    first = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=ctxn,
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    second = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged (retry)",
        client_transaction_id=ctxn,
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    assert first.id == second.id
    rows = (
        db.execute(
            select(TransferWriteOff).where(TransferWriteOff.discrepancy_id == discrepancy.id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_session_g_duplicate_pending_write_off_rejected(db: Session) -> None:
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()
    write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("5"),
        reason="first request",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        write_off_service.create_write_off(
            db,
            discrepancy_id=discrepancy.id,
            quantity=Decimal("5"),
            reason="second request",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b.id,
            initiated_by=initiator.id,
        )
    assert exc_info.value.error_code == "DUPLICATE_PENDING_WRITE_OFF"


def test_session_g_idempotent_approval_retry_posts_exactly_once(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    first = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()
    second = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved (retry)", approved_by=approver.id
    )
    assert first.journal_entry_id == second.journal_entry_id

    journal_count = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "TRANSFER_WRITE_OFF",
                JournalEntry.source_id == write_off.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(journal_count) == 1
    approvals = (
        db.execute(select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off.id))
        .scalars()
        .all()
    )
    assert len(approvals) == 1


def test_session_g_double_posting_blocked_at_db_level(db: Session) -> None:
    """The free backstop from the existing partial unique index on
    (source_type, source_id): a second STANDARD journal entry for the
    SAME write-off is structurally impossible, bypassing the service
    layer entirely."""
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    db.commit()
    write_off = TransferWriteOff(
        transfer_id=transfer_id,
        transfer_line_id=line_id,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        unit_cost=discrepancy.unit_cost,
        amount=Decimal("40.000000"),
        store_id=store_a.id,
        reason="direct test row",
        initiated_by=initiator.id,
        initiated_at=datetime.now(UTC),
        required_role_id=_role_id(db, MANAGER),
        status="POSTED",
        client_transaction_id=f"wo-{unique_suffix()}",
    )
    db.add(write_off)
    db.flush()
    accounting_service.post_transfer_write_off_journal(
        db, write_off=write_off, posting_date=date.today(), created_by=None
    )
    db.flush()

    with pytest.raises(IntegrityError):
        accounting_service.post_transfer_write_off_journal(
            db, write_off=write_off, posting_date=date.today(), created_by=None
        )


# --- Session H: rejected approval = no inventory/GL change -------------


def test_session_h_denial_has_no_gl_or_inventory_effect_and_reopens_discrepancy(
    db: Session,
) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    line_before = db.get(InterStoreTransferLine, line_id)
    assert line_before is not None
    written_off_before = line_before.written_off_quantity

    denied = write_off_service.deny_write_off(
        db,
        write_off_id=write_off.id,
        decision_notes="insufficient evidence of damage",
        approved_by=approver.id,
    )
    db.commit()

    assert denied.status == "DENIED"
    assert denied.journal_entry_id is None
    db.refresh(line_before)
    assert line_before.written_off_quantity == written_off_before
    journal_rows = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "TRANSFER_WRITE_OFF",
                JournalEntry.source_id == write_off.id,
            )
        )
        .scalars()
        .all()
    )
    assert journal_rows == []

    db.refresh(discrepancy)
    assert discrepancy.status == "INVESTIGATING"
    assert discrepancy.resolution_type is None
    assert discrepancy.resolved_at is None


# --- Session I: successful approval posts correctly ---------------------


def test_session_i_approval_posts_correct_journal_and_inventory_effect(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, quantity_damaged=Decimal("10"), unit_cost=Decimal("4.50"))
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged in transit",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()

    assert posted.unit_cost == Decimal("4.50")
    assert posted.amount == Decimal("45.000000")
    assert posted.store_id == store_a.id  # the SOURCE store, not the destination

    entry = db.get(JournalEntry, posted.journal_entry_id)
    assert entry is not None
    assert entry.store_id == store_a.id
    lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == entry.id))
        .scalars()
        .all()
    )
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit == Decimal("45.000000")
    debit_accounts = {_account_code(db, line.account_id) for line in lines if line.debit > 0}
    credit_accounts = {_account_code(db, line.account_id) for line in lines if line.credit > 0}
    assert debit_accounts == {ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE}
    assert credit_accounts == {ACCOUNT_INVENTORY_IN_TRANSIT}

    line = db.get(InterStoreTransferLine, line_id)
    assert line is not None
    assert line.written_off_quantity == Decimal("10.000")

    db.refresh(discrepancy)
    assert discrepancy.status == "RESOLVED"

    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")


# --- Session J: accounting-period closed enforced ------------------------


def test_session_j_closed_period_blocks_approval(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    today = date.today()
    db.add(
        AccountingPeriod(
            store_id=store_a.id,
            period_start=today,
            period_end=today,
            closed_by=approver.id,
            closed_at=datetime.now(UTC),
            reason="period-close test",
        )
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        write_off_service.approve_write_off(
            db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
        )
    assert exc_info.value.error_code == "PERIOD_CLOSED"


# --- Session K: failure injection at every boundary -> no partial op ----


def test_session_k_journal_posting_failure_leaves_no_partial_state(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("injected journal-posting failure")

    monkeypatch.setattr(accounting_service, "post_transfer_write_off_journal", _boom)
    with pytest.raises(RuntimeError):
        write_off_service.approve_write_off(
            db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
        )
    db.rollback()

    reloaded = db.get(TransferWriteOff, write_off.id)
    assert reloaded is not None
    assert reloaded.status == "PENDING_APPROVAL"
    assert reloaded.journal_entry_id is None
    line = db.get(InterStoreTransferLine, line_id)
    assert line is not None
    assert line.written_off_quantity == Decimal("0")


def test_session_k_audit_failure_after_flush_leaves_no_partial_state(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects a failure AFTER the main flush (status/journal/quantity/
    approval all staged in-session) to prove the transaction boundary,
    not just an early-exit path."""
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("injected audit-log failure")

    monkeypatch.setattr(audit_service, "log_event", _boom)
    with pytest.raises(RuntimeError):
        write_off_service.approve_write_off(
            db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
        )
    db.rollback()

    reloaded = db.get(TransferWriteOff, write_off.id)
    assert reloaded is not None
    assert reloaded.status == "PENDING_APPROVAL"
    assert reloaded.journal_entry_id is None
    line = db.get(InterStoreTransferLine, line_id)
    assert line is not None
    assert line.written_off_quantity == Decimal("0")
    approvals = (
        db.execute(select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off.id))
        .scalars()
        .all()
    )
    assert approvals == []


def test_session_k_discrepancy_reopen_failure_on_denial_leaves_no_partial_state(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("injected discrepancy-reopen failure")

    monkeypatch.setattr(discrepancy_service, "reopen_discrepancy_after_write_off_denial", _boom)
    with pytest.raises(RuntimeError):
        write_off_service.deny_write_off(
            db, write_off_id=write_off.id, decision_notes="denied", approved_by=approver.id
        )
    db.rollback()

    reloaded = db.get(TransferWriteOff, write_off.id)
    assert reloaded is not None
    assert reloaded.status == "PENDING_APPROVAL"
    db.refresh(discrepancy)
    assert discrepancy.status == "RESOLVED"


# --- Session L: reversal is Central/System Admin-only -------------------


def test_session_l_reversal_rejected_for_ordinary_manager(db: Session) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()

    with pytest.raises(ForbiddenError, match="REVERSAL_REQUIRES_ADMIN|Admin"):
        write_off_service.reverse_write_off(
            db, write_off_id=posted.id, reason="manager trying to reverse", reversed_by=approver.id
        )


def test_session_l_admin_reversal_posts_compensating_entry_and_preserves_original(
    db: Session,
) -> None:
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()
    original_entry_id = posted.journal_entry_id
    original_lines_before = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == original_entry_id))
        .scalars()
        .all()
    )
    original_debits_before = {
        (line.account_id, line.debit, line.credit) for line in original_lines_before
    }

    reversed_write_off = write_off_service.reverse_write_off(
        db, write_off_id=posted.id, reason="found undamaged after all", reversed_by=admin.id
    )
    db.commit()

    assert reversed_write_off.status == "REVERSED"
    assert reversed_write_off.reversal_journal_entry_id is not None
    assert reversed_write_off.reversal_journal_entry_id != original_entry_id

    original_lines_after = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == original_entry_id))
        .scalars()
        .all()
    )
    original_debits_after = {
        (line.account_id, line.debit, line.credit) for line in original_lines_after
    }
    assert original_debits_after == original_debits_before  # never edited

    reversal_entry = db.get(JournalEntry, reversed_write_off.reversal_journal_entry_id)
    assert reversal_entry is not None
    assert reversal_entry.source_type == "TRANSFER_WRITE_OFF_REVERSAL"
    reversal_lines = (
        db.execute(select(JournalLine).where(JournalLine.journal_entry_id == reversal_entry.id))
        .scalars()
        .all()
    )
    reversal_debit_accounts = {
        _account_code(db, line.account_id) for line in reversal_lines if line.debit > 0
    }
    reversal_credit_accounts = {
        _account_code(db, line.account_id) for line in reversal_lines if line.credit > 0
    }
    assert reversal_debit_accounts == {ACCOUNT_INVENTORY_IN_TRANSIT}
    assert reversal_credit_accounts == {ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE}

    line = db.get(InterStoreTransferLine, line_id)
    assert line is not None
    assert line.written_off_quantity == Decimal("0")
    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")


def test_session_l_cannot_reverse_non_posted_write_off(db: Session) -> None:
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()

    with pytest.raises(ConflictError):
        write_off_service.reverse_write_off(
            db, write_off_id=write_off.id, reason="not posted yet", reversed_by=admin.id
        )


def test_session_l_generic_reversal_mechanism_refuses_automated_write_off_source(
    db: Session,
) -> None:
    """The journal-deletion/rewrite attack: even Admin cannot reverse a
    TRANSFER_WRITE_OFF-sourced entry through the GENERIC
    reverse_journal_entry mechanism -- only the dedicated
    reverse_write_off function may, mirroring every other automated
    source type's own precedent."""
    _make_policy_rule(db, min_amount=Decimal("0"), max_amount=None, role_name=MANAGER)
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    initiator = make_user_with_role(db, store_b, MANAGER)
    approver = make_user_with_role(db, store_a, MANAGER)
    db.commit()
    write_off = write_off_service.create_write_off(
        db,
        discrepancy_id=discrepancy.id,
        quantity=Decimal("10"),
        reason="damaged",
        client_transaction_id=f"wo-{unique_suffix()}",
        caller_store_id=store_b.id,
        initiated_by=initiator.id,
    )
    db.commit()
    posted = write_off_service.approve_write_off(
        db, write_off_id=write_off.id, decision_notes="approved", approved_by=approver.id
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=posted.journal_entry_id,
            reason="trying the generic path",
            reversed_by=approver.id,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"


# --- Session M: cross-store/entity isolation, ID substitution -----------


def test_session_m_api_initiation_with_substituted_discrepancy_id_rejected(
    client: TestClient, db: Session
) -> None:
    """A Manager at an UNINVOLVED third store submits a create payload
    referencing a discrepancy from an unrelated transfer -- the service
    layer re-derives the store/amount/cost from the persisted
    discrepancy row itself, never trusting anything the client sends, so
    this is rejected on the caller's OWN store membership regardless of
    what discrepancy_id is substituted in."""
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db)
    )
    third_store = make_store(db)
    username = f"mgr_{unique_suffix()}"
    make_user_with_role(db, third_store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/write-offs",
        json={
            "discrepancy_id": discrepancy.id,
            "quantity": "10",
            "reason": "not my business",
            "client_transaction_id": f"wo-{unique_suffix()}",
        },
        headers=headers,
    )
    assert response.status_code == 403


def test_session_m_client_supplied_amount_and_store_fields_ignored(
    client: TestClient, db: Session
) -> None:
    """The create schema has no amount/unit_cost/store_id field to
    manipulate in the first place -- posting them anyway (as an
    over-posting attack) has no effect; every monetary figure is derived
    server-side from the persisted discrepancy."""
    discrepancy, transfer_id, line_id, store_a, store_b, source, dest, receipt_id = (
        _resolved_discrepancy(db, quantity_damaged=Decimal("10"), unit_cost=Decimal("4.00"))
    )
    username = f"mgr_{unique_suffix()}"
    make_user_with_role(db, store_b, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/write-offs",
        json={
            "discrepancy_id": discrepancy.id,
            "quantity": "10",
            "reason": "attempted override",
            "client_transaction_id": f"wo-{unique_suffix()}",
            "amount": "999999.00",
            "unit_cost": "999999.00",
            "store_id": 999999,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["amount"]) == Decimal("40.000000")
    assert Decimal(body["unit_cost"]) == Decimal("4.000000")
    assert body["store_id"] == store_a.id


# --- Session N: real concurrency across independent DB connections ------


def _resolved_discrepancy_cross_connection() -> tuple[int, int, int, int, int]:
    """Returns (discrepancy_id, transfer_id, line_id, store_a_id,
    store_b_id), fully committed and visible to other connections --
    mirrors tests/test_m25_phase3_discrepancy_investigation.py's own
    _ship_and_receive_cross_connection, extended through investigate/
    resolve."""
    setup = SessionLocal()
    try:
        store_a = make_store(setup)
        store_b = make_store(setup)
        source = make_product(
            setup,
            store_a,
            sku=f"SKU-{unique_suffix()}",
            current_qty_on_hand=Decimal("1000"),
            current_cost=Decimal("4.00"),
        )
        setup.commit()
        make_product(setup, store_b, sku=source.sku)
        setup.commit()
        transfer = transfer_service.create_transfer(
            setup,
            from_store_id=store_a.id,
            to_store_id=store_b.id,
            requested_date=date(2024, 1, 1),
            lines=[
                TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("100"))
            ],
            caller_store_id=None,
        )
        setup.commit()
        line_id = transfer.lines[0].id
        transfer_service.ship_transfer(
            setup,
            transfer_id=transfer.id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("100"))],
            client_transaction_id=f"ship-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        receipt = transfer_service.receive_transfer(
            setup,
            transfer_id=transfer.id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("90"),
                    quantity_damaged=Decimal("10"),
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        discrepancy = _discrepancy_for_receipt(setup, receipt.id)
        assert discrepancy is not None
        discrepancy_service.investigate_discrepancy(
            setup, discrepancy.id, caller_store_id=None, actor_id=None
        )
        setup.commit()
        discrepancy_service.resolve_discrepancy(
            setup,
            discrepancy.id,
            resolution_type="SOURCE_STORE_CAUSED",
            resolution_notes="cross-connection setup",
            caller_store_id=None,
            actor_id=None,
        )
        setup.commit()
        return discrepancy.id, transfer.id, line_id, store_a.id, store_b.id
    finally:
        setup.close()


def test_session_n_concurrent_create_attempts_exactly_one_pending() -> None:
    discrepancy_id, transfer_id, line_id, store_a_id, store_b_id = (
        _resolved_discrepancy_cross_connection()
    )
    setup = SessionLocal()
    try:
        initiator_id = make_user_with_role(setup, setup.get(Store, store_b_id), MANAGER).id
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[str] = []

    def _attempt(suffix: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            write_off = write_off_service.create_write_off(
                session,
                discrepancy_id=discrepancy_id,
                quantity=Decimal("5"),
                reason=f"concurrent attempt {suffix}",
                client_transaction_id=f"wo-concurrent-{suffix}-{unique_suffix()}",
                caller_store_id=store_b_id,
                initiated_by=initiator_id,
            )
            session.commit()
            results.append(f"ok:{write_off.id}")
        except ConflictError as exc:
            session.rollback()
            results.append(f"conflict:{exc.error_code}")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt, args=(str(i),)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert len(results) == 2
    ok_results = [r for r in results if r.startswith("ok:")]
    conflict_results = [r for r in results if r.startswith("conflict:")]
    assert len(ok_results) == 1
    assert len(conflict_results) == 1
    assert "DUPLICATE_PENDING_WRITE_OFF" in conflict_results[0]

    verify = SessionLocal()
    try:
        pending = (
            verify.execute(
                select(TransferWriteOff).where(
                    TransferWriteOff.discrepancy_id == discrepancy_id,
                    TransferWriteOff.status == "PENDING_APPROVAL",
                )
            )
            .scalars()
            .all()
        )
        assert len(pending) == 1
    finally:
        verify.close()


def test_session_n_concurrent_approval_attempts_post_exactly_once() -> None:
    discrepancy_id, transfer_id, line_id, store_a_id, store_b_id = (
        _resolved_discrepancy_cross_connection()
    )
    setup = SessionLocal()
    try:
        write_off_service.create_approval_policy_rule(
            setup,
            action_type="TRANSFER_WRITE_OFF",
            scope_store_id=None,
            min_amount=Decimal("0"),
            max_amount=None,
            required_role_id=_role_id(setup, MANAGER),
            escalation_role_id=None,
            is_active=True,
            created_by=None,
        )
        setup.commit()
        initiator_id = make_user_with_role(setup, setup.get(Store, store_b_id), MANAGER).id
        approver_a_id = make_user_with_role(setup, setup.get(Store, store_a_id), MANAGER).id
        approver_b_id = make_user_with_role(setup, setup.get(Store, store_a_id), MANAGER).id
        setup.commit()
        write_off = write_off_service.create_write_off(
            setup,
            discrepancy_id=discrepancy_id,
            quantity=Decimal("10"),
            reason="concurrency test",
            client_transaction_id=f"wo-{unique_suffix()}",
            caller_store_id=store_b_id,
            initiated_by=initiator_id,
        )
        setup.commit()
        write_off_id = write_off.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[str] = []

    def _attempt(approver_id: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            posted = write_off_service.approve_write_off(
                session,
                write_off_id=write_off_id,
                decision_notes="concurrent approval",
                approved_by=approver_id,
            )
            session.commit()
            results.append(f"posted:{posted.journal_entry_id}")
        except ConflictError as exc:
            session.rollback()
            results.append(f"conflict:{exc.error_code}")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=(approver_a_id,)),
        threading.Thread(target=_attempt, args=(approver_b_id,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert len(results) == 2

    verify = SessionLocal()
    try:
        approvals = (
            verify.execute(
                select(WriteOffApproval).where(WriteOffApproval.write_off_id == write_off_id)
            )
            .scalars()
            .all()
        )
        assert len(approvals) == 1
        journal_entries = (
            verify.execute(
                select(JournalEntry).where(
                    JournalEntry.source_type == "TRANSFER_WRITE_OFF",
                    JournalEntry.source_id == write_off_id,
                )
            )
            .scalars()
            .all()
        )
        assert len(journal_entries) == 1
    finally:
        verify.close()
