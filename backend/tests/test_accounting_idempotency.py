"""Accounting idempotency (M4 task Section 21 / Invariant 8): a duplicate
operational request must never duplicate its journal entry, and the
accounting layer's own (source_type, source_id) uniqueness is a real,
independent backstop — not just inherited for free from the operational
layer's client_transaction_id.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounting.models import JournalEntry
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user,
    unique_suffix,
)


def _journal_count_for(db: Session, source_type: str, source_id: int) -> int:
    return len(
        list(
            db.execute(
                select(JournalEntry).where(
                    JournalEntry.source_type == source_type, JournalEntry.source_id == source_id
                )
            ).scalars()
        )
    )


def test_retrying_a_sale_with_the_same_client_transaction_id_posts_only_one_journal(
    db: Session,
) -> None:
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    db.commit()
    txn_id = f"txn-{unique_suffix()}"

    sale_1 = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=txn_id,
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    sale_2 = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=txn_id,  # same key — a retry
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()

    assert sale_1.id == sale_2.id  # the operational fast path returned the same sale
    assert _journal_count_for(db, "SALE", sale_1.id) == 1

    db.refresh(product)
    # Stock/COGS must reflect exactly ONE sale of 1 unit, not two.
    assert product.current_qty_on_hand == Decimal("9.000")


def test_retrying_a_goods_receipt_posts_only_one_journal(db: Session) -> None:
    store = make_store(db)
    supplier = make_supplier(db)
    product = make_product(db, store)
    po = make_purchase_order(db, store, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("50"),
        unit_cost=Decimal("3.000000"),
    )
    db.add(item)
    db.commit()
    txn_id = f"txn-{unique_suffix()}"

    receipt_1 = purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("3.000000"))],
        client_transaction_id=txn_id,
        caller_store_id=None,
    )
    db.commit()
    receipt_2 = purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date(2024, 1, 1),
        lines=[GoodsReceiptLineInput(item.id, Decimal("10"), Decimal("3.000000"))],
        client_transaction_id=txn_id,
        caller_store_id=None,
    )
    db.commit()

    assert receipt_1.id == receipt_2.id
    assert _journal_count_for(db, "PURCHASE_RECEIPT", receipt_1.id) == 1

    db.refresh(product)
    assert product.current_qty_on_hand == Decimal("10.000")  # not 20 — only one receipt happened


def test_posting_twice_for_the_same_source_id_is_rejected_at_the_db_level(db: Session) -> None:
    """Simulates a hypothetical future bug: a code path calls the
    accounting posting function twice for the same already-existing
    source row. This must fail loudly (DB unique constraint), not
    silently create a duplicate revenue/inventory entry — the real M4
    task Section 21 guarantee, independent of any operational-layer
    idempotency that happens to prevent this from ever occurring in the
    current call sites."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db,
        store,
        current_price=Decimal("10.00"),
        current_cost=Decimal("4.000000"),
        current_qty_on_hand=Decimal("10"),
    )
    db.commit()
    sale = sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("10.00"))],
    )
    db.commit()
    assert _journal_count_for(db, "SALE", sale.id) == 1

    from app.modules.accounting.service import _LineSpec, _post_journal

    with pytest.raises(IntegrityError):
        _post_journal(
            db,
            store_id=store.id,
            posting_date=date(2024, 1, 1),
            source_type="SALE",
            source_id=sale.id,  # already has a STANDARD entry
            memo="duplicate posting attempt",
            created_by=cashier.id,
            lines=[
                _LineSpec("1000", debit=Decimal("1.00"), credit=Decimal("0")),
                _LineSpec("4000", debit=Decimal("0"), credit=Decimal("1.00")),
            ],
        )
    db.rollback()
    assert _journal_count_for(db, "SALE", sale.id) == 1  # still exactly one, not two
