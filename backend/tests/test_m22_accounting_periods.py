"""M22: accounting periods (posting lock) and the P&L operating-expenses
fix. See docs/M22_DISCOVERY.md for the full design and docs/
M22_TESTING_SESSIONS.md for the session-by-session record this file
implements.

Session A: period lifecycle. Session B: closed-period posting protection
(INV-1/INV-2). Session C: correction/reversal survives a closed period
(INV-5). Session D: accounting reconciliation (closing posts nothing,
trial balance still balances). Session E: P&L now includes every EXPENSE
account (INV-6/7/8, resolves F2/Business Question A). Session G:
multi-store isolation (INV-3). Session H: authorization (INV-10). Session
L: adversarial.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationAppError
from app.modules.accounting import service as accounting_service
from app.modules.accounting.constants import (
    ACCOUNT_CASH_ON_HAND,
    ACCOUNT_CASH_OVER_SHORT,
    ACCOUNT_COGS,
    ACCOUNT_PURCHASE_PRICE_VARIANCE,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_WAGE_SALARY_EXPENSE,
)
from app.modules.accounting.models import JournalEntry
from app.modules.auth.permissions import ADMIN, MANAGER
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_store,
    make_user,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def _post_manual(
    db: Session, *, store_id: int, posting_date: date, amount: Decimal = Decimal("5.00")
) -> JournalEntry:
    from app.modules.accounting.service import _credit, _debit, _post_journal

    return _post_journal(
        db,
        store_id=store_id,
        posting_date=posting_date,
        source_type="MANUAL",
        source_id=None,
        memo="m22 test entry",
        created_by=None,
        lines=[_debit(ACCOUNT_CASH_ON_HAND, amount), _credit(ACCOUNT_SALES_REVENUE, amount)],
    )


def _post_expense(
    db: Session, *, store_id: int, account_code: str, amount: Decimal, posting_date: date
) -> JournalEntry:
    from app.modules.accounting.service import _credit, _debit, _post_journal

    return _post_journal(
        db,
        store_id=store_id,
        posting_date=posting_date,
        source_type="MANUAL",
        source_id=None,
        memo="m22 expense test entry",
        created_by=None,
        lines=[_debit(account_code, amount), _credit(ACCOUNT_CASH_ON_HAND, amount)],
    )


# --- Session A: period lifecycle ---------------------------------------------


def test_close_period_creates_row_and_is_listed(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()

    period = accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    assert period.id is not None
    listed = accounting_service.list_accounting_periods(db, store_id=store.id)
    assert [p.id for p in listed] == [period.id]


def test_close_overlapping_period_for_same_store_rejected(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.close_accounting_period(
            db,
            store_id=store.id,
            period_start=date(2025, 1, 15),
            period_end=date(2025, 2, 15),
            reason="overlapping close",
            closed_by=admin.id,
            caller_store_id=None,
        )
    assert exc_info.value.error_code == "PERIOD_OVERLAP"
    db.rollback()


def test_close_period_invalid_range_rejected(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()

    with pytest.raises(ValidationAppError):
        accounting_service.close_accounting_period(
            db,
            store_id=store.id,
            period_start=date(2025, 2, 1),
            period_end=date(2025, 1, 1),
            reason="backwards range",
            closed_by=admin.id,
            caller_store_id=None,
        )


# --- Session B: closed-period posting protection (INV-1/INV-2) --------------


def test_posting_inside_a_closed_period_is_refused(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    before_count = (
        db.execute(select(JournalEntry).where(JournalEntry.store_id == store.id)).scalars().all()
    )
    assert before_count == []

    with pytest.raises(ConflictError) as exc_info:
        _post_manual(db, store_id=store.id, posting_date=date(2025, 1, 15))
    assert exc_info.value.error_code == "PERIOD_CLOSED"
    db.rollback()

    # Zero rows created -- no partial entry, no orphaned line.
    after_count = (
        db.execute(select(JournalEntry).where(JournalEntry.store_id == store.id)).scalars().all()
    )
    assert after_count == []


def test_posting_just_outside_a_closed_period_boundary_succeeds(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    # The day right after the closed range is still open.
    entry = _post_manual(db, store_id=store.id, posting_date=date(2025, 2, 1))
    db.commit()
    assert entry.id is not None

    # The day right before the closed range is still open.
    entry2 = _post_manual(db, store_id=store.id, posting_date=date(2024, 12, 31))
    db.commit()
    assert entry2.id is not None


# --- Session G: multi-store isolation (INV-3) --------------------------------


def test_closing_one_store_does_not_block_another_store(db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    admin = make_user(db)
    db.commit()
    accounting_service.close_accounting_period(
        db,
        store_id=store_a.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="Store A January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    # Same date range, different store -- unaffected.
    entry = _post_manual(db, store_id=store_b.id, posting_date=date(2025, 1, 15))
    db.commit()
    assert entry.id is not None


# --- Session C: correction/reversal survives a closed period (INV-5) --------


def test_reversal_succeeds_even_when_the_original_entrys_date_is_now_closed(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    original = _post_manual(db, store_id=store.id, posting_date=date(2025, 1, 15))
    db.commit()

    # Close January AFTER the entry already posted -- the entry itself is
    # untouched (no historical mutation), only NEW postings dated into
    # January are blocked from here on.
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    reversal = accounting_service.reverse_journal_entry(
        db,
        journal_entry_id=original.id,
        reason="correcting a January mistake",
        reversed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()
    assert reversal.posting_date == date.today()
    assert reversal.reversal_of_id == original.id


def test_reversal_is_refused_when_todays_own_period_is_closed(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    original = _post_manual(db, store_id=store.id, posting_date=date(2025, 1, 15))
    db.commit()

    today = date.today()
    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=today,
        period_end=today,
        reason="closing today itself",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    with pytest.raises(ConflictError) as exc_info:
        accounting_service.reverse_journal_entry(
            db, journal_entry_id=original.id, reason="x", reversed_by=admin.id, caller_store_id=None
        )
    assert exc_info.value.error_code == "PERIOD_CLOSED"
    db.rollback()


# --- Session D: accounting reconciliation ------------------------------------


def test_closing_a_period_posts_no_journal_entries(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()

    before = (
        db.execute(select(JournalEntry).where(JournalEntry.store_id == store.id)).scalars().all()
    )
    assert before == []

    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    after = (
        db.execute(select(JournalEntry).where(JournalEntry.store_id == store.id)).scalars().all()
    )
    assert after == []


def test_trial_balance_still_balances_after_closing_a_period(db: Session) -> None:
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    _post_manual(db, store_id=store.id, posting_date=date(2024, 6, 1), amount=Decimal("42.00"))
    db.commit()

    accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    rows = accounting_service.trial_balance(db, store_id=store.id)
    assert sum(r.total_debit for r in rows) == sum(r.total_credit for r in rows)


# --- Session L: adversarial ---------------------------------------------------


def test_closing_a_period_that_already_has_postings_in_it_succeeds(db: Session) -> None:
    """Closing is about the FUTURE (no more postings), not retroactively
    invalidating what already posted -- an already-populated period is a
    completely ordinary thing to close."""
    store = make_store(db)
    admin = make_user(db)
    db.commit()
    _post_manual(db, store_id=store.id, posting_date=date(2025, 1, 15))
    db.commit()

    period = accounting_service.close_accounting_period(
        db,
        store_id=store.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="January close, after the fact",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()
    assert period.id is not None


# --- Session H: authorization (INV-10) ---------------------------------------


def test_admin_can_close_a_period_via_the_api(client, db: Session) -> None:
    store = make_store(db)
    username = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/accounting/periods",
        headers=headers,
        json={
            "store_id": store.id,
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
            "reason": "January close",
        },
    )
    assert response.status_code == 201
    assert response.json()["store_id"] == store.id


def test_manager_cannot_close_a_period_via_the_api(client, db: Session) -> None:
    """Manager holds accounting.reverse but NOT accounting.admin (see
    app.modules.auth.permissions.ROLE_PERMISSIONS) -- period-close is a
    configuration-level action, not a routine correction."""
    store = make_store(db)
    username = f"manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/accounting/periods",
        headers=headers,
        json={
            "store_id": store.id,
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
            "reason": "January close",
        },
    )
    assert response.status_code == 403


def test_close_period_missing_reason_rejected_by_schema(client, db: Session) -> None:
    store = make_store(db)
    username = f"admin_{unique_suffix()}"
    make_user_with_role(db, None, ADMIN, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.post(
        "/api/v1/accounting/periods",
        headers=headers,
        json={
            "store_id": store.id,
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
            "reason": "",
        },
    )
    assert response.status_code == 422


def test_close_accounting_period_rejects_cross_store_service_caller(db: Session) -> None:
    """Service-layer bypass check (mirrors M21 Session C methodology): a
    caller scoped to a DIFFERENT store than the one being closed is
    refused even calling the service function directly, not just at the
    HTTP layer."""
    store = make_store(db)
    other_store = make_store(db)
    admin = make_user(db, other_store)
    db.commit()

    with pytest.raises(ForbiddenError):
        accounting_service.close_accounting_period(
            db,
            store_id=store.id,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            reason="cross-store attempt",
            closed_by=admin.id,
            caller_store_id=other_store.id,
        )


def test_store_scoped_manager_only_sees_own_stores_periods_via_api(client, db: Session) -> None:
    store_a = make_store(db)
    store_b = make_store(db)
    admin = make_user(db)
    db.commit()
    accounting_service.close_accounting_period(
        db,
        store_id=store_a.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="A close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    accounting_service.close_accounting_period(
        db,
        store_id=store_b.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        reason="B close",
        closed_by=admin.id,
        caller_store_id=None,
    )
    db.commit()

    username = f"manager_a_{unique_suffix()}"
    make_user_with_role(db, store_a, MANAGER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/accounting/periods", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert {p["store_id"] for p in body} == {store_a.id}


# --- Session E: P&L now includes every EXPENSE account (INV-6/7/8) ----------
# Resolves F2 / Business Question A (docs/M22_DISCOVERY.md Phase 0/4).


def test_profit_and_loss_includes_wage_salary_expense(db: Session) -> None:
    store = make_store(db)
    db.commit()
    _post_expense(
        db,
        store_id=store.id,
        account_code=ACCOUNT_WAGE_SALARY_EXPENSE,
        amount=Decimal("1000.00"),
        posting_date=date(2025, 6, 1),
    )
    db.commit()

    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.operating_expenses == Decimal("1000.00")
    assert pnl.net_income == pnl.gross_profit + pnl.other_income - pnl.operating_expenses


def test_profit_and_loss_sums_multiple_expense_accounts_at_once(db: Session) -> None:
    store = make_store(db)
    db.commit()
    _post_expense(
        db,
        store_id=store.id,
        account_code=ACCOUNT_WAGE_SALARY_EXPENSE,
        amount=Decimal("600.00"),
        posting_date=date(2025, 6, 1),
    )
    _post_expense(
        db,
        store_id=store.id,
        account_code=ACCOUNT_PURCHASE_PRICE_VARIANCE,
        amount=Decimal("50.00"),
        posting_date=date(2025, 6, 2),
    )
    _post_expense(
        db,
        store_id=store.id,
        account_code=ACCOUNT_CASH_OVER_SHORT,
        amount=Decimal("10.00"),
        posting_date=date(2025, 6, 3),
    )
    db.commit()

    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.operating_expenses == Decimal("660.00")


def test_profit_and_loss_never_double_counts_cogs_in_operating_expenses(db: Session) -> None:
    store = make_store(db)
    db.commit()
    _post_expense(
        db,
        store_id=store.id,
        account_code=ACCOUNT_COGS,
        amount=Decimal("300.00"),
        posting_date=date(2025, 6, 1),
    )
    db.commit()

    pnl = accounting_service.profit_and_loss(db, store_id=store.id)
    assert pnl.cogs == Decimal("300.00")
    # COGS is subtracted once, inside gross_profit -- never a second time
    # inside operating_expenses.
    assert pnl.operating_expenses == Decimal("0")
