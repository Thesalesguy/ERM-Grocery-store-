"""Pydantic read/request schemas for the accounting API. No internal ORM
models are ever returned directly (M4 task Section 27)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    account_type: str
    normal_balance: str
    is_system: bool
    is_active: bool
    description: str | None = None


class JournalLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    account_code: str = ""
    account_name: str = ""
    debit: Decimal
    credit: Decimal
    product_id: int | None = None
    description: str | None = None


class JournalEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    journal_number: str
    store_id: int
    posting_date: date
    entry_type: str
    source_type: str
    source_id: int | None = None
    reversal_of_id: int | None = None
    memo: str | None = None
    created_by: int | None = None
    created_at: datetime
    is_reversed: bool = False
    lines: list[JournalLineRead] = Field(default_factory=list)


class JournalEntryReverseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class TrialBalanceRowRead(BaseModel):
    account_code: str
    account_name: str
    account_type: str
    normal_balance: str
    total_debit: Decimal
    total_credit: Decimal


class TrialBalanceRead(BaseModel):
    store_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    rows: list[TrialBalanceRowRead]
    total_debit: Decimal
    total_credit: Decimal


class ProfitAndLossRead(BaseModel):
    store_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    net_sales: Decimal
    cogs: Decimal
    gross_profit: Decimal
    other_income: Decimal
    operating_expenses: Decimal
    net_income: Decimal


class InventoryReconciliationRowRead(BaseModel):
    store_id: int
    gl_inventory_balance: Decimal
    operational_valuation: Decimal
    discrepancy: Decimal


class InventoryReconciliationRead(BaseModel):
    rows: list[InventoryReconciliationRowRead]
