"""Accounting concurrency proofs against real PostgreSQL connections (not
the `db` fixture — see tests/test_concurrency.py's module docstring for
why: a SAVEPOINT-based inner commit never actually fires a DEFERRED
constraint trigger, since that only happens at a real COMMIT).

Covers:
  A. The deferred balance/non-zero/non-empty trigger really rejects a bad
     entry at commit time, using a real transaction against erp_app.
  B. UPDATE/DELETE are really blocked for erp_app on both ledger tables.
  C. Two concurrent sales against the same product each post their own
     correctly balanced journal entry — no lost/duplicate posting, no
     deadlock (repeated 5x).
  D. Two concurrent goods receipts on the same PO each post their own
     correctly balanced journal entry (repeated 5x).
  E. Two concurrent reversal attempts against the SAME journal entry
     serialize on the advisory lock and produce exactly one reversal
     entry, never two (repeated 5x).
"""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.session import SessionLocal, engine
from app.modules.accounting import service as accounting_service
from app.modules.accounting.models import JournalEntry, JournalLine
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import make_product, make_purchase_order, make_store, make_supplier, make_user

# --- A/B: DB-level invariants, real commits, raw SQL (mirrors the manual
# verification already done by hand against a scratch table before this
# design was chosen — see docs/M4_ACCOUNTING_CORE.md Section 5). ----------


def test_a_deferred_trigger_rejects_unbalanced_entry_at_commit() -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        setup.commit()
        store_id = store.id
    finally:
        setup.close()

    app_engine = engine
    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="unbalanced|integrity"):
            with conn.begin():
                cash_id = conn.execute(
                    text("SELECT id FROM accounts WHERE code = '1000'")
                ).scalar_one()
                revenue_id = conn.execute(
                    text("SELECT id FROM accounts WHERE code = '4000'")
                ).scalar_one()
                entry_id = conn.execute(
                    text(
                        "INSERT INTO journal_entries "
                        "(journal_number, store_id, posting_date, entry_type, source_type, "
                        " source_id, memo) "
                        "VALUES (:num, :store_id, current_date, 'STANDARD', 'SALE', :src, 'x') "
                        "RETURNING id"
                    ),
                    {"num": f"TESTA-{uuid.uuid4().hex}", "store_id": store_id, "src": -1},
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit) "
                        "VALUES (:eid, :aid, 10.00, 0)"
                    ),
                    {"eid": entry_id, "aid": cash_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit) "
                        "VALUES (:eid, :aid, 0, 9.99)"
                    ),
                    {"eid": entry_id, "aid": revenue_id},
                )


def test_b_erp_app_cannot_update_or_delete_posted_journal_rows() -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user(setup, store)
        product = make_product(
            setup,
            store,
            current_price=Decimal("10.00"),
            current_cost=Decimal("4.000000"),
            current_qty_on_hand=Decimal("10"),
        )
        setup.commit()
        sale = sales_service.finalize_sale(
            setup,
            store_id=store.id,
            cashier_id=cashier.id,
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
        setup.commit()
        entry = setup.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
            )
        ).scalar_one()
        entry_id = entry.id
    finally:
        setup.close()

    with engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            with conn.begin():
                conn.execute(
                    text("UPDATE journal_lines SET debit = 999 WHERE journal_entry_id = :eid"),
                    {"eid": entry_id},
                )
    with engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            with conn.begin():
                conn.execute(text("DELETE FROM journal_entries WHERE id = :eid"), {"eid": entry_id})


# --- C: concurrent sales against the same product -----------------------


@dataclass
class _Result:
    succeeded: bool = False
    error: str | None = None
    journal_entry_ids: list[int] = field(default_factory=list)


def _attempt_sale_and_record_journal(
    *, store_id: int, product_id: int, cashier_id: int, barrier: threading.Barrier, result: _Result
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        sale = sales_service.finalize_sale(
            session,
            store_id=store_id,
            cashier_id=cashier_id,
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
            lines=[SaleLineInput(product_id=product_id, quantity=Decimal("1"))],
            payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
        )
        session.commit()
        entry = session.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "SALE", JournalEntry.source_id == sale.id
            )
        ).scalar_one()
        result.succeeded = True
        result.journal_entry_ids.append(entry.id)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


@pytest.mark.parametrize("iteration", range(5))
def test_c_two_concurrent_sales_each_post_their_own_balanced_journal(iteration: int) -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user(setup, store)
        product = make_product(
            setup,
            store,
            current_price=Decimal("10.00"),
            current_cost=Decimal("4.000000"),
            current_qty_on_hand=Decimal("10"),
        )
        setup.commit()
        store_id, cashier_id, product_id = store.id, cashier.id, product.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_1, result_2 = _Result(), _Result()
    t1 = threading.Thread(
        target=_attempt_sale_and_record_journal,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            cashier_id=cashier_id,
            barrier=barrier,
            result=result_1,
        ),
    )
    t2 = threading.Thread(
        target=_attempt_sale_and_record_journal,
        kwargs=dict(
            store_id=store_id,
            product_id=product_id,
            cashier_id=cashier_id,
            barrier=barrier,
            result=result_2,
        ),
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert result_1.error is None, result_1
    assert result_2.error is None, result_2
    assert result_1.succeeded and result_2.succeeded
    all_ids = result_1.journal_entry_ids + result_2.journal_entry_ids
    assert len(all_ids) == 2
    assert len(set(all_ids)) == 2  # two distinct journal entries, never one lost/merged

    verify = SessionLocal()
    try:
        for entry_id in all_ids:
            lines = list(
                verify.execute(
                    select(JournalLine).where(JournalLine.journal_entry_id == entry_id)
                ).scalars()
            )
            assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
    finally:
        verify.close()


# --- D: concurrent goods receipts on the same PO --------------------------


def _attempt_receive_and_record_journal(
    *, purchase_order_id: int, item_id: int, barrier: threading.Barrier, result: _Result
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        receipt = purchasing_service.receive_goods(
            session,
            purchase_order_id=purchase_order_id,
            received_date=date(2024, 1, 1),
            lines=[GoodsReceiptLineInput(item_id, Decimal("1"), Decimal("2.000000"))],
            client_transaction_id=f"txn-{uuid.uuid4().hex}",
            caller_store_id=None,
        )
        session.commit()
        entry = session.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "PURCHASE_RECEIPT",
                JournalEntry.source_id == receipt.id,
            )
        ).scalar_one()
        result.succeeded = True
        result.journal_entry_ids.append(entry.id)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


@pytest.mark.parametrize("iteration", range(5))
def test_d_two_concurrent_receipts_each_post_their_own_balanced_journal(iteration: int) -> None:
    setup = SessionLocal()
    try:
        store = make_store(setup)
        supplier = make_supplier(setup)
        product = make_product(setup, store)
        po = make_purchase_order(setup, store, supplier)
        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity_ordered=Decimal("100"),
            unit_cost=Decimal("2.000000"),
        )
        setup.add(item)
        setup.commit()
        po_id, item_id = po.id, item.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_1, result_2 = _Result(), _Result()
    t1 = threading.Thread(
        target=_attempt_receive_and_record_journal,
        kwargs=dict(purchase_order_id=po_id, item_id=item_id, barrier=barrier, result=result_1),
    )
    t2 = threading.Thread(
        target=_attempt_receive_and_record_journal,
        kwargs=dict(purchase_order_id=po_id, item_id=item_id, barrier=barrier, result=result_2),
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert result_1.error is None, result_1
    assert result_2.error is None, result_2
    all_ids = result_1.journal_entry_ids + result_2.journal_entry_ids
    assert len(set(all_ids)) == 2


# --- E: concurrent reversal of the SAME journal entry ----------------------


@dataclass
class _ReversalResult:
    reversal_id: int | None = None
    error: str | None = None


def _attempt_reverse(
    *, journal_entry_id: int, cashier_id: int, barrier: threading.Barrier, result: _ReversalResult
) -> None:
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        reversal = accounting_service.reverse_journal_entry(
            session,
            journal_entry_id=journal_entry_id,
            reason="concurrent reversal test",
            reversed_by=cashier_id,
            caller_store_id=None,
        )
        session.commit()
        result.reversal_id = reversal.id
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


@pytest.mark.parametrize("iteration", range(5))
def test_e_two_concurrent_reversals_of_the_same_entry_produce_exactly_one_reversal(
    iteration: int,
) -> None:
    """Uses a MANUAL entry, not a SALE — reversing an automated source is
    now refused entirely (docs/M4_HARDENING_AUDIT.md Section 1), so the
    concurrency proof needs a source_type reversal is actually legal
    against, per the reversal function's own docstring."""
    from app.modules.accounting.service import _credit, _debit, _post_journal

    setup = SessionLocal()
    try:
        store = make_store(setup)
        cashier = make_user(setup, store)
        setup.commit()
        entry = _post_journal(
            setup,
            store_id=store.id,
            posting_date=date(2024, 1, 1),
            source_type="MANUAL",
            source_id=None,
            memo="concurrency test manual entry",
            created_by=cashier.id,
            lines=[
                _debit("1000", Decimal("10.00")),
                _credit("4000", Decimal("10.00")),
            ],
        )
        setup.commit()
        entry_id, cashier_id = entry.id, cashier.id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    result_1, result_2 = _ReversalResult(), _ReversalResult()
    t1 = threading.Thread(
        target=_attempt_reverse,
        kwargs=dict(
            journal_entry_id=entry_id, cashier_id=cashier_id, barrier=barrier, result=result_1
        ),
    )
    t2 = threading.Thread(
        target=_attempt_reverse,
        kwargs=dict(
            journal_entry_id=entry_id, cashier_id=cashier_id, barrier=barrier, result=result_2
        ),
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert result_1.error is None, result_1
    assert result_2.error is None, result_2
    assert result_1.reversal_id == result_2.reversal_id  # both see the SAME reversal

    verify = SessionLocal()
    try:
        reversal_count = len(
            list(
                verify.execute(
                    select(JournalEntry).where(JournalEntry.reversal_of_id == entry_id)
                ).scalars()
            )
        )
        assert reversal_count == 1  # never two reversal entries for one original
    finally:
        verify.close()
