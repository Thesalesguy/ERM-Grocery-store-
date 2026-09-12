"""M8 Section 4: inter-store transfer lifecycle, cost determinism, partial
shipment/receipt, cancellation, and in-transit reconciliation —
domain-level (calling app.modules.transfers.service directly). See
docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decisions 6-9"."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.modules.accounting.constants import ACCOUNT_INVENTORY, ACCOUNT_INVENTORY_IN_TRANSIT
from app.modules.accounting.models import Account, JournalEntry, JournalLine
from app.modules.inventory.models import InventoryMovement
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store, unique_suffix


def test_create_transfer_matches_destination_product_by_sku(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-MATCH")
    destination = make_product(db, store_b, sku="SKU-MATCH")
    db.commit()

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    assert transfer.status == "DRAFT"
    (line,) = transfer.lines
    assert line.destination_product_id == destination.id
    assert line.shipped_quantity == Decimal("0")
    assert line.received_quantity == Decimal("0")


def test_create_transfer_rejects_when_no_destination_sku_match(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-ORPHAN")
    db.commit()

    with pytest.raises(NotFoundError) as exc_info:
        transfer_service.create_transfer(
            db,
            from_store_id=store_a.id,
            to_store_id=store_b.id,
            requested_date=date(2024, 1, 1),
            lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("1"))],
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "DESTINATION_PRODUCT_NOT_FOUND"


def test_create_transfer_rejects_same_store(db: Session) -> None:
    store = make_store(db)
    product = make_product(db, store)
    db.commit()
    with pytest.raises(ValidationAppError) as exc_info:
        transfer_service.create_transfer(
            db,
            from_store_id=store.id,
            to_store_id=store.id,
            requested_date=date(2024, 1, 1),
            lines=[
                TransferLineInput(source_product_id=product.id, requested_quantity=Decimal("1"))
            ],
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "SAME_STORE_TRANSFER"


def test_ship_freezes_unit_cost_and_moves_inventory_out(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db, store_a, sku="SKU-SHIP", current_qty_on_hand=Decimal("50"), current_cost=Decimal("7.50")
    )
    make_product(db, store_b, sku="SKU-SHIP")
    db.commit()

    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("20"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id

    # Source cost changes AFTER shipment must not retroactively change the
    # frozen shipment cost (Design Decision 9).
    shipped = transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("20"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(source)

    assert shipped.status == "SHIPPED"
    (line,) = shipped.lines
    assert line.unit_cost_at_shipment == Decimal("7.500000")
    assert line.shipped_quantity == Decimal("20")
    assert source.current_qty_on_hand == Decimal("30")

    movement = (
        db.query(InventoryMovement)
        .filter_by(
            reference_type="inter_store_transfer",
            reference_id=transfer.id,
            movement_type="TRANSFER_OUT",
        )
        .one()
    )
    assert movement.quantity_delta == Decimal("-20")

    journal_entry = (
        db.query(JournalEntry)
        .filter_by(source_type="INTER_STORE_TRANSFER_SHIP", source_id=transfer.id)
        .one()
    )
    lines = db.query(JournalLine).filter_by(journal_entry_id=journal_entry.id).all()
    by_account = {db.get(Account, jl.account_id).code: jl for jl in lines}
    assert by_account[ACCOUNT_INVENTORY_IN_TRANSIT].debit == Decimal("150.000000")
    assert by_account[ACCOUNT_INVENTORY].credit == Decimal("150.000000")


def test_ship_rejects_over_shipment(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-OVER", current_qty_on_hand=Decimal("10"))
    make_product(db, store_b, sku="SKU-OVER")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("5"))],
        caller_store_id=None,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.ship_transfer(
            db,
            transfer_id=transfer.id,
            lines=[
                ShipLineInput(transfer_line_id=transfer.lines[0].id, quantity_to_ship=Decimal("6"))
            ],
            client_transaction_id=f"ship-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_SHIPMENT"


def test_ship_is_idempotent_by_client_transaction_id(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-IDEM", current_qty_on_hand=Decimal("10"))
    make_product(db, store_b, sku="SKU-IDEM")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("5"))],
        caller_store_id=None,
    )
    db.commit()
    key = f"ship-{unique_suffix()}"
    line_id = transfer.lines[0].id

    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("5"))],
        client_transaction_id=key,
        caller_store_id=None,
    )
    db.commit()
    db.refresh(source)
    qty_after_first_ship = source.current_qty_on_hand

    # Retry with the SAME key must not ship again.
    again = transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("5"))],
        client_transaction_id=key,
        caller_store_id=None,
    )
    db.commit()
    db.refresh(source)
    assert again.id == transfer.id
    assert source.current_qty_on_hand == qty_after_first_ship


def test_ship_rejects_shipping_twice_with_different_keys(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-TWICE", current_qty_on_hand=Decimal("10"))
    make_product(db, store_b, sku="SKU-TWICE")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("5"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("5"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.ship_transfer(
            db,
            transfer_id=transfer.id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("1"))],
            client_transaction_id=f"ship-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "INVALID_TRANSFER_STATE"


def test_receive_uses_frozen_shipment_cost_not_destination_current_wac(db: Session) -> None:
    """The central M8 requirement: the receiving store's WAC recompute
    must use the FROZEN unit_cost_at_shipment, never be recomputed from
    the destination's own current WAC — even when they differ a lot."""
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db,
        store_a,
        sku="SKU-WAC",
        current_qty_on_hand=Decimal("100"),
        current_cost=Decimal("10.00"),
    )
    destination = make_product(
        db, store_b, sku="SKU-WAC", current_qty_on_hand=Decimal("50"), current_cost=Decimal("2.00")
    )
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("50"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("50"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    # The source's cost also drifts AFTER shipment, before the receipt —
    # this must have NO effect: the receipt uses the FROZEN
    # unit_cost_at_shipment (10.00), never the source's current cost.
    source.current_cost = Decimal("999.00")
    db.commit()

    receipt = transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("50"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    db.refresh(destination)

    # Expected WAC uses the destination's own existing (qty=50, wac=2.00)
    # combined with the FROZEN shipment cost (10.00, not the drifted
    # source cost of 999.00 or any other value): (50*2.00 + 50*10.00) / 100 = 6.00
    assert destination.current_cost == Decimal("6.000000")
    assert destination.current_qty_on_hand == Decimal("100")
    assert receipt.transfer_id == transfer.id


def test_receive_rejects_over_receipt(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-OVERR", current_qty_on_hand=Decimal("10"))
    make_product(db, store_b, sku="SKU-OVERR")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("5"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.receive_transfer(
            db,
            transfer_id=transfer.id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("6"))],
            client_transaction_id=f"recv-{unique_suffix()}",
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OVER_RECEIPT"


def test_partial_shipment_and_multiple_partial_receipts(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-PARTIAL", current_qty_on_hand=Decimal("100"))
    make_product(db, store_b, sku="SKU-PARTIAL")
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
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("60"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("25"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 3),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("35"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    refreshed = transfer_service.get_transfer(db, transfer.id)
    (line,) = refreshed.lines
    assert line.shipped_quantity == Decimal("60")
    assert line.received_quantity == Decimal("60")  # fully received of what was shipped
    assert line.requested_quantity == Decimal("100")  # not fully shipped

    receipts = transfer_service.list_transfer_receipts(db, transfer_id=transfer.id)
    assert len(receipts) == 2


def test_cancel_only_allowed_while_draft(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(db, store_a, sku="SKU-CANCEL", current_qty_on_hand=Decimal("10"))
    make_product(db, store_b, sku="SKU-CANCEL")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()

    cancelled = transfer_service.cancel_transfer(
        db, transfer.id, actor_id=None, caller_store_id=None, reason="mistake"
    )
    assert cancelled.status == "CANCELLED"

    transfer2 = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer2.id,
        lines=[
            ShipLineInput(transfer_line_id=transfer2.lines[0].id, quantity_to_ship=Decimal("10"))
        ],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    with pytest.raises(ConflictError) as exc_info:
        transfer_service.cancel_transfer(
            db, transfer2.id, actor_id=None, caller_store_id=None, reason="too late"
        )
    assert exc_info.value.error_code == "INVALID_TRANSFER_STATE"


def test_store_scoped_caller_cannot_create_transfer_touching_other_stores(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    store_c = make_store(db)
    source = make_product(db, store_a, sku="SKU-SCOPE")
    db.commit()
    with pytest.raises(ForbiddenError):
        transfer_service.create_transfer(
            db,
            from_store_id=store_a.id,
            to_store_id=store_b.id,
            requested_date=date(2024, 1, 1),
            lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("1"))],
            caller_store_id=store_c.id,
        )


def test_in_transit_reconciliation_matches_outstanding_lines(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    source = make_product(
        db,
        store_a,
        sku="SKU-RECON",
        current_qty_on_hand=Decimal("100"),
        current_cost=Decimal("5.00"),
    )
    make_product(db, store_b, sku="SKU-RECON")
    db.commit()
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date(2024, 1, 1),
        lines=[TransferLineInput(source_product_id=source.id, requested_quantity=Decimal("40"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("40"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date(2024, 1, 2),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("15"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    row = transfer_service.inventory_in_transit_reconciliation(db)
    # 25 units still outstanding at the frozen 5.00 shipment cost = 125.00.
    assert row.outstanding_in_transit_total >= Decimal("125.000000")
    assert row.discrepancy == row.gl_in_transit_balance - row.outstanding_in_transit_total
