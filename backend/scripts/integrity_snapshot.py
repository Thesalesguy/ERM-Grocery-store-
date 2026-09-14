"""M12 Phase 15: a reusable, deterministic data-integrity snapshot across
every critical domain (accounting, inventory, AP, payroll, sales).

Used by Phase 3 (backup/restore), Phase 4 (accounting recovery
integrity), Phase 5 (inventory recovery integrity), and directly by
tests/test_data_integrity_snapshot.py -- the same function, called
against the same database before and after a backup/restore or
migration, must return an identical dict, key for key.

Deliberately reuses the EXISTING, already-correct reporting/accounting
functions (accounting.service.trial_balance/profit_and_loss,
reports.service.sales_summary/payroll_cost_summary/
payroll_gl_reconciliation, ap.service.ap_aging/ap_reconciliation/
purchase_clearing_reconciliation) as its data source wherever one
exists, rather than re-deriving GL/inventory/payroll arithmetic a
second way -- exactly the "no second accounting truth" discipline
M11 established. Only genuinely new raw counts (journal imbalance
count, audit log count) are computed directly here, because no
existing function already produces them.

Can be run standalone: `python -m scripts.integrity_snapshot <database_url>`
prints the snapshot as JSON. Import `take_snapshot(engine)` /
`take_snapshot_from_url(url)` from tests or other scripts.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session

# A snapshot must cover "all time" regardless of when this script is run
# relative to the data it's inspecting -- matches the exact pattern
# M11's adversarial audit already proved safe for a multi-century range.
_EPOCH_START = date(1901, 1, 1)
_EPOCH_END = date(2099, 12, 31)


@dataclass(frozen=True)
class IntegritySnapshot:
    # Accounting
    posted_journal_count: int
    journal_debit_total: str
    journal_credit_total: str
    journal_imbalance_count: int
    trial_balance_total_debit: str
    trial_balance_total_credit: str
    net_income: str

    # Inventory
    inventory_movement_count: int
    inventory_total_quantity: str
    inventory_total_valuation: str
    inventory_gl_balance: str
    inventory_reconciliation_discrepancy: str

    # AP
    purchase_invoice_grand_total: str
    supplier_payment_total: str
    supplier_credit_note_total: str
    ap_outstanding_total: str
    ap_reconciliation_discrepancy: str
    purchase_clearing_gl_balance: str
    purchase_clearing_discrepancy: str

    # Payroll
    posted_payroll_period_count: int
    payroll_gross_total: str
    payroll_deductions_total: str
    payroll_net_total: str
    payroll_gl_reconciliation_discrepancy: str

    # Sales
    completed_sale_count: int
    sale_return_count: int
    void_count: int
    gross_sales_revenue: str
    sales_discounts_total: str
    sales_cogs_total: str
    net_sales_revenue: str

    # Cross-cutting
    audit_log_count: int
    store_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def take_snapshot(db: Session) -> IntegritySnapshot:
    from app.modules.accounting import service as accounting_service
    from app.modules.accounting.constants import ACCOUNT_INVENTORY, ACCOUNT_PURCHASE_CLEARING
    from app.modules.accounting.models import JournalEntry, JournalLine
    from app.modules.ap import service as ap_service
    from app.modules.ap.models import PurchaseInvoice, SupplierCreditNote, SupplierPayment
    from app.modules.audit.models import AuditLog
    from app.modules.auth.models import Store
    from app.modules.reports import service as reports_service
    from app.modules.sales.models import SaleReturn

    # --- Accounting: reuse trial_balance/profit_and_loss verbatim -----
    tb_rows = accounting_service.trial_balance(db, store_ids=None)
    trial_balance_total_debit = sum((r.total_debit for r in tb_rows), start=Decimal("0"))
    trial_balance_total_credit = sum((r.total_credit for r in tb_rows), start=Decimal("0"))
    pnl = accounting_service.profit_and_loss(
        db, store_ids=None, date_from=_EPOCH_START, date_to=_EPOCH_END
    )

    posted_journal_count = db.execute(select(func.count(JournalEntry.id))).scalar_one()
    journal_debit_total = Decimal(
        db.execute(select(func.coalesce(func.sum(JournalLine.debit), 0))).scalar_one()
    )
    journal_credit_total = Decimal(
        db.execute(select(func.coalesce(func.sum(JournalLine.credit), 0))).scalar_one()
    )
    imbalance_query = (
        select(JournalLine.journal_entry_id)
        .group_by(JournalLine.journal_entry_id)
        .having(func.sum(JournalLine.debit) != func.sum(JournalLine.credit))
    )
    journal_imbalance_count = len(list(db.execute(imbalance_query)))

    # --- Inventory: reuse inventory_value_by_store + the existing M4
    # inventory_reconciliation (never recomputed independently) --------
    inventory_rows = reports_service.inventory_value_by_store(db, store_ids=None)
    inventory_total_quantity = sum((r.quantity_on_hand for r in inventory_rows), start=Decimal("0"))
    inventory_total_valuation = sum((r.value for r in inventory_rows), start=Decimal("0"))
    inventory_gl_row = next((r for r in tb_rows if r.account_code == ACCOUNT_INVENTORY), None)
    inventory_gl_balance = (
        (inventory_gl_row.total_debit - inventory_gl_row.total_credit)
        if inventory_gl_row
        else Decimal("0")
    )
    inv_reconciliation_rows = accounting_service.inventory_reconciliation(db, store_id=None)
    inventory_reconciliation_discrepancy = sum(
        (r.discrepancy for r in inv_reconciliation_rows), start=Decimal("0")
    )

    # --- AP: reuse ap_aging/ap_reconciliation/purchase_clearing_reconciliation ---
    invoice_total = Decimal(
        db.execute(select(func.coalesce(func.sum(PurchaseInvoice.grand_total), 0))).scalar_one()
    )
    payment_total = Decimal(
        db.execute(select(func.coalesce(func.sum(SupplierPayment.amount), 0))).scalar_one()
    )
    credit_total = Decimal(
        db.execute(select(func.coalesce(func.sum(SupplierCreditNote.grand_total), 0))).scalar_one()
    )
    aging_rows = ap_service.ap_aging(db, store_ids=None)
    ap_outstanding_total = sum((r.total for r in aging_rows), start=Decimal("0"))
    ap_reconciliation = ap_service.ap_reconciliation(db, store_id=None)
    purchase_clearing = ap_service.purchase_clearing_reconciliation(db, store_id=None)
    purchase_clearing_gl_row = next(
        (r for r in tb_rows if r.account_code == ACCOUNT_PURCHASE_CLEARING), None
    )
    purchase_clearing_gl_balance = (
        (purchase_clearing_gl_row.total_credit - purchase_clearing_gl_row.total_debit)
        if purchase_clearing_gl_row
        else Decimal("0")
    )

    # --- Payroll: reuse payroll_cost_summary/payroll_gl_reconciliation ---
    payroll_summary = reports_service.payroll_cost_summary(
        db, store_ids=None, period_start=_EPOCH_START, period_end=_EPOCH_END
    )
    payroll_reconciliation = reports_service.payroll_gl_reconciliation(db, store_ids=None)

    # --- Sales: reuse sales_summary verbatim --------------------------
    sales_summary = reports_service.sales_summary(
        db, store_ids=None, date_from=_EPOCH_START, date_to=_EPOCH_END
    )
    sale_return_count = db.execute(select(func.count(SaleReturn.id))).scalar_one()

    # --- Cross-cutting -------------------------------------------------
    audit_log_count = db.execute(select(func.count(AuditLog.id))).scalar_one()
    store_count = db.execute(select(func.count(Store.id))).scalar_one()

    return IntegritySnapshot(
        posted_journal_count=int(posted_journal_count),
        journal_debit_total=str(journal_debit_total),
        journal_credit_total=str(journal_credit_total),
        journal_imbalance_count=journal_imbalance_count,
        trial_balance_total_debit=str(trial_balance_total_debit),
        trial_balance_total_credit=str(trial_balance_total_credit),
        net_income=str(pnl.net_income),
        inventory_movement_count=_count(db, "inventory_movements"),
        inventory_total_quantity=str(inventory_total_quantity),
        inventory_total_valuation=str(inventory_total_valuation),
        inventory_gl_balance=str(inventory_gl_balance),
        inventory_reconciliation_discrepancy=str(inventory_reconciliation_discrepancy),
        purchase_invoice_grand_total=str(invoice_total),
        supplier_payment_total=str(payment_total),
        supplier_credit_note_total=str(credit_total),
        ap_outstanding_total=str(ap_outstanding_total),
        ap_reconciliation_discrepancy=str(ap_reconciliation.discrepancy),
        purchase_clearing_gl_balance=str(purchase_clearing_gl_balance),
        purchase_clearing_discrepancy=str(purchase_clearing.discrepancy),
        posted_payroll_period_count=_count(db, "payroll_periods", where="status = 'POSTED'"),
        payroll_gross_total=str(payroll_summary.gross_pay),
        payroll_deductions_total=str(payroll_summary.deductions),
        payroll_net_total=str(payroll_summary.net_pay),
        payroll_gl_reconciliation_discrepancy=str(payroll_reconciliation.discrepancy),
        completed_sale_count=sales_summary.transaction_count,
        sale_return_count=int(sale_return_count),
        void_count=sales_summary.void_count,
        gross_sales_revenue=str(sales_summary.gross_sales),
        sales_discounts_total=str(sales_summary.discounts),
        sales_cogs_total=str(sales_summary.cogs),
        net_sales_revenue=str(sales_summary.net_sales),
        audit_log_count=int(audit_log_count),
        store_count=int(store_count),
    )


def _count(db: Session, table: str, *, where: str | None = None) -> int:
    sql = f"SELECT count(*) FROM {table}"  # noqa: S608 -- table name is a fixed literal, never user input
    if where:
        sql += f" WHERE {where}"
    return int(db.execute(text(sql)).scalar_one())


def take_snapshot_from_engine(engine: Engine) -> IntegritySnapshot:
    with Session(bind=engine) as db:
        return take_snapshot(db)


def take_snapshot_from_url(database_url: str) -> IntegritySnapshot:
    engine = create_engine(database_url)
    try:
        return take_snapshot_from_engine(engine)
    finally:
        engine.dispose()


def diff_snapshots(
    before: IntegritySnapshot, after: IntegritySnapshot
) -> dict[str, tuple[Any, Any]]:
    """Every field that differs between two snapshots, {field: (before, after)}.
    Empty dict means the snapshots are identical -- the expected outcome
    across a backup/restore or migration cycle."""
    before_dict, after_dict = before.to_dict(), after.to_dict()
    return {
        key: (before_dict[key], after_dict[key])
        for key in before_dict
        if before_dict[key] != after_dict[key]
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m scripts.integrity_snapshot <database_url>", file=sys.stderr)
        raise SystemExit(2)
    snapshot = take_snapshot_from_url(sys.argv[1])
    print(json.dumps(snapshot.to_dict(), indent=2))
