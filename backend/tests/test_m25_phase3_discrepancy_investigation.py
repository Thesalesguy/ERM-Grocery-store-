"""M25 Phase 3: discrepancy investigation ledger.

See docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/12/16/17 for the full
design. Phase 2's receive_transfer now turns any nonzero damaged/
declared-short receipt item into an explicit ReceivingDiscrepancy row,
automatically, in the same transaction. The ledger's state machine is a
strict subset of the contract's full one: RECORDED -> INVESTIGATING ->
RESOLVED only -- no write-off, shrinkage posting, vendor claim, AP/AR
entry, or physical return is ever created here (Phase 4+).

Sessions A-L, matching the M25 Phase 3 brief exactly (Session M, mutation
testing, is performed live against backups per this codebase's
established methodology -- not encoded as a permanent test; results are
narrated in the final report):
A no discrepancy; B shortage; C damage; D mixed; E resolution; F invalid
transitions; G idempotency; H authorization/isolation; I accounting
regression; J failure injection; K concurrency; L migration (covered by
the dedicated additions to tests/test_migrations.py).

Naming note: the brief's own prose uses "OPEN" generically for the
ledger's initial state. docs/M24D_TECHNICAL_CONTRACT.md Section 12.1 --
which this task explicitly required reading first, and which is
authoritative -- already names that same state "RECORDED". This test
file (and the implementation) use RECORDED throughout, consistent with
the contract's own vocabulary; see the Phase 3 final report for the
explicit call-out."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.db.session import SessionLocal
from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Store
from app.modules.auth.permissions import CASHIER, INVENTORY_CLERK
from app.modules.discrepancies import service as discrepancy_service
from app.modules.discrepancies.models import ReceivingDiscrepancy
from app.modules.products.models import Product
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransferLine, InterStoreTransferReceiptItem
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
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
    """Returns (transfer_id, line_id, store_a, store_b, source, dest,
    receipt_id). Mirrors tests/test_m25_phase2_receiving_custody.py's own
    _setup_shipped_transfer, extended to also perform the receipt."""
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


def _get_discrepancy_for_receipt(db: Session, receipt_id: int) -> ReceivingDiscrepancy | None:
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


def _ship_and_receive_cross_connection(
    *,
    quantity_received: Decimal = Decimal("90"),
    quantity_damaged: Decimal = Decimal("0"),
    quantity_declared_short: Decimal = Decimal("10"),
) -> int:
    """For tests needing the resulting discrepancy visible from a
    GENUINELY separate connection (real concurrent threads) -- the `db`
    fixture's savepoint-based commit is invisible to any other
    connection even after `db.commit()`. Mirrors
    tests/test_m25_phase2_receiving_custody.py's own
    _setup_shipped_transfer_cross_connection exactly. Returns the created
    discrepancy's id."""
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
                    quantity_received=quantity_received,
                    quantity_damaged=quantity_damaged,
                    quantity_declared_short=quantity_declared_short,
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
        setup.commit()
        discrepancy = _get_discrepancy_for_receipt(setup, receipt.id)
        assert discrepancy is not None
        return discrepancy.id
    finally:
        setup.close()


# --- Session A: no discrepancy -----------------------------------------


def test_session_a_no_discrepancy(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, shipped_qty=Decimal("100"), quantity_received=Decimal("100")
    )

    assert _get_discrepancy_for_receipt(db, receipt_id) is None
    db.refresh(dest)
    assert dest.current_qty_on_hand == Decimal("100")
    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")


# --- Session B: shortage -------------------------------------------------


def test_session_b_shortage(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        shipped_qty=Decimal("100"),
        quantity_received=Decimal("90"),
        quantity_declared_short=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )

    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.status == "RECORDED"
    assert discrepancy.discrepancy_type == "SHORTAGE"
    assert discrepancy.quantity_short == Decimal("10")
    assert discrepancy.quantity_damaged == Decimal("0")
    assert discrepancy.product_id == dest.id
    assert discrepancy.store_id == store_b.id
    assert discrepancy.counterparty_store_id == store_a.id
    assert discrepancy.transfer_id == transfer_id
    assert discrepancy.unit_cost == Decimal("4.00")
    assert discrepancy.source_type == "TRANSFER_RECEIPT"

    # No GL loss -- the in-transit account still carries the short
    # quantity's value (unchanged by discrepancy creation). Scoped to
    # this test's own line, not the company-wide reconciliation total
    # (which also reflects every other test's data in the same run).
    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")
    line = db.get(InterStoreTransferLine, line_id)
    this_line_outstanding = (
        line.shipped_quantity - line.received_quantity
    ) * line.unit_cost_at_shipment
    assert this_line_outstanding == Decimal("40.00")  # 10 * 4.00


# --- Session C: damage ---------------------------------------------------


def test_session_c_damage(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        shipped_qty=Decimal("100"),
        quantity_received=Decimal("90"),
        quantity_damaged=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )

    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.status == "RECORDED"
    assert discrepancy.discrepancy_type == "DAMAGE"
    assert discrepancy.quantity_damaged == Decimal("10")
    assert discrepancy.quantity_short == Decimal("0")
    assert discrepancy.product_id == dest.id
    assert discrepancy.transfer_id == transfer_id

    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")


# --- Session D: mixed -----------------------------------------------------


def test_session_d_mixed(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        shipped_qty=Decimal("100"),
        quantity_received=Decimal("88"),
        quantity_damaged=Decimal("2"),
        quantity_declared_short=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )

    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.discrepancy_type == "BOTH"
    assert discrepancy.quantity_damaged == Decimal("2")
    assert discrepancy.quantity_short == Decimal("10")
    assert discrepancy.quantity_damaged + discrepancy.quantity_short == Decimal("12")


# --- Session E: resolution -------------------------------------------------


def test_session_e_resolution(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        quantity_received=Decimal("90"),
        quantity_declared_short=Decimal("10"),
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    before_entry_count = db.execute(select(JournalEntry.id)).all()

    investigated = discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    db.commit()
    assert investigated.status == "INVESTIGATING"

    resolved = discrepancy_service.resolve_discrepancy(
        db,
        discrepancy.id,
        resolution_type="SOURCE_STORE_CAUSED",
        resolution_notes="Source store's own count was short before shipment.",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()

    assert resolved.status == "RESOLVED"
    assert resolved.resolution_type == "SOURCE_STORE_CAUSED"
    assert resolved.resolution_notes == "Source store's own count was short before shipment."
    assert resolved.resolved_at is not None
    # The original factual discrepancy is preserved unchanged.
    assert resolved.quantity_short == Decimal("10")
    assert resolved.quantity_damaged == Decimal("0")

    after_entry_count = db.execute(select(JournalEntry.id)).all()
    assert len(after_entry_count) == len(before_entry_count)  # no new journal entry

    audit_actions = (
        db.execute(
            select(AuditLog.action)
            .where(
                AuditLog.entity_type == "receiving_discrepancy",
                AuditLog.entity_id == discrepancy.id,
            )
            .order_by(AuditLog.id)
        )
        .scalars()
        .all()
    )
    assert audit_actions == [
        "DISCREPANCY_RECORDED",
        "DISCREPANCY_INVESTIGATION_STARTED",
        "DISCREPANCY_RESOLVED",
    ]


# --- Session F: invalid transitions ---------------------------------------


def test_session_f_resolved_to_investigating_rejected(db: Session) -> None:
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    discrepancy_service.resolve_discrepancy(
        db,
        discrepancy.id,
        resolution_type="UNKNOWN",
        resolution_notes="n/a",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError):
        discrepancy_service.investigate_discrepancy(
            db, discrepancy.id, caller_store_id=None, actor_id=None
        )


def test_session_f_resolved_to_resolved_again_rejected(db: Session) -> None:
    """RESOLVED -> OPEN/RECORDED has no API at all (there is no
    "un-resolve" transition) -- attempting to resolve an already-RESOLVED
    discrepancy a second time is the equivalent invalid transition and
    must fail safely."""
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    discrepancy_service.resolve_discrepancy(
        db,
        discrepancy.id,
        resolution_type="UNKNOWN",
        resolution_notes="n/a",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError):
        discrepancy_service.resolve_discrepancy(
            db,
            discrepancy.id,
            resolution_type="VENDOR_CAUSED",
            resolution_notes="trying to reopen",
            caller_store_id=None,
            actor_id=None,
        )


def test_session_f_resolve_from_recorded_directly_rejected(db: Session) -> None:
    """docs/M24D_TECHNICAL_CONTRACT.md Section 12.1: "a discrepancy
    cannot be closed out of RECORDED directly" -- resolve requires
    INVESTIGATING, never RECORDED."""
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.status == "RECORDED"

    with pytest.raises(ConflictError):
        discrepancy_service.resolve_discrepancy(
            db,
            discrepancy.id,
            resolution_type="UNKNOWN",
            resolution_notes="n/a",
            caller_store_id=None,
            actor_id=None,
        )


def test_session_f_invalid_resolution_type_rejected(db: Session) -> None:
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )

    with pytest.raises(ValidationAppError):
        discrepancy_service.resolve_discrepancy(
            db,
            discrepancy.id,
            resolution_type="NOT_A_REAL_FINDING",
            resolution_notes="n/a",
            caller_store_id=None,
            actor_id=None,
        )


def test_session_f_empty_resolution_notes_rejected(db: Session) -> None:
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )

    with pytest.raises(ValidationAppError):
        discrepancy_service.resolve_discrepancy(
            db,
            discrepancy.id,
            resolution_type="UNKNOWN",
            resolution_notes="   ",
            caller_store_id=None,
            actor_id=None,
        )


def test_session_f_db_rejects_negative_quantity(db: Session) -> None:
    """Defense in depth below the service layer: the CHECK constraint
    itself refuses a negative quantity even if some future code path
    tried to bypass the service."""
    store_b = make_store(db)
    product = make_product(db, store_b)
    db.commit()
    with pytest.raises((IntegrityError, DataError)):
        db.execute(
            ReceivingDiscrepancy.__table__.insert().values(
                source_type="TRANSFER_RECEIPT",
                source_id=999999,
                product_id=product.id,
                store_id=store_b.id,
                discrepancy_type="SHORTAGE",
                quantity_short=Decimal("-5"),
                quantity_damaged=Decimal("0"),
                unit_cost=Decimal("4.00"),
                status="RECORDED",
                raised_at=discrepancy_service.datetime.now(discrepancy_service.UTC),
            )
        )
    db.rollback()


def test_session_f_db_rejects_all_zero_quantity(db: Session) -> None:
    store_b = make_store(db)
    product = make_product(db, store_b)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            ReceivingDiscrepancy.__table__.insert().values(
                source_type="TRANSFER_RECEIPT",
                source_id=999998,
                product_id=product.id,
                store_id=store_b.id,
                discrepancy_type="SHORTAGE",
                quantity_short=Decimal("0"),
                quantity_damaged=Decimal("0"),
                unit_cost=Decimal("4.00"),
                status="RECORDED",
                raised_at=discrepancy_service.datetime.now(discrepancy_service.UTC),
            )
        )
    db.rollback()


def test_session_f_quantity_cannot_be_altered_via_resolve(db: Session) -> None:
    """ "Quantity exceeding the original discrepancy" is structurally
    impossible in Phase 3: DiscrepancyResolveRequest/resolve_discrepancy
    accept no quantity field at all, and a raw client-supplied
    quantity_short in the request body is simply ignored."""
    _, _, _, _, _, _, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )

    import inspect

    assert (
        "quantity_short"
        not in inspect.signature(discrepancy_service.resolve_discrepancy).parameters
    )
    assert (
        "quantity_damaged"
        not in inspect.signature(discrepancy_service.resolve_discrepancy).parameters
    )


def test_session_f_resolving_one_discrepancy_never_touches_another(db: Session) -> None:
    """ "Resolution against another transfer's discrepancy": resolving
    discrepancy A by id must never affect discrepancy B, even one on a
    completely different transfer."""
    _, _, _, _, _, _, receipt_id_a = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    _, _, _, _, _, _, receipt_id_b = _ship_and_receive(
        db, quantity_received=Decimal("95"), quantity_damaged=Decimal("5")
    )
    discrepancy_a = _get_discrepancy_for_receipt(db, receipt_id_a)
    discrepancy_b = _get_discrepancy_for_receipt(db, receipt_id_b)
    assert discrepancy_a is not None and discrepancy_b is not None
    assert discrepancy_a.id != discrepancy_b.id

    discrepancy_service.investigate_discrepancy(
        db, discrepancy_a.id, caller_store_id=None, actor_id=None
    )
    discrepancy_service.resolve_discrepancy(
        db,
        discrepancy_a.id,
        resolution_type="UNKNOWN",
        resolution_notes="only A",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()

    db.refresh(discrepancy_b)
    assert discrepancy_b.status == "RECORDED"
    assert discrepancy_b.resolved_at is None
    assert discrepancy_b.quantity_damaged == Decimal("5")


# --- Session G: idempotency -------------------------------------------------


def test_session_g_discrepancy_creation_is_idempotent_within_one_receive_call(
    db: Session,
) -> None:
    """A retried receive_transfer request (same client_transaction_id)
    returns the SAME receipt without re-running the creation logic at
    all -- proven here by calling receive_transfer twice with the same
    client_transaction_id and confirming exactly one discrepancy row."""
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("1000")
    )
    make_product(db, store_b, sku=source.sku)
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("100"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("100"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    client_transaction_id = f"recv-{unique_suffix()}"
    receipt_1 = transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=Decimal("90"),
                quantity_declared_short=Decimal("10"),
            )
        ],
        client_transaction_id=client_transaction_id,
        caller_store_id=None,
    )
    db.commit()
    receipt_2 = transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[
            ReceiveLineInput(
                transfer_line_id=line_id,
                quantity_received=Decimal("90"),
                quantity_declared_short=Decimal("10"),
            )
        ],
        client_transaction_id=client_transaction_id,
        caller_store_id=None,
    )
    assert receipt_1.id == receipt_2.id

    discrepancies = (
        db.execute(
            select(ReceivingDiscrepancy).where(ReceivingDiscrepancy.transfer_id == transfer.id)
        )
        .scalars()
        .all()
    )
    assert len(discrepancies) == 1


def test_session_g_direct_duplicate_creation_call_is_idempotent(db: Session) -> None:
    """Even a direct, out-of-band call to create_discrepancy_for_receipt_item
    with the SAME receipt_item_id can create at most one row -- the
    UniqueConstraint backstop, independent of receive_transfer's own
    idempotency."""
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    second = discrepancy_service.create_discrepancy_for_receipt_item(
        db,
        receipt_item_id=discrepancy.source_id,
        transfer_id=discrepancy.transfer_id,
        product_id=discrepancy.product_id,
        store_id=discrepancy.store_id,
        counterparty_store_id=discrepancy.counterparty_store_id,
        quantity_short=Decimal("10"),
        quantity_damaged=Decimal("0"),
        unit_cost=discrepancy.unit_cost,
        raised_by=None,
        raised_at=discrepancy.raised_at,
    )
    assert second is not None
    assert second.id == discrepancy.id

    all_rows = (
        db.execute(
            select(ReceivingDiscrepancy).where(
                ReceivingDiscrepancy.source_id == discrepancy.source_id
            )
        )
        .scalars()
        .all()
    )
    assert len(all_rows) == 1


def test_session_g_concurrent_duplicate_creation_creates_exactly_one() -> None:
    """Real concurrent connections racing to create a discrepancy for the
    SAME receipt item -- mirrors
    test_m25_phase2_receiving_custody.py's own duplicate-receipt race,
    applied to the discrepancy ledger's own idempotency guarantee. Uses
    the cross-connection setup helper (not the `db` fixture) since real
    threads need the setup data to be genuinely committed and visible
    (see tests/test_m25_phase2_receiving_custody.py's own precedent for
    why the `db` fixture's savepoint-based commit can't be used here)."""
    discrepancy_id = _ship_and_receive_cross_connection()
    setup = SessionLocal()
    try:
        discrepancy = setup.get(ReceivingDiscrepancy, discrepancy_id)
        assert discrepancy is not None
        source_id = discrepancy.source_id
        transfer_id = discrepancy.transfer_id
        product_id = discrepancy.product_id
        store_id = discrepancy.store_id
        counterparty_store_id = discrepancy.counterparty_store_id
        unit_cost = discrepancy.unit_cost
        raised_at = discrepancy.raised_at
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[str] = []

    def _attempt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            row = discrepancy_service.create_discrepancy_for_receipt_item(
                session,
                receipt_item_id=source_id,
                transfer_id=transfer_id,
                product_id=product_id,
                store_id=store_id,
                counterparty_store_id=counterparty_store_id,
                quantity_short=Decimal("10"),
                quantity_damaged=Decimal("0"),
                unit_cost=unit_cost,
                raised_by=None,
                raised_at=raised_at,
            )
            session.commit()
            assert row is not None
            results.append(row.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert len(results) == 2
    assert results[0] == results[1] == discrepancy_id

    verify = SessionLocal()
    try:
        count = (
            verify.execute(
                select(ReceivingDiscrepancy).where(ReceivingDiscrepancy.source_id == source_id)
            )
            .scalars()
            .all()
        )
        assert len(count) == 1
    finally:
        verify.close()


# --- Session H: authorization / store isolation -----------------------------


def test_session_h_investigate_wrong_store_rejected(db: Session) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    with pytest.raises(ForbiddenError):
        discrepancy_service.investigate_discrepancy(
            db, discrepancy.id, caller_store_id=store_a.id, actor_id=None
        )


def test_session_h_source_store_cannot_investigate(db: Session) -> None:
    """The source store (from_store_id) has no claim on a discrepancy
    discovered at the destination -- only the destination store_id (or
    an unrestricted/company-wide caller) may act on it."""
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    assert discrepancy.store_id == store_b.id

    with pytest.raises(ForbiddenError):
        discrepancy_service.investigate_discrepancy(
            db, discrepancy.id, caller_store_id=store_a.id, actor_id=None
        )
    # The correct (destination) store succeeds.
    result = discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=store_b.id, actor_id=None
    )
    assert result.status == "INVESTIGATING"


def test_session_h_resolve_wrong_store_rejected(db: Session) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )

    with pytest.raises(ForbiddenError):
        discrepancy_service.resolve_discrepancy(
            db,
            discrepancy.id,
            resolution_type="UNKNOWN",
            resolution_notes="n/a",
            caller_store_id=store_a.id,
            actor_id=None,
        )


def test_session_h_list_scoped_to_caller_store(db: Session) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    scoped_to_wrong_store = discrepancy_service.list_discrepancies(db, store_id=store_a.id)
    assert discrepancy.id not in {d.id for d in scoped_to_wrong_store}
    scoped_to_right_store = discrepancy_service.list_discrepancies(db, store_id=store_b.id)
    assert discrepancy.id in {d.id for d in scoped_to_right_store}


def test_session_h_api_get_wrong_store_not_found_not_forbidden(
    client: TestClient, db: Session
) -> None:
    """No information leakage: a cross-store GET returns the same
    404 a nonexistent discrepancy would, never a 403 that would confirm
    existence -- mirrors app.api.v1.endpoints.transfers.get_transfer's
    own precedent."""
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    username = f"clerk_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get(f"/api/v1/discrepancies/{discrepancy.id}", headers=headers)
    assert response.status_code == 404
    missing_response = client.get("/api/v1/discrepancies/99999999", headers=headers)
    assert missing_response.status_code == 404


def test_session_h_nonexistent_discrepancy_rejected(db: Session) -> None:
    with pytest.raises(NotFoundError):
        discrepancy_service.get_discrepancy(db, 99999999)
    with pytest.raises(NotFoundError):
        discrepancy_service.investigate_discrepancy(
            db, 99999999, caller_store_id=None, actor_id=None
        )
    with pytest.raises(NotFoundError):
        discrepancy_service.resolve_discrepancy(
            db,
            99999999,
            resolution_type="UNKNOWN",
            resolution_notes="n/a",
            caller_store_id=None,
            actor_id=None,
        )


def test_session_h_api_unauthorized_user_cannot_investigate(
    client: TestClient, db: Session
) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    username = f"cashier_{unique_suffix()}"
    make_user_with_role(db, store_b, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(f"/api/v1/discrepancies/{discrepancy.id}/investigate", headers=headers)
    assert response.status_code == 403


def test_session_h_api_cross_store_investigate_rejected(client: TestClient, db: Session) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None
    username = f"clerk_{unique_suffix()}"
    make_user_with_role(db, store_a, INVENTORY_CLERK, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(f"/api/v1/discrepancies/{discrepancy.id}/investigate", headers=headers)
    assert response.status_code == 403


# --- Session I: accounting regression ---------------------------------------


def test_session_i_discrepancy_creation_does_not_affect_gl_or_pnl(db: Session) -> None:
    transfer_id, line_id, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db,
        shipped_qty=Decimal("100"),
        quantity_received=Decimal("88"),
        quantity_damaged=Decimal("2"),
        quantity_declared_short=Decimal("10"),
        unit_cost=Decimal("4.00"),
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    trial_balance = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])
    total_debits = sum((row.total_debit for row in trial_balance), Decimal("0"))
    total_credits = sum((row.total_credit for row in trial_balance), Decimal("0"))
    assert total_debits == total_credits  # GL stays balanced

    pnl = accounting_service.profit_and_loss(
        db, date_from=date(2024, 1, 1), date_to=date(2024, 12, 31), store_ids=[store_b.id]
    )
    assert pnl.net_income == Decimal("0")  # no revenue or expense from a pure transfer receipt

    reconciliation = transfer_service.inventory_in_transit_reconciliation(db)
    assert reconciliation.discrepancy == Decimal("0")
    # The damaged+short value (12 * 4.00 = 48.00) still sits in transit --
    # neither resolved nor written off by mere discrepancy creation.
    # Scoped to this test's own line (see test_session_b_shortage).
    line = db.get(InterStoreTransferLine, line_id)
    this_line_outstanding = (
        line.shipped_quantity - line.received_quantity
    ) * line.unit_cost_at_shipment
    assert this_line_outstanding == Decimal("48.00")


def test_session_i_resolution_does_not_affect_gl(db: Session) -> None:
    _, _, store_a, store_b, source, dest, receipt_id = _ship_and_receive(
        db, quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy = _get_discrepancy_for_receipt(db, receipt_id)
    assert discrepancy is not None

    before = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])

    discrepancy_service.investigate_discrepancy(
        db, discrepancy.id, caller_store_id=None, actor_id=None
    )
    discrepancy_service.resolve_discrepancy(
        db,
        discrepancy.id,
        resolution_type="VENDOR_CAUSED",
        resolution_notes="Supplier shorted the source store's own PO.",
        caller_store_id=None,
        actor_id=None,
    )
    db.commit()

    after = accounting_service.trial_balance(db, store_ids=[store_a.id, store_b.id])
    before_by_code = {row.account_code: (row.total_debit, row.total_credit) for row in before}
    after_by_code = {row.account_code: (row.total_debit, row.total_credit) for row in after}
    assert before_by_code == after_by_code  # byte-identical -- resolving posts nothing


# --- Session J: failure injection --------------------------------------------


def test_session_j_failed_receive_leaves_no_discrepancy(db: Session) -> None:
    """A receive_transfer call that raises before it commits (e.g. an
    over-receipt it itself rejects) must leave no partial discrepancy
    state behind."""
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("1000")
    )
    make_product(db, store_b, sku=source.sku)
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("100"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("100"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    before_count = len(db.execute(select(ReceivingDiscrepancy.id)).all())
    with pytest.raises(ConflictError):
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer.id,
            received_date=date(2024, 1, 2),
            lines=[
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("50"),
                    quantity_declared_short=Decimal("60"),  # 50+60 > 100 shipped
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()
    after_count = len(db.execute(select(ReceivingDiscrepancy.id)).all())
    assert after_count == before_count


def test_session_j_retry_after_failure_succeeds_cleanly(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku=f"SKU-{unique_suffix()}", current_qty_on_hand=Decimal("1000")
    )
    make_product(db, store_b, sku=source.sku)
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("100"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("100"))],
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
                ReceiveLineInput(
                    transfer_line_id=line_id,
                    quantity_received=Decimal("50"),
                    quantity_declared_short=Decimal("60"),
                )
            ],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    db.rollback()

    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
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
    discrepancy = _get_discrepancy_for_receipt(db, receipt.id)
    assert discrepancy is not None
    assert discrepancy.quantity_short == Decimal("10")


# --- Session K: concurrency --------------------------------------------------


def test_session_k_concurrent_investigate_exactly_one_wins() -> None:
    discrepancy_id = _ship_and_receive_cross_connection()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def _attempt() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            discrepancy_service.investigate_discrepancy(
                session, discrepancy_id, caller_store_id=None, actor_id=None
            )
            session.commit()
            results.append("OK")
        except ConflictError:
            session.rollback()
            results.append("ALREADY_CLAIMED")
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["ALREADY_CLAIMED", "OK"]

    verify = SessionLocal()
    try:
        final = verify.get(ReceivingDiscrepancy, discrepancy_id)
        assert final is not None
        assert final.status == "INVESTIGATING"
    finally:
        verify.close()


def test_session_k_concurrent_resolve_exactly_one_wins() -> None:
    discrepancy_id = _ship_and_receive_cross_connection()
    setup = SessionLocal()
    try:
        discrepancy_service.investigate_discrepancy(
            setup, discrepancy_id, caller_store_id=None, actor_id=None
        )
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def _attempt(resolution_type: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            discrepancy_service.resolve_discrepancy(
                session,
                discrepancy_id,
                resolution_type=resolution_type,
                resolution_notes=f"resolved as {resolution_type}",
                caller_store_id=None,
                actor_id=None,
            )
            session.commit()
            results.append("OK")
        except ConflictError:
            session.rollback()
            results.append("ALREADY_RESOLVED")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=("VENDOR_CAUSED",)),
        threading.Thread(target=_attempt, args=("SOURCE_STORE_CAUSED",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["ALREADY_RESOLVED", "OK"]

    verify = SessionLocal()
    try:
        final = verify.get(ReceivingDiscrepancy, discrepancy_id)
        assert final is not None
        assert final.status == "RESOLVED"
        # Exactly one resolution_type was recorded, not a mix of both.
        assert final.resolution_type in ("VENDOR_CAUSED", "SOURCE_STORE_CAUSED")
    finally:
        verify.close()


def test_session_k_concurrent_investigate_on_different_discrepancies_both_succeed() -> None:
    """Independence: locking discrepancy A never blocks a concurrent
    claim on discrepancy B."""
    discrepancy_id_a = _ship_and_receive_cross_connection(
        quantity_received=Decimal("90"), quantity_declared_short=Decimal("10")
    )
    discrepancy_id_b = _ship_and_receive_cross_connection(
        quantity_received=Decimal("95"),
        quantity_damaged=Decimal("5"),
        quantity_declared_short=Decimal("0"),
    )

    barrier = threading.Barrier(2)
    results: list[str] = []

    def _attempt(discrepancy_id: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            discrepancy_service.investigate_discrepancy(
                session, discrepancy_id, caller_store_id=None, actor_id=None
            )
            session.commit()
            results.append("OK")
        except Exception as exc:  # noqa: BLE001
            results.append(repr(exc))
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=(discrepancy_id_a,)),
        threading.Thread(target=_attempt, args=(discrepancy_id_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == ["OK", "OK"]
