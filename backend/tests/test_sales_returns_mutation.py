"""M5 Session H: mutation-style tests for two of the six required
protections that are cleanly interceptable at a function/tuple boundary
(and so are kept as permanent, automated regression tests rather than a
one-off manual edit+revert during the audit session):

- Idempotency protection (neutering `_match_or_reject_idempotent_return`)
- Automated-source accounting protection for SALE_RETURN specifically
  (neutering `AUTOMATED_SOURCE_TYPES`)

The other four required mutation targets (store isolation,
return-quantity protection, historical-cost usage, historical-tax usage)
were mutation-tested via a manual, reverted source-code edit during the
M5 audit session rather than as permanent tests here, because the
protection in each case is which variable an inline expression reads,
not a call to a separately-patchable function — see
docs/M5_HARDENING_AUDIT.md Section "Mutation testing" for the exact edit
made, the test(s) that failed under it, and confirmation of the revert.
Store isolation's mutation test (removing the ROUTE-layer
enforce_store_access call) IS a permanent automated test — see
tests/test_sales_returns_api.py::test_store_isolation_mutation_test_removing_enforce_store_access.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.modules.accounting import service as accounting_service
from app.modules.sales import service as sales_service
from app.modules.sales.models import Sale
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from tests.factories import make_product, make_store, make_user, unique_suffix


def _sell(db: Session, store, cashier, product, *, quantity: Decimal) -> Sale:
    price = product.current_price
    return sales_service.finalize_sale(
        db,
        store_id=store.id,
        cashier_id=cashier.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=quantity)],
        payments=[PaymentInput(payment_method="CASH", amount=price * quantity)],
    )


def test_mutation_removing_idempotency_check_breaks_retry_transparency(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: a retry with the same client_transaction_id and the same
    payload against an already-fully-returned sale transparently returns
    the original SaleReturn (idempotent). Neutering
    _match_or_reject_idempotent_return (as if the check didn't exist)
    makes the SAME retry instead raise SALE_NOT_RETURNABLE, since without
    the check the second call runs the full validation pipeline against
    the sale's now-REFUNDED status rather than short-circuiting to the
    original result. This proves the check is load-bearing for retry
    safety, not just an optimization."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    sale_item_id = sale.items[0].id
    txn_id = f"ret-txn-{unique_suffix()}"

    first = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=txn_id,
        caller_store_id=None,
    )
    db.commit()

    # Baseline (unmutated): retry is transparent.
    retry = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=txn_id,
        caller_store_id=None,
    )
    assert retry.id == first.id

    # Mutation: neuter the idempotency check entirely.
    monkeypatch.setattr(
        sales_service, "_match_or_reject_idempotent_return", lambda *args, **kwargs: None
    )

    with pytest.raises(ConflictError) as exc_info:
        sales_service.create_sale_return(
            db,
            sale_id=sale.id,
            store_id=store.id,
            return_date=date.today(),
            lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=Decimal("1"))],
            refund_method="CASH",
            client_transaction_id=txn_id,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "SALE_NOT_RETURNABLE"
    db.rollback()

    # Restore and confirm the retry is transparent again (proves the
    # mutation, not leftover test state, caused the failure above).
    monkeypatch.undo()
    restored_retry = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=txn_id,
        caller_store_id=None,
    )
    assert restored_retry.id == first.id


def test_mutation_removing_sale_return_from_automated_sources_allows_bare_journal_reversal(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: reverse_journal_entry refuses to reverse a SALE_RETURN-
    sourced journal entry directly (OPERATIONAL_REVERSAL_REQUIRED) — the
    same automated-source block M4's hardening audit added, now proven to
    actually cover this new M5 source type rather than merely being
    schema-ready for it. Removing "SALE_RETURN" from
    AUTOMATED_SOURCE_TYPES (as seen by accounting.service) makes the
    exact same call succeed instead, proving the block is what's
    stopping it — not some unrelated guard."""
    store = make_store(db)
    cashier = make_user(db, store)
    product = make_product(
        db, store, current_price=Decimal("10.00"), current_qty_on_hand=Decimal("5")
    )
    db.commit()
    sale = _sell(db, store, cashier, product, quantity=Decimal("1"))
    db.commit()
    sale_item_id = sale.items[0].id

    sale_return = sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store.id,
        return_date=date.today(),
        lines=[SaleReturnLineInput(sale_item_id=sale_item_id, quantity=Decimal("1"))],
        refund_method="CASH",
        client_transaction_id=f"ret-txn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    from sqlalchemy import select

    from app.modules.accounting.models import JournalEntry

    return_journal = db.execute(
        select(JournalEntry).where(
            JournalEntry.source_type == "SALE_RETURN", JournalEntry.source_id == sale_return.id
        )
    ).scalar_one()

    # Baseline (unmutated): blocked.
    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=return_journal.id,
            reason="attempted direct reversal",
            reversed_by=cashier.id,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"
    db.rollback()

    # Mutation: SALE_RETURN no longer treated as an automated source.
    monkeypatch.setattr(
        accounting_service,
        "AUTOMATED_SOURCE_TYPES",
        ("SALE", "PURCHASE_RECEIPT", "PURCHASE_RETURN", "STOCK_ADJUSTMENT"),
    )
    reversal = accounting_service.reverse_journal_entry(
        db,
        journal_entry_id=return_journal.id,
        reason="attempted direct reversal under mutation",
        reversed_by=cashier.id,
        caller_store_id=None,
    )
    assert reversal.entry_type == "REVERSAL"  # the block's absence let this through
    db.rollback()

    # Restore and confirm the block is back.
    monkeypatch.undo()
    with pytest.raises(ConflictError) as exc_info_restored:
        accounting_service.reverse_journal_entry(
            db,
            journal_entry_id=return_journal.id,
            reason="attempted direct reversal after restore",
            reversed_by=cashier.id,
            caller_store_id=None,
        )
    assert exc_info_restored.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"
