"""Management analytics/reporting (M11). Every route resolves the
caller's authorized store list via
app.modules.reports.service.resolve_authorized_store_ids BEFORE calling
any service function — a store id supplied by the client can never
bypass authorization (docs/M11_DESIGN.md Section 3/12). Permissions
reuse the existing read permission for each domain (no new permission
constants): `reports.read` for sales/KPI, `accounting.read` for
financial reports, `inventory.read` for inventory analytics,
`purchasing.read` for purchasing/supplier analytics, `payroll.read` for
labor/payroll analytics — matching exactly what already gates reading
the underlying raw data in each domain.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.accounting import service as accounting_service
from app.modules.auth.permissions import (
    ACCOUNTING_READ,
    INVENTORY_READ,
    PAYROLL_READ,
    PURCHASING_READ,
    REPORTS_READ,
)
from app.modules.auth.service import CurrentUser, require_permission
from app.modules.reports import schemas as report_schemas
from app.modules.reports import service as reports_service

router = APIRouter(prefix="/reports", tags=["reports"])

_reports_permission = require_permission(REPORTS_READ)
_accounting_permission = require_permission(ACCOUNTING_READ)
_inventory_permission = require_permission(INVENTORY_READ)
_purchasing_permission = require_permission(PURCHASING_READ)
_payroll_permission = require_permission(PAYROLL_READ)


def _resolve(current_user: CurrentUser, store_id: list[int] | None) -> list[int] | None:
    return reports_service.resolve_authorized_store_ids(current_user, store_id)


# --- Sales & profitability (Phase 3) ----------------------------------------


@router.get("/sales/summary", response_model=report_schemas.SalesSummaryRead)
def get_sales_summary(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reports_permission),
) -> report_schemas.SalesSummaryRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.sales_summary(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return report_schemas.SalesSummaryRead.model_validate(result)


@router.get("/sales/breakdown", response_model=list[report_schemas.SalesByDimensionRowRead])
def get_sales_breakdown(
    dimension: str = Query(..., pattern="^(store|product)$"),
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reports_permission),
) -> list[report_schemas.SalesByDimensionRowRead]:
    resolved = _resolve(current_user, store_id)
    fn = (
        reports_service.sales_by_store if dimension == "store" else reports_service.sales_by_product
    )
    rows = fn(db, store_ids=resolved, date_from=date_from, date_to=date_to)
    return [report_schemas.SalesByDimensionRowRead.model_validate(r) for r in rows]


@router.get("/sales/by-payment-method", response_model=list[report_schemas.PaymentMethodRowRead])
def get_sales_by_payment_method(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reports_permission),
) -> list[report_schemas.PaymentMethodRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.sales_by_payment_method(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.PaymentMethodRowRead.model_validate(r) for r in rows]


@router.get("/sales/trend", response_model=list[report_schemas.SalesTrendPointRead])
def get_sales_trend(
    date_from: date,
    date_to: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reports_permission),
) -> list[report_schemas.SalesTrendPointRead]:
    resolved = _resolve(current_user, store_id)
    points = reports_service.sales_trend_by_day(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.SalesTrendPointRead.model_validate(p) for p in points]


# --- Financial reporting (Phase 4) ------------------------------------------


@router.get("/financial/trial-balance", response_model=list[report_schemas.TrialBalanceRowRead])
def get_financial_trial_balance(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> list[report_schemas.TrialBalanceRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = accounting_service.trial_balance(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.TrialBalanceRowRead.model_validate(r) for r in rows]


@router.get("/financial/profit-loss", response_model=report_schemas.ProfitAndLossRead)
def get_financial_profit_loss(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> report_schemas.ProfitAndLossRead:
    resolved = _resolve(current_user, store_id)
    result = accounting_service.profit_and_loss(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return report_schemas.ProfitAndLossRead.model_validate(result)


@router.get(
    "/financial/profit-loss-comparative", response_model=report_schemas.ProfitAndLossComparativeRead
)
def get_financial_profit_loss_comparative(
    date_from: date,
    date_to: date,
    prior_date_from: date,
    prior_date_to: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> report_schemas.ProfitAndLossComparativeRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.profit_and_loss_comparative(
        db,
        store_ids=resolved,
        date_from=date_from,
        date_to=date_to,
        prior_date_from=prior_date_from,
        prior_date_to=prior_date_to,
    )
    return report_schemas.ProfitAndLossComparativeRead(
        current=report_schemas.ProfitAndLossRead.model_validate(result.current),
        prior=report_schemas.ProfitAndLossRead.model_validate(result.prior),
    )


@router.get("/financial/account-summary", response_model=report_schemas.AccountTypeSummaryRead)
def get_financial_account_summary(
    account_type: str = Query(..., pattern="^(REVENUE|EXPENSE|ASSET|LIABILITY)$"),
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> report_schemas.AccountTypeSummaryRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.account_type_summary(
        db, account_type=account_type, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return report_schemas.AccountTypeSummaryRead(
        account_type=result.account_type,
        rows=[report_schemas.TrialBalanceRowRead.model_validate(r) for r in result.rows],
        total_debit=result.total_debit,
        total_credit=result.total_credit,
    )


@router.get("/financial/balance-sheet", response_model=report_schemas.BalanceSheetSummaryRead)
def get_financial_balance_sheet(
    store_id: list[int] | None = Query(None),
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> report_schemas.BalanceSheetSummaryRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.balance_sheet_summary(db, store_ids=resolved, as_of=as_of)
    return report_schemas.BalanceSheetSummaryRead(
        store_ids=result.store_ids,
        as_of=result.as_of,
        assets=[report_schemas.TrialBalanceRowRead.model_validate(r) for r in result.assets],
        liabilities=[
            report_schemas.TrialBalanceRowRead.model_validate(r) for r in result.liabilities
        ],
        total_assets=result.total_assets,
        total_liabilities=result.total_liabilities,
    )


@router.get(
    "/financial/cash-summary",
    response_model=list[report_schemas.PaymentMethodReconciliationRowRead],
)
def get_financial_cash_summary(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_accounting_permission),
) -> list[report_schemas.PaymentMethodReconciliationRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.cash_payment_method_summary(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.PaymentMethodReconciliationRowRead.model_validate(r) for r in rows]


# --- Inventory analytics (Phase 5) ------------------------------------------


@router.get("/inventory/value", response_model=list[report_schemas.InventoryValueRowRead])
def get_inventory_value(
    group_by: str = Query("store", pattern="^(store|category)$"),
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.InventoryValueRowRead]:
    resolved = _resolve(current_user, store_id)
    fn = (
        reports_service.inventory_value_by_store
        if group_by == "store"
        else reports_service.inventory_value_by_category
    )
    rows = fn(db, store_ids=resolved)
    return [report_schemas.InventoryValueRowRead.model_validate(r) for r in rows]


@router.get("/inventory/movements", response_model=list[report_schemas.MovementSummaryRowRead])
def get_inventory_movements(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.MovementSummaryRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.inventory_movement_summary(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.MovementSummaryRowRead.model_validate(r) for r in rows]


@router.get("/inventory/shrinkage", response_model=list[report_schemas.ShrinkageRowRead])
def get_inventory_shrinkage(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.ShrinkageRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.shrinkage_summary(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.ShrinkageRowRead.model_validate(r) for r in rows]


@router.get("/inventory/turnover", response_model=list[report_schemas.TurnoverRowRead])
def get_inventory_turnover(
    date_from: date,
    date_to: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.TurnoverRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.inventory_turnover(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.TurnoverRowRead.model_validate(r) for r in rows]


@router.get("/inventory/slow-moving", response_model=list[report_schemas.SlowMovingRowRead])
def get_inventory_slow_moving(
    store_id: list[int] | None = Query(None),
    window_days: int = 90,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.SlowMovingRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.slow_moving_products(db, store_ids=resolved, window_days=window_days)
    return [report_schemas.SlowMovingRowRead.model_validate(r) for r in rows]


@router.get("/inventory/stockouts", response_model=list[report_schemas.StockoutRowRead])
def get_inventory_stockouts(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.StockoutRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.stockouts(db, store_ids=resolved)
    return [report_schemas.StockoutRowRead.model_validate(r) for r in rows]


@router.get("/inventory/negative-stock", response_model=list[report_schemas.NegativeStockRowRead])
def get_inventory_negative_stock(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.NegativeStockRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.negative_stock_products(db, store_ids=resolved)
    return [report_schemas.NegativeStockRowRead.model_validate(r) for r in rows]


@router.get("/inventory/in-transit", response_model=list[report_schemas.InTransitRowRead])
def get_inventory_in_transit(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.InTransitRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.inventory_in_transit(db, store_ids=resolved)
    return [report_schemas.InTransitRowRead.model_validate(r) for r in rows]


@router.get(
    "/inventory/in-transit-reconciliation", response_model=report_schemas.GlReconciliationRowRead
)
def get_inventory_in_transit_reconciliation(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> report_schemas.GlReconciliationRowRead:
    # Deliberately no store filter: matches the existing M8 endpoint
    # (GET /transfers/reports/inventory-in-transit-reconciliation) this
    # wraps, which is company-wide by design -- the account represents
    # value in transit BETWEEN stores, not within one.
    result = reports_service.in_transit_reconciliation(db)
    return report_schemas.GlReconciliationRowRead.model_validate(result)


@router.get(
    "/inventory/stock-counts/{stock_count_id}/variance",
    response_model=list[report_schemas.StockCountVarianceRowRead],
)
def get_stock_count_variance(
    stock_count_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_inventory_permission),
) -> list[report_schemas.StockCountVarianceRowRead]:
    from app.core.exceptions import ForbiddenError
    from app.modules.inventory.models import StockCount

    count = db.get(StockCount, stock_count_id)
    if (
        count is not None
        and current_user.store_id is not None
        and current_user.store_id != count.store_id
    ):
        raise ForbiddenError(
            f"Your account is scoped to store {current_user.store_id} and cannot view "
            f"stock count {stock_count_id} in store {count.store_id}",
            error_code="STORE_ACCESS_DENIED",
        )
    rows = reports_service.stock_count_variance(db, stock_count_id=stock_count_id)
    return [report_schemas.StockCountVarianceRowRead.model_validate(r) for r in rows]


# --- Purchasing & supplier analytics (Phase 6) ------------------------------


@router.get("/purchasing/spend", response_model=list[report_schemas.PurchaseSpendRowRead])
def get_purchasing_spend(
    group_by: str = Query("supplier", pattern="^(supplier|store|product)$"),
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_purchasing_permission),
) -> list[report_schemas.PurchaseSpendRowRead]:
    resolved = _resolve(current_user, store_id)
    fn = {
        "supplier": reports_service.purchase_spend_by_supplier,
        "store": reports_service.purchase_spend_by_store,
        "product": reports_service.purchase_spend_by_product,
    }[group_by]
    rows = fn(db, store_ids=resolved, date_from=date_from, date_to=date_to)
    return [report_schemas.PurchaseSpendRowRead.model_validate(r) for r in rows]


@router.get("/purchasing/po-fulfillment", response_model=list[report_schemas.PoFulfillmentRowRead])
def get_purchasing_po_fulfillment(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_purchasing_permission),
) -> list[report_schemas.PoFulfillmentRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.po_fulfillment(db, store_ids=resolved)
    return [report_schemas.PoFulfillmentRowRead.model_validate(r) for r in rows]


@router.get(
    "/purchasing/delivery-performance",
    response_model=list[report_schemas.SupplierDeliveryPerformanceRowRead],
)
def get_purchasing_delivery_performance(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_purchasing_permission),
) -> list[report_schemas.SupplierDeliveryPerformanceRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.supplier_delivery_performance(db, store_ids=resolved)
    return [report_schemas.SupplierDeliveryPerformanceRowRead.model_validate(r) for r in rows]


@router.get(
    "/purchasing/price-variance", response_model=list[report_schemas.PurchasePriceVarianceRowRead]
)
def get_purchasing_price_variance(
    store_id: list[int] | None = Query(None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_purchasing_permission),
) -> list[report_schemas.PurchasePriceVarianceRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.purchase_price_variance_report(
        db, store_ids=resolved, date_from=date_from, date_to=date_to
    )
    return [report_schemas.PurchasePriceVarianceRowRead.model_validate(r) for r in rows]


# --- Labor & payroll analytics (Phase 7) ------------------------------------


@router.get("/payroll/headcount", response_model=list[report_schemas.HeadcountRowRead])
def get_payroll_headcount(
    store_id: list[int] | None = Query(None),
    as_of: date | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_payroll_permission),
) -> list[report_schemas.HeadcountRowRead]:
    resolved = _resolve(current_user, store_id)
    rows = reports_service.headcount_by_store(db, store_ids=resolved, as_of=as_of)
    return [report_schemas.HeadcountRowRead.model_validate(r) for r in rows]


@router.get("/payroll/cost-summary", response_model=report_schemas.PayrollCostSummaryRead)
def get_payroll_cost_summary(
    period_start: date,
    period_end: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_payroll_permission),
) -> report_schemas.PayrollCostSummaryRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.payroll_cost_summary(
        db, store_ids=resolved, period_start=period_start, period_end=period_end
    )
    return report_schemas.PayrollCostSummaryRead.model_validate(result)


@router.get("/payroll/labor-cost-percent", response_model=report_schemas.LaborCostPercentRowRead)
def get_payroll_labor_cost_percent(
    period_start: date,
    period_end: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_payroll_permission),
) -> report_schemas.LaborCostPercentRowRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.labor_cost_percent_of_sales(
        db, store_ids=resolved, period_start=period_start, period_end=period_end
    )
    return report_schemas.LaborCostPercentRowRead.model_validate(result)


@router.get("/payroll/reconciliation", response_model=report_schemas.GlReconciliationRowRead)
def get_payroll_reconciliation(
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_payroll_permission),
) -> report_schemas.GlReconciliationRowRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.payroll_gl_reconciliation(db, store_ids=resolved)
    return report_schemas.GlReconciliationRowRead.model_validate(result)


# --- KPI dashboard (Phase 8) -------------------------------------------------


@router.get("/dashboard", response_model=report_schemas.KpiDashboardRead)
def get_kpi_dashboard(
    period_start: date,
    period_end: date,
    store_id: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(_reports_permission),
) -> report_schemas.KpiDashboardRead:
    resolved = _resolve(current_user, store_id)
    result = reports_service.kpi_dashboard(
        db, store_ids=resolved, period_start=period_start, period_end=period_end
    )
    return report_schemas.KpiDashboardRead.model_validate(result)
