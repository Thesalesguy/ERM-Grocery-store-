"""M25 Phase 2: extended receiving and custody acceptance.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/5/7/11 for the full
design. Each transfer-receiving event now records the actual physical
result (good/damaged/declared-short) instead of a single "received"
quantity, and every receiving event is also its own immutable custody
acceptance. Only the good quantity ever moves inventory or posts to the
GL; damaged/declared-short remain explicit, unresolved physical facts —
no write-off, no vendor claim, no investigation workflow exists yet
(Phase 3+).

Sessions A-L, matching the M25 Phase 2 brief exactly:
A normal full receipt; B partial (short); C damaged; D short+damaged;
E invalid quantities; F custody acceptance; G idempotency; H
authorization/isolation; I accounting reconciliation; J failure
injection; K concurrency; L mutation testing (performed live against
backups, per this codebase's established methodology -- not encoded as
permanent tests here; results are narrated in the final report).
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import ACCOUNT_INVENTORY, ACCOUNT_INVENTORY_IN_TRANSIT
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.auth.models import Store
from app.modules.inventory.models import InventoryMovement
from app.modules.products.models import Product
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransferLine, TransferCustodyAcceptance
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store, unique_suffix


def _setup_shipped_transfer(
    db: Session, *, shipped_qty: Decimal = Decimal("100"), unit_cost: Decimal = Decimal("4.00")
) -> tuple[int, int, Store, Store, Product, Product]:
    """Returns (transfer_id, transfer_line_id, store_a, store_b, source, dest)."""
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
    return transfer.id, line_id, store_a, store_b, source, dest


def _setup_shipped_transfer_cross_connection(
    shipped_qty: Decimal = Decimal("100"),
) -> tuple[int, int]:
    """For tests that need the setup data visible from a GENUINELY
    separate connection (a fresh SessionLocal(), as a real client retry
    or a concurrent request would use) -- the `db` fixture's savepoint-
    based rollback (see tests/conftest.py) is invisible to any other
    connection even after `db.commit()`, so cross-connection tests must
    set up through a real, independently-committing session instead.
    Mirrors tests/test_transfers_concurrency.py::_setup_transfer exactly.
    Returns (transfer_id, transfer_line_id)."""
    setup = SessionLocal()
    try:
        store_a = make_store(setup)
        store_b = make_store(setup)
        source = make_product(
            setup, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("1000")
        )
        setup.commit()
        make_product(setup, store_b, sku=source.sku)
        setup.commit()
        transfer = transfer_service.create_transfer(
            setup,
            from_store_id=store_a.id,
            to_store_id=store_b.id,
            requested_date=date(2024, 1, 1),
            lines=[TransferLineInput(source_product_id=source.id, requested_quantity=shipped_qty)],
            caller_store_id=None,
        )
        setup.commit()
        line_id = transfer.lines[0].id
        transfer_service.ship_transfer(
            setup,
            transfer_id=transfer.id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=shipped_qty)],
            client_transaction_id=f"ship-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        return transfer.id, line_id
    finally:
        setup.close()


# --- Session A: normal full receipt ------------------------------------------


def test_session_a_normal_full_receipt(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )

    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
        received_by=None,
    )
    db.commit()
    db.refresh(dest)

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("100")
    assert line.damaged_quantity == Decimal("0")
    assert line.declared_short_quantity == Decimal("0")
    # Expected == shipped == good + damaged + short + remaining(0).
    assert (
        line.received_quantity + line.damaged_quantity + line.declared_short_quantity
        == line.shipped_quantity
    )
    assert dest.current_qty_on_hand == Decimal("100")

    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")

    (item,) = receipt.items
    assert item.quantity_received == Decimal("100")
    assert item.quantity_damaged == Decimal("0")
    assert item.quantity_declared_short == Decimal("0")


# --- Session B: partial receipt (short) --------------------------------------


def test_session_b_partial_receipt_short_preserved_not_written_off(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))

    transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=Decimal("90"),
                quantity_declared_short=Decimal("10"),
            )
        ],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("90")
    assert line.damaged_quantity == Decimal("0")
    assert line.declared_short_quantity == Decimal("10")
    assert (
        line.received_quantity + line.damaged_quantity + line.declared_short_quantity
        == line.shipped_quantity
    )
    # No write-off, no loss expense, no new journal beyond the ordinary
    # good-quantity receipt journal -- exactly one JournalEntry for this
    # receipt's source_type/source_id.
    receipt_entries = (
        db.query(JournalEntry)
        .filter_by(source_type="INTER_STORE_TRANSFER_RECEIVE")
        .filter(JournalEntry.source_id.isnot(None))
        .all()
    )
    matching = [
        e
        for e in receipt_entries
        if e.source_id
        in [r.id for r in transfer_service.list_transfer_receipts(db, transfer_id=transfer_id)]
    ]
    assert len(matching) == 1


# --- Session C: damaged receipt ----------------------------------------------


def test_session_c_damaged_receipt_remains_unresolved(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))

    transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
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
    db.commit()

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("90")
    assert line.damaged_quantity == Decimal("10")
    assert line.declared_short_quantity == Decimal("0")
    assert (
        line.received_quantity + line.damaged_quantity + line.declared_short_quantity
        == line.shipped_quantity
    )
    # No loss/write-off is posted for the damaged quantity -- exactly one
    # journal entry exists for this receipt (the ordinary good-quantity
    # receive journal), valued at good-only (90), never the full 100.
    receipts = transfer_service.list_transfer_receipts(db, transfer_id=transfer_id)
    receipt_entry = (
        db.query(JournalEntry)
        .filter_by(source_type="INTER_STORE_TRANSFER_RECEIVE", source_id=receipts[0].id)
        .one()
    )
    by_account = {
        db.get(Account, jl.account_id).code: jl
        for jl in db.query(JournalLine).filter_by(journal_entry_id=receipt_entry.id).all()
    }
    assert by_account[ACCOUNT_INVENTORY].debit == Decimal("360.000000")  # 90 * 4.00


# --- Session D: short + damaged receipt --------------------------------------


def test_session_d_short_and_damaged_exact_reconciliation(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100"), unit_cost=Decimal("4.00")
    )

    transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=Decimal("88"),
                quantity_damaged=Decimal("2"),
                quantity_declared_short=Decimal("10"),
            )
        ],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(dest)

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("88")
    assert line.damaged_quantity == Decimal("2")
    assert line.declared_short_quantity == Decimal("10")
    # Expected 100 -> Shipped 100 -> Good 88 -> Damaged 2 -> Short 10 ->
    # remaining in transit 0.
    assert (
        line.received_quantity + line.damaged_quantity + line.declared_short_quantity
        == line.shipped_quantity
        == Decimal("100")
    )
    assert dest.current_qty_on_hand == Decimal("88")

    journal_entry = (
        db.query(JournalEntry)
        .filter_by(source_type="INTER_STORE_TRANSFER_RECEIVE")
        .join(JournalLine)
        .filter(
            JournalLine.account_id == db.query(Account).filter_by(code=ACCOUNT_INVENTORY).one().id
        )
        .first()
    )
    assert journal_entry is not None
    by_account = {
        db.get(Account, jl.account_id).code: jl
        for jl in db.query(JournalLine).filter_by(journal_entry_id=journal_entry.id).all()
    }
    assert by_account[ACCOUNT_INVENTORY].debit == Decimal("352.000000")  # 88 * 4.00
    assert by_account[ACCOUNT_INVENTORY_IN_TRANSIT].credit == Decimal("352.000000")


# --- Session E: invalid quantities -------------------------------------------


def test_session_e_negative_quantity_rejected(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ValidationAppError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("10"),
                    quantity_damaged=Decimal("-5"),
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "INVALID_QUANTITY"


def test_session_e_over_receipt_rejected(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("101"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_RECEIPT"


def test_session_e_combined_over_receipt_rejected(db: Session) -> None:
    """100 shipped; 90 good + 15 damaged = 105 > 100 must be rejected even
    though no single quantity alone exceeds 100."""
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("90"),
                    quantity_damaged=Decimal("15"),
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_RECEIPT"


def test_session_e_mismatched_quantities_exceeding_remaining_across_events(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("60"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 3),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("41"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_RECEIPT"


def test_session_e_zero_quantities_rejected_as_malformed(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ValidationAppError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("0"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "INVALID_QUANTITY"


def test_session_e_api_rejects_malformed_payload(client, db: Session) -> None:
    """Schema-level rejection: the API must reject an all-zero line and a
    negative quantity before ever reaching the service layer."""
    from app.modules.auth.permissions import ADMIN
    from tests.factories import DEFAULT_TEST_PASSWORD, make_user_with_role
    from tests.helpers import auth_headers

    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    admin = make_user_with_role(db, None, ADMIN)
    db.commit()
    headers = auth_headers(client, admin.username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/transfers/{transfer_id}/receipts",
        headers=headers,
        json={
            "received_date": "2024-01-02",
            "client_transaction_id": f"recv-{unique_suffix()}",
            "lines": [
                {
                    "transfer_line_id": line_id,
                    "quantity_received": "0",
                    "quantity_damaged": "0",
                    "quantity_declared_short": "0",
                }
            ],
        },
    )
    assert response.status_code == 422

    response = client.post(
        f"/api/v1/transfers/{transfer_id}/receipts",
        headers=headers,
        json={
            "received_date": "2024-01-02",
            "client_transaction_id": f"recv-{unique_suffix()}",
            "lines": [
                {
                    "transfer_line_id": line_id,
                    "quantity_received": "10",
                    "quantity_damaged": "-1",
                }
            ],
        },
    )
    assert response.status_code == 422


# --- Session F: custody acceptance -------------------------------------------


def test_session_f_custody_acceptance_recorded_with_full_evidence(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, *_ = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )
    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
        received_by=None,
    )
    db.commit()

    custody = db.execute(
        select(TransferCustodyAcceptance).where(
            TransferCustodyAcceptance.inter_store_transfer_receipt_id == receipt.id
        )
    ).scalar_one()
    assert custody.transfer_id == transfer_id
    assert custody.store_id == store_b.id
    assert custody.accepted_at is not None
    assert custody.is_digital_acknowledgment is True

    from app.modules.audit.models import AuditLog

    audit_row = (
        db.query(AuditLog)
        .filter_by(
            action="TRANSFER_CUSTODY_ACCEPTED",
            entity_type="inter_store_transfer_custody_acceptance",
            entity_id=custody.id,
        )
        .one()
    )
    assert audit_row.after_state["transfer_id"] == transfer_id


def test_session_f_custody_acceptance_is_immutable_at_db_level(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    custody = db.execute(
        select(TransferCustodyAcceptance).where(
            TransferCustodyAcceptance.inter_store_transfer_receipt_id == receipt.id
        )
    ).scalar_one()

    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        db.execute(
            TransferCustodyAcceptance.__table__.update()
            .where(TransferCustodyAcceptance.id == custody.id)
            .values(is_digital_acknowledgment=False)
        )
        db.flush()
    db.rollback()


def test_session_f_one_custody_row_per_receipt_event_not_per_transfer(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    receipt_1 = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("40"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    receipt_2 = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 3),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("60"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    custody_rows = (
        db.execute(
            select(TransferCustodyAcceptance).where(
                TransferCustodyAcceptance.transfer_id == transfer_id
            )
        )
        .scalars()
        .all()
    )
    assert {c.inter_store_transfer_receipt_id for c in custody_rows} == {
        receipt_1.id,
        receipt_2.id,
    }
    assert len(custody_rows) == 2


# --- Session G: idempotency ---------------------------------------------------


def test_session_g_identical_retry_is_a_no_op(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    ctid = f"recv-{unique_suffix()}"
    first = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=ctid,
        caller_store_id=None,
    )
    db.commit()
    second = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=ctid,
        caller_store_id=None,
    )
    assert second.id == first.id

    custody_count = (
        db.execute(
            select(TransferCustodyAcceptance).where(
                TransferCustodyAcceptance.inter_store_transfer_receipt_id == first.id
            )
        )
        .scalars()
        .all()
    )
    assert len(custody_count) == 1

    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("100")  # not double-added


def test_session_g_retry_after_successful_commit() -> None:
    transfer_id, line_id = _setup_shipped_transfer_cross_connection(Decimal("100"))
    ctid = f"recv-{unique_suffix()}"
    session1 = SessionLocal()
    try:
        first = transfer_service.receive_transfer(
            session1,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
            client_transaction_id=ctid,
            caller_store_id=None,
        )
        session1.commit()
        first_id = first.id
    finally:
        session1.close()

    # A fresh session, mirroring a real client retry over a new connection.
    session2 = SessionLocal()
    try:
        replay = transfer_service.receive_transfer(
            session2,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
            client_transaction_id=ctid,
            caller_store_id=None,
        )
        assert replay.id == first_id
    finally:
        session2.close()


def test_session_g_concurrent_duplicate_receipt_creates_exactly_one() -> None:
    """Uses a PARTIAL quantity (40 of 100 shipped) deliberately -- a full
    100/100 duplicate would also be (correctly) rejected by the
    over-receipt guard on the second attempt even if idempotency itself
    were broken, masking the exact defect this test exists to catch. 40+40
    = 80 <= 100 is well within the over-receipt bound, so only genuine
    duplicate-request deduplication can keep this at exactly one receipt
    and received_quantity == 40 rather than 80."""
    transfer_id, line_id = _setup_shipped_transfer_cross_connection(Decimal("100"))

    ctid = f"recv-{unique_suffix()}"
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[str] = []

    def _attempt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            receipt = transfer_service.receive_transfer(
                session,
                transfer_id=transfer_id,
                received_date=date(2024, 1, 2),
                lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("40"))],
                client_transaction_id=ctid,
                caller_store_id=None,
            )
            session.commit()
            results.append(receipt.id)
        except Exception as exc:  # pragma: no cover - diagnostic only
            session.rollback()
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert len(results) == 2
    assert results[0] == results[1]  # both resolved to the SAME receipt

    verify = SessionLocal()
    try:
        custody_count = (
            verify.execute(
                select(TransferCustodyAcceptance).where(
                    TransferCustodyAcceptance.inter_store_transfer_receipt_id == results[0]
                )
            )
            .scalars()
            .all()
        )
        assert len(custody_count) == 1
        line = verify.get(InterStoreTransferLine, line_id)
        assert line.received_quantity == Decimal("40")  # not 80 (double-counted)
    finally:
        verify.close()


# --- Session H: authorization/isolation --------------------------------------


def test_session_h_unauthorized_destination_store_rejected(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, *_ = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )
    other_store = make_store(db)
    db.commit()
    with pytest.raises(ForbiddenError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=other_store.id,
        )
    assert exc_info.value.error_code == "STORE_ACCESS_DENIED"


def test_session_h_source_store_cannot_receive_its_own_shipment(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, *_ = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )
    with pytest.raises(ForbiddenError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=store_a.id,
        )


def test_session_h_line_id_substitution_from_another_transfer_rejected(db: Session) -> None:
    transfer_id_1, _line_id_1, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    transfer_id_2, line_id_2, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(NotFoundError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id_1,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id_2, quantity_received=Decimal("10"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )


def test_session_h_nonexistent_transfer_id_rejected(db: Session) -> None:
    with pytest.raises(NotFoundError):
        transfer_service.receive_transfer(
            db,
            transfer_id=999_999_999,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=1, quantity_received=Decimal("10"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )


def test_session_h_accounting_entity_field_does_not_weaken_store_isolation(db: Session) -> None:
    """The new Store.accounting_entity_id column (M25 Phase 1) must not
    create any new bypass -- a third store on the SAME default entity as
    the real destination is still rejected exactly like any other
    unrelated store."""
    transfer_id, line_id, store_a, store_b, *_ = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )
    from app.modules.accounting_entities.service import is_same_accounting_entity

    third_store = make_store(db)
    db.commit()
    assert is_same_accounting_entity(db, third_store.id, store_b.id) is True

    with pytest.raises(ForbiddenError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=third_store.id,
        )


def test_session_h_api_cross_store_receive_rejected(client, db: Session) -> None:
    from app.modules.auth.permissions import INVENTORY_CLERK
    from tests.factories import DEFAULT_TEST_PASSWORD, make_user_with_role
    from tests.helpers import auth_headers

    transfer_id, line_id, store_a, store_b, *_ = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100")
    )
    other_store = make_store(db)
    clerk = make_user_with_role(db, other_store, INVENTORY_CLERK)
    db.commit()
    headers = auth_headers(client, clerk.username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        f"/api/v1/transfers/{transfer_id}/receipts",
        headers=headers,
        json={
            "received_date": "2024-01-02",
            "client_transaction_id": f"recv-{unique_suffix()}",
            "lines": [{"transfer_line_id": line_id, "quantity_received": "100"}],
        },
    )
    assert response.status_code == 403


# --- Session I: accounting reconciliation ------------------------------------


def test_session_i_full_reconciliation_after_mixed_receipt(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest = _setup_shipped_transfer(
        db, shipped_qty=Decimal("100"), unit_cost=Decimal("5.00")
    )
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=Decimal("80"),
                quantity_damaged=Decimal("5"),
                quantity_declared_short=Decimal("15"),
            )
        ],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(dest)

    assert dest.current_qty_on_hand == Decimal("80")
    assert dest.current_cost == Decimal("5.000000")  # frozen cost rolled, unaffected by WAC

    rows = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])
    total_debit = sum(r.total_debit for r in rows)
    total_credit = sum(r.total_credit for r in rows)
    assert total_debit == total_credit  # double-entry still balances

    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")

    pl = accounting_service.profit_and_loss(
        db, store_id=store_b.id, date_from=date(2000, 1, 1), date_to=date(2100, 1, 1)
    )
    assert pl is not None  # no loss expense posted yet; P&L computes cleanly


# --- Session J: failure injection ---------------------------------------------


def test_session_j_failure_before_flush_leaves_no_partial_state(db: Session) -> None:
    """A validation failure (negative quantity) must occur before any
    inventory/journal/custody row is ever created."""
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ValidationAppError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("10"),
                    quantity_damaged=Decimal("-1"),
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("0")
    movements = db.query(InventoryMovement).filter_by(reference_id=transfer_id).all()
    assert all(m.movement_type != "TRANSFER_IN" for m in movements)


def test_session_j_rollback_after_overreceipt_mid_request_is_clean(db: Session) -> None:
    """A multi-line request where one line over-receives must roll back
    the WHOLE request -- no partial movement/journal for the other,
    valid line."""
    store_a = make_store(db)
    store_b = make_store(db)
    source_1 = make_product(db, store_a, current_qty_on_hand=Decimal("1000"))
    dest_1 = make_product(db, store_b, sku=source_1.sku)
    source_2 = make_product(db, store_a, current_qty_on_hand=Decimal("1000"))
    dest_2 = make_product(db, store_b, sku=source_2.sku)
    db.commit()

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[
            TransferLineInput(source_product_id=source_1.id, requested_quantity=Decimal("50")),
            TransferLineInput(source_product_id=source_2.id, requested_quantity=Decimal("50")),
        ],
        caller_store_id=None,
    )
    db.commit()
    line_1, line_2 = transfer.lines
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[
            ShipLineInput(transfer_line_id=line_1.id, quantity_to_ship=Decimal("50")),
            ShipLineInput(transfer_line_id=line_2.id, quantity_to_ship=Decimal("50")),
        ],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer.id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(transfer_line_id=line_1.id, quantity_received=Decimal("50")),
                ReceiveLineInput(transfer_line_id=line_2.id, quantity_received=Decimal("51")),
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    db.refresh(dest_1)
    db.refresh(dest_2)
    assert dest_1.current_qty_on_hand == Decimal("0")
    assert dest_2.current_qty_on_hand == Decimal("0")


def test_session_j_retry_after_failure_succeeds_cleanly(db: Session) -> None:
    transfer_id, line_id, *_ = _setup_shipped_transfer(db, shipped_qty=Decimal("100"))
    with pytest.raises(ConflictError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("101"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer_id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("100"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    assert receipt.id is not None
    line = db.get(InterStoreTransferLine, line_id)
    assert line.received_quantity == Decimal("100")


# --- Session K: concurrency ---------------------------------------------------


def test_session_k_concurrent_non_overlapping_receipts_both_succeed() -> None:
    transfer_id, line_id = _setup_shipped_transfer_cross_connection(Decimal("100"))

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[str] = []

    def _attempt(qty: Decimal, label: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            transfer_service.receive_transfer(
                session,
                transfer_id=transfer_id,
                received_date=date(2024, 1, 2),
                lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=qty)],
                client_transaction_id=f"recv-{label}-{unique_suffix()}",
                caller_store_id=None,
            )
            session.commit()
            results.append("OK")
        except Exception as exc:  # pragma: no cover - diagnostic only
            session.rollback()
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=(Decimal("40"), "a")),
        threading.Thread(target=_attempt, args=(Decimal("60"), "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert results == ["OK", "OK"]

    verify = SessionLocal()
    try:
        line = verify.get(InterStoreTransferLine, line_id)
        assert line.received_quantity == Decimal("100")  # deterministic final state
    finally:
        verify.close()


def test_session_k_concurrent_overlapping_receipts_never_jointly_overreceive() -> None:
    transfer_id, line_id = _setup_shipped_transfer_cross_connection(Decimal("100"))

    barrier = threading.Barrier(2)
    results: list[str] = []

    def _attempt(label: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            transfer_service.receive_transfer(
                session,
                transfer_id=transfer_id,
                received_date=date(2024, 1, 2),
                lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("70"))],
                client_transaction_id=f"recv-{label}-{unique_suffix()}",
                caller_store_id=None,
            )
            session.commit()
            results.append("OK")
        except ConflictError:
            session.rollback()
            results.append("OVER_RECEIPT")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=("a",)),
        threading.Thread(target=_attempt, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["OK", "OVER_RECEIPT"]

    verify = SessionLocal()
    try:
        line = verify.get(InterStoreTransferLine, line_id)
        assert line.received_quantity == Decimal("70")  # never exceeds shipped
    finally:
        verify.close()
