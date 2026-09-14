"""Accounting endpoints: chart of accounts, journal entries, reversal, and
financial reports.

`accounting.read` gates every GET. `accounting.reverse` gates the one
mutating endpoint — reversing a posted journal entry. There is
deliberately no manual journal-creation endpoint in M4 (see
app.modules.auth.permissions.ACCOUNTING_POST's docstring): every M4
journal entry is posted automatically, inline, by the operational
transaction it represents (sale finalization, goods receiving, purchase
return, stock adjustment) — exposing arbitrary journal creation to users
would let anyone fabricate financial history, which M4 task Section 27
explicitly says not to do without a genuine need.

Journal entries and reports are store-scoped the same way sales/
purchasing already are (M2 hardening audit Section 12): a store-scoped
user's requests are filtered to their own store via scoped_store_filter,
and a direct-by-ID read of another store's journal entry 404s rather than
leaking its existence.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import NotFoundError
from app.modules.accounting import service
from app.modules.accounting.models import Account, JournalEntry
from app.modules.accounting.schemas import (
    AccountRead,
    InventoryReconciliationRead,
    InventoryReconciliationRowRead,
    JournalEntryRead,
    JournalEntryReverseRequest,
    JournalLineRead,
    ProfitAndLossRead,
    TrialBalanceRead,
    TrialBalanceRowRead,
)
from app.modules.auth.permissions import ACCOUNTING_READ, ACCOUNTING_REVERSE
from app.modules.auth.service import CurrentUser, require_permission, scoped_store_filter

router = APIRouter(prefix="/accounting", tags=["accounting"])

_read_permission = require_permission(ACCOUNTING_READ)
_reverse_permission = require_permission(ACCOUNTING_REVERSE)


def _to_journal_entry_read(db: Session, entry: JournalEntry) -> JournalEntryRead:
    lines = service.list_journal_lines(db, entry.id)
    account_ids = {line.account_id for line in lines}
    accounts = {
        a.id: a for a in db.execute(select(Account).where(Account.id.in_(account_ids))).scalars()
    }
    is_reversed = (
        entry.entry_type == "STANDARD"
        and db.execute(
            select(JournalEntry.id).where(JournalEntry.reversal_of_id == entry.id)
        ).scalar_one_or_none()
        is not None
    )
    read = JournalEntryRead.model_validate(entry)
    read.is_reversed = is_reversed
    read.lines = [
        JournalLineRead(
            id=line.id,
            account_id=line.account_id,
            account_code=accounts[line.account_id].code,
            account_name=accounts[line.account_id].name,
            debit=line.debit,
            credit=line.credit,
            product_id=line.product_id,
            description=line.description,
        )
        for line in lines
    ]
    return read


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[AccountRead]:
    return [
        AccountRead.model_validate(a) for a in service.list_accounts(db, active_only=active_only)
    ]


@router.get("/journals", response_model=list[JournalEntryRead])
def list_journals(
    store_id: int | None = None,
    source_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> list[JournalEntryRead]:
    effective_store_id = scoped_store_filter(current_user, store_id)
    entries = service.list_journal_entries(
        db,
        store_id=effective_store_id,
        source_type=source_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [_to_journal_entry_read(db, e) for e in entries]


@router.get("/journals/{journal_entry_id}", response_model=JournalEntryRead)
def get_journal(
    journal_entry_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> JournalEntryRead:
    entry = service.get_journal_entry(db, journal_entry_id)
    if current_user.store_id is not None and current_user.store_id != entry.store_id:
        raise NotFoundError(f"Journal entry {journal_entry_id} not found")
    return _to_journal_entry_read(db, entry)


@router.post("/journals/{journal_entry_id}/reverse", response_model=JournalEntryRead)
def reverse_journal(
    journal_entry_id: int,
    payload: JournalEntryReverseRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reverse_permission),
) -> JournalEntryRead:
    reversal = service.reverse_journal_entry(
        db,
        journal_entry_id=journal_entry_id,
        reason=payload.reason,
        reversed_by=current_user.id,
        caller_store_id=current_user.store_id,
    )
    db.commit()
    db.refresh(reversal)
    return _to_journal_entry_read(db, service.get_journal_entry(db, reversal.id))


@router.get("/reports/trial-balance", response_model=TrialBalanceRead)
def get_trial_balance(
    store_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> TrialBalanceRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    rows = service.trial_balance(
        db, store_id=effective_store_id, date_from=date_from, date_to=date_to
    )
    return TrialBalanceRead(
        store_id=effective_store_id,
        date_from=date_from,
        date_to=date_to,
        rows=[TrialBalanceRowRead(**row.__dict__) for row in rows],
        total_debit=sum((row.total_debit for row in rows), start=Decimal("0")),
        total_credit=sum((row.total_credit for row in rows), start=Decimal("0")),
    )


@router.get("/reports/profit-loss", response_model=ProfitAndLossRead)
def get_profit_and_loss(
    store_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> ProfitAndLossRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    pnl = service.profit_and_loss(
        db, store_id=effective_store_id, date_from=date_from, date_to=date_to
    )
    return ProfitAndLossRead(**pnl.__dict__)


@router.get("/reports/inventory-reconciliation", response_model=InventoryReconciliationRead)
def get_inventory_reconciliation(
    store_id: int | None = None,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_read_permission),
) -> InventoryReconciliationRead:
    effective_store_id = scoped_store_filter(current_user, store_id)
    rows = service.inventory_reconciliation(db, store_id=effective_store_id, as_of=as_of)
    return InventoryReconciliationRead(
        rows=[InventoryReconciliationRowRead(**row.__dict__) for row in rows]
    )
