"""M8 Section 5 (mandatory): inter-store transfer concurrency, proven
against real PostgreSQL with genuinely independent connections (same
discipline as tests/test_purchasing_concurrency.py).

Races covered:
  A. Two users try to ship the SAME transfer concurrently — exactly one
     ships; the other sees it already SHIPPED (idempotent-by-state), no
     double TRANSFER_OUT movement.
  B. Two users try to receive OVERLAPPING quantities of the SAME shipped
     line concurrently, together exceeding what was shipped — the
     over-receipt must be rejected for whichever request's total would
     cross the line, never silently double-counted.
  C. Shipment races with an unrelated sale-equivalent stock adjustment
     against the SAME source product — must serialize correctly via the
     product lock, final quantity reflects both changes exactly.
  D. Concurrent duplicate ship requests with the SAME client_transaction_id
     — exactly one TRANSFER_OUT movement results.
  E. Concurrent duplicate receive requests with the SAME
     client_transaction_id — exactly one receipt results.
Each race is run 5 times where timing-sensitive, per the task's explicit
instruction.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.exceptions import ConflictError
from app.db.session import SessionLocal
from app.modules.inventory.models import InventoryMovement
from app.modules.transfers import service as transfer_service
from app.modules.transfers.models import InterStoreTransferReceipt
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from tests.factories import make_product, make_store


@dataclass
class _Outcome:
    succeeded: bool = False
    error_code: str | None = None
    unexpected_error: str | None = None
    resulting_id: int | None = None


def _attempt_ship(
    *,
    transfer_id: int,
    line_id: int,
    quantity: Decimal,
    client_transaction_id: str,
    barrier: threading.Barrier,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        transfer = transfer_service.ship_transfer(
            session,
            transfer_id=transfer_id,
            lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=quantity)],
            client_transaction_id=client_transaction_id,
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.resulting_id = transfer.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _attempt_receive(
    *,
    transfer_id: int,
    line_id: int,
    quantity: Decimal,
    client_transaction_id: str,
    barrier: threading.Barrier,
    result: _Outcome,
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        receipt = transfer_service.receive_transfer(
            session,
            transfer_id=transfer_id,
            received_date=date(2024, 1, 2),
            lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=quantity)],
            client_transaction_id=client_transaction_id,
            caller_store_id=None,
        )
        session.commit()
        result.succeeded = True
        result.resulting_id = receipt.id
    except ConflictError as exc:
        session.rollback()
        result.succeeded = False
        result.error_code = exc.error_code
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.unexpected_error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def _setup_transfer(quantity: Decimal = Decimal("50")) -> tuple[int, int, int]:
    setup = SessionLocal()
    try:
        store_a = make_store(setup)
        store_b = make_store(setup)
        source = make_product(
            setup, store_a, sku=f"SKU-{uuid.uuid4().hex[:8]}", current_qty_on_hand=quantity
        )
        setup.commit()
        make_product(setup, store_b, sku=source.sku)
        setup.commit()
        transfer = transfer_service.create_transfer(
            setup,
            from_store_id=store_a.id,
            to_store_id=store_b.id,
            requested_date=date(2024, 1, 1),
            lines=[TransferLineInput(source_product_id=source.id, requested_quantity=quantity)],
            caller_store_id=None,
        )
        setup.commit()
        return transfer.id, transfer.lines[0].id, source.id
    finally:
        setup.close()


def test_a_five_iterations_two_concurrent_shipments_of_same_transfer() -> None:
    """A: exactly one of two concurrent ship attempts on the SAME DRAFT
    transfer must actually ship — the loser sees INVALID_TRANSFER_STATE
    (already SHIPPED), never a double TRANSFER_OUT movement."""
    for _ in range(5):
        transfer_id, line_id, source_product_id = _setup_transfer(Decimal("50"))

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_ship,
            kwargs=dict(
                transfer_id=transfer_id,
                line_id=line_id,
                quantity=Decimal("50"),
                client_transaction_id=f"ship-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_ship,
            kwargs=dict(
                transfer_id=transfer_id,
                line_id=line_id,
                quantity=Decimal("50"),
                client_transaction_id=f"ship-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_b,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        # Exactly one succeeds; the other is rejected as already-shipped.
        outcomes = [result_a.succeeded, result_b.succeeded]
        assert outcomes.count(True) == 1, (result_a, result_b)
        loser = result_a if not result_a.succeeded else result_b
        assert loser.error_code == "INVALID_TRANSFER_STATE"

        verify = SessionLocal()
        try:
            movements = (
                verify.query(InventoryMovement)
                .filter_by(
                    reference_type="inter_store_transfer",
                    reference_id=transfer_id,
                    movement_type="TRANSFER_OUT",
                )
                .all()
            )
            assert len(movements) == 1, "exactly one TRANSFER_OUT, never duplicated"
        finally:
            verify.close()


def test_b_five_iterations_overlapping_receives_never_exceed_shipped_quantity() -> None:
    """B: two receive attempts against the same line, each individually
    valid but together exceeding shipped_quantity — the second to acquire
    the lock must see the reduced remaining quantity and be rejected,
    never allowed to push received_quantity past shipped_quantity."""
    for _ in range(5):
        transfer_id, line_id, source_product_id = _setup_transfer(Decimal("50"))
        ship_result = _Outcome()
        _attempt_ship(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("50"),
            client_transaction_id=f"ship-{uuid.uuid4().hex}",
            barrier=threading.Barrier(1),
            result=ship_result,
        )
        assert ship_result.succeeded, ship_result

        barrier = threading.Barrier(2)
        result_a, result_b = _Outcome(), _Outcome()
        thread_a = threading.Thread(
            target=_attempt_receive,
            kwargs=dict(
                transfer_id=transfer_id,
                line_id=line_id,
                quantity=Decimal("30"),
                client_transaction_id=f"recv-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_a,
            ),
        )
        thread_b = threading.Thread(
            target=_attempt_receive,
            kwargs=dict(
                transfer_id=transfer_id,
                line_id=line_id,
                quantity=Decimal("30"),
                client_transaction_id=f"recv-{uuid.uuid4().hex}",
                barrier=barrier,
                result=result_b,
            ),
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert result_a.unexpected_error is None, result_a
        assert result_b.unexpected_error is None, result_b
        # 30 + 30 = 60 > 50 shipped: exactly one must be rejected as OVER_RECEIPT.
        outcomes = [result_a.succeeded, result_b.succeeded]
        assert outcomes.count(True) == 1, (result_a, result_b)
        loser = result_a if not result_a.succeeded else result_b
        assert loser.error_code == "OVER_RECEIPT"

        verify = SessionLocal()
        try:
            from app.modules.transfers.models import InterStoreTransferLine

            line = verify.get(InterStoreTransferLine, line_id)
            assert line.received_quantity == Decimal("30")  # never 60
        finally:
            verify.close()


def test_c_shipment_concurrent_with_unrelated_adjustment_on_source_product() -> None:
    """C: a shipment and an unrelated stock adjustment race on the SAME
    source product — both go through lock_product_for_update, so the
    final on-hand quantity must reflect both changes exactly, in either
    order."""
    transfer_id, line_id, source_product_id = _setup_transfer(Decimal("100"))
    from app.modules.inventory import service as inventory_service

    barrier = threading.Barrier(2)
    ship_result = _Outcome()
    adjustment_error: list[str] = []

    def _do_ship() -> None:
        _attempt_ship(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("40"),
            client_transaction_id=f"ship-{uuid.uuid4().hex}",
            barrier=barrier,
            result=ship_result,
        )

    def _do_adjustment() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            from app.modules.products.models import Product

            product = session.get(Product, source_product_id)
            inventory_service.create_stock_adjustment(
                session,
                store_id=product.store_id,
                product_id=source_product_id,
                quantity_delta=Decimal("-10"),
                reason_code="OTHER",
                notes=None,
                created_by=None,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            adjustment_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    t_ship = threading.Thread(target=_do_ship)
    t_adjust = threading.Thread(target=_do_adjustment)
    t_ship.start()
    t_adjust.start()
    t_ship.join(timeout=15)
    t_adjust.join(timeout=15)

    assert ship_result.unexpected_error is None, ship_result
    assert ship_result.succeeded, ship_result
    assert adjustment_error == [], adjustment_error

    verify = SessionLocal()
    try:
        from app.modules.products.models import Product

        product = verify.get(Product, source_product_id)
        # 100 - 40 (ship) - 10 (adjustment) = 50, regardless of order.
        assert product.current_qty_on_hand == Decimal("50")
    finally:
        verify.close()


def test_d_concurrent_duplicate_ship_requests_create_only_one_shipment() -> None:
    """D: two threads submit a ship request against the SAME transfer with
    the SAME client_transaction_id at the same instant — exactly one
    TRANSFER_OUT movement must result."""
    transfer_id, line_id, source_product_id = _setup_transfer(Decimal("20"))
    shared_key = f"ship-{uuid.uuid4().hex}"

    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_ship,
        kwargs=dict(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("20"),
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_ship,
        kwargs=dict(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("20"),
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.resulting_id == result_b.resulting_id

    verify = SessionLocal()
    try:
        movements = (
            verify.query(InventoryMovement)
            .filter_by(
                reference_type="inter_store_transfer",
                reference_id=transfer_id,
                movement_type="TRANSFER_OUT",
            )
            .all()
        )
        assert len(movements) == 1
        from app.modules.products.models import Product

        product = verify.get(Product, source_product_id)
        assert product.current_qty_on_hand == Decimal("0")  # only ONE shipment applied
    finally:
        verify.close()


def test_e_concurrent_duplicate_receive_requests_create_only_one_receipt() -> None:
    """E: two threads submit a receive request against the SAME shipped
    line with the SAME client_transaction_id at the same instant —
    exactly one InterStoreTransferReceipt must result."""
    transfer_id, line_id, source_product_id = _setup_transfer(Decimal("20"))
    ship_result = _Outcome()
    _attempt_ship(
        transfer_id=transfer_id,
        line_id=line_id,
        quantity=Decimal("20"),
        client_transaction_id=f"ship-{uuid.uuid4().hex}",
        barrier=threading.Barrier(1),
        result=ship_result,
    )
    assert ship_result.succeeded, ship_result

    shared_key = f"recv-{uuid.uuid4().hex}"
    barrier = threading.Barrier(2)
    result_a, result_b = _Outcome(), _Outcome()
    thread_a = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("20"),
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_a,
        ),
    )
    thread_b = threading.Thread(
        target=_attempt_receive,
        kwargs=dict(
            transfer_id=transfer_id,
            line_id=line_id,
            quantity=Decimal("20"),
            client_transaction_id=shared_key,
            barrier=barrier,
            result=result_b,
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert result_a.unexpected_error is None, result_a
    assert result_b.unexpected_error is None, result_b
    assert result_a.succeeded and result_b.succeeded
    assert result_a.resulting_id == result_b.resulting_id

    verify = SessionLocal()
    try:
        count = (
            verify.query(InterStoreTransferReceipt)
            .filter_by(client_transaction_id=shared_key)
            .count()
        )
        assert count == 1
    finally:
        verify.close()
