/**
 * M16: read-only client for the M11 reporting API
 * (app/api/v1/endpoints/reports.py). Every interface mirrors a Pydantic
 * schema in app/modules/reports/schemas.py exactly — no field is added,
 * renamed, or recomputed here. All monetary/quantity fields stay
 * strings (server-formatted Decimals) and must never be added/multiplied
 * as JS numbers except for display-only aggregation the server itself
 * already did.
 */
import { apiFetch, buildQuery } from './client'

export interface ReportDateRange {
  store_id?: number[]
  date_from?: string
  date_to?: string
}

// --- Sales & profitability ---------------------------------------------

export interface SalesSummary {
  store_ids: number[] | null
  date_from: string | null
  date_to: string | null
  gross_sales: string
  discounts: string
  returns: string
  void_count: number
  void_amount: string
  net_sales: string
  tax: string
  cogs: string
  gross_profit: string
  gross_margin_percent: string | null
  transaction_count: number
  units_sold: string
  average_transaction_value: string | null
}

export function getSalesSummary(params: ReportDateRange = {}): Promise<SalesSummary> {
  return apiFetch<SalesSummary>(`/api/v1/reports/sales/summary${buildQuery(params)}`)
}

export interface SalesByDimensionRow {
  key: number | string
  label: string
  gross_sales: string
  returns: string
  net_sales: string
  cogs: string
  units_sold: string
  transaction_count: number
}

export function getSalesBreakdown(
  dimension: 'store' | 'product',
  params: ReportDateRange = {},
): Promise<SalesByDimensionRow[]> {
  return apiFetch<SalesByDimensionRow[]>(
    `/api/v1/reports/sales/breakdown${buildQuery({ dimension, ...params })}`,
  )
}

export interface PaymentMethodRow {
  payment_method: string
  amount: string
  payment_count: number
}

export function getSalesByPaymentMethod(params: ReportDateRange = {}): Promise<PaymentMethodRow[]> {
  return apiFetch<PaymentMethodRow[]>(
    `/api/v1/reports/sales/by-payment-method${buildQuery(params)}`,
  )
}

export interface SalesTrendPoint {
  sale_date: string
  gross_sales: string
  returns: string
  net_sales: string
  transaction_count: number
}

export function getSalesTrend(
  date_from: string,
  date_to: string,
  store_id?: number[],
): Promise<SalesTrendPoint[]> {
  return apiFetch<SalesTrendPoint[]>(
    `/api/v1/reports/sales/trend${buildQuery({ date_from, date_to, store_id })}`,
  )
}

// --- Financial (GL-derived) ---------------------------------------------

export interface TrialBalanceRow {
  account_code: string
  account_name: string
  account_type: string
  normal_balance: string
  total_debit: string
  total_credit: string
}

export function getTrialBalance(params: ReportDateRange = {}): Promise<TrialBalanceRow[]> {
  return apiFetch<TrialBalanceRow[]>(`/api/v1/reports/financial/trial-balance${buildQuery(params)}`)
}

export interface ProfitAndLoss {
  store_id: number | null
  date_from: string | null
  date_to: string | null
  net_sales: string
  cogs: string
  gross_profit: string
  other_income: string
  operating_expenses: string
  net_income: string
}

export function getProfitAndLoss(params: ReportDateRange = {}): Promise<ProfitAndLoss> {
  return apiFetch<ProfitAndLoss>(`/api/v1/reports/financial/profit-loss${buildQuery(params)}`)
}

export interface ProfitAndLossComparative {
  current: ProfitAndLoss
  prior: ProfitAndLoss
}

export function getProfitAndLossComparative(params: {
  date_from: string
  date_to: string
  prior_date_from: string
  prior_date_to: string
  store_id?: number[]
}): Promise<ProfitAndLossComparative> {
  return apiFetch<ProfitAndLossComparative>(
    `/api/v1/reports/financial/profit-loss-comparative${buildQuery(params)}`,
  )
}

export interface AccountTypeSummary {
  account_type: string
  rows: TrialBalanceRow[]
  total_debit: string
  total_credit: string
}

export function getAccountSummary(
  account_type: 'REVENUE' | 'EXPENSE' | 'ASSET' | 'LIABILITY',
  params: ReportDateRange = {},
): Promise<AccountTypeSummary> {
  return apiFetch<AccountTypeSummary>(
    `/api/v1/reports/financial/account-summary${buildQuery({ account_type, ...params })}`,
  )
}

export interface BalanceSheetSummary {
  store_ids: number[] | null
  as_of: string | null
  assets: TrialBalanceRow[]
  liabilities: TrialBalanceRow[]
  total_assets: string
  total_liabilities: string
}

export function getBalanceSheet(params: {
  store_id?: number[]
  as_of?: string
}): Promise<BalanceSheetSummary> {
  return apiFetch<BalanceSheetSummary>(
    `/api/v1/reports/financial/balance-sheet${buildQuery(params)}`,
  )
}

export interface PaymentMethodReconciliationRow {
  payment_method: string
  operational_amount: string
  gl_account_code: string
  gl_balance: string
  discrepancy: string
}

export function getCashSummary(
  params: ReportDateRange = {},
): Promise<PaymentMethodReconciliationRow[]> {
  return apiFetch<PaymentMethodReconciliationRow[]>(
    `/api/v1/reports/financial/cash-summary${buildQuery(params)}`,
  )
}

// --- Inventory analytics --------------------------------------------------

export interface InventoryValueRow {
  key: number
  label: string
  quantity_on_hand: string
  value: string
}

export function getInventoryValue(
  group_by: 'store' | 'category',
  store_id?: number[],
): Promise<InventoryValueRow[]> {
  return apiFetch<InventoryValueRow[]>(
    `/api/v1/reports/inventory/value${buildQuery({ group_by, store_id })}`,
  )
}

export interface MovementSummaryRow {
  movement_type: string
  quantity: string
  movement_count: number
}

export function getInventoryMovements(params: ReportDateRange = {}): Promise<MovementSummaryRow[]> {
  return apiFetch<MovementSummaryRow[]>(`/api/v1/reports/inventory/movements${buildQuery(params)}`)
}

export interface ShrinkageRow {
  store_id: number
  reason_code: string
  quantity: string
  adjustment_count: number
}

export function getShrinkage(params: ReportDateRange = {}): Promise<ShrinkageRow[]> {
  return apiFetch<ShrinkageRow[]>(`/api/v1/reports/inventory/shrinkage${buildQuery(params)}`)
}

export interface TurnoverRow {
  product_id: number
  product_name: string
  cogs: string
  average_inventory_value: string | null
  turnover: string | null
  days_on_hand: string | null
}

export function getInventoryTurnover(
  date_from: string,
  date_to: string,
  store_id?: number[],
): Promise<TurnoverRow[]> {
  return apiFetch<TurnoverRow[]>(
    `/api/v1/reports/inventory/turnover${buildQuery({ date_from, date_to, store_id })}`,
  )
}

export interface SlowMovingRow {
  product_id: number
  product_name: string
  store_id: number
  quantity_on_hand: string
  units_sold_in_window: string
}

export function getSlowMoving(window_days = 90, store_id?: number[]): Promise<SlowMovingRow[]> {
  return apiFetch<SlowMovingRow[]>(
    `/api/v1/reports/inventory/slow-moving${buildQuery({ window_days, store_id })}`,
  )
}

export interface StockoutRow {
  product_id: number
  product_name: string
  store_id: number
}

export function getStockouts(store_id?: number[]): Promise<StockoutRow[]> {
  return apiFetch<StockoutRow[]>(`/api/v1/reports/inventory/stockouts${buildQuery({ store_id })}`)
}

export interface NegativeStockRow {
  product_id: number
  product_name: string
  store_id: number
  quantity_on_hand: string
}

export function getNegativeStock(store_id?: number[]): Promise<NegativeStockRow[]> {
  return apiFetch<NegativeStockRow[]>(
    `/api/v1/reports/inventory/negative-stock${buildQuery({ store_id })}`,
  )
}

export interface InTransitRow {
  transfer_id: number
  from_store_id: number
  to_store_id: number
  source_product_id: number
  destination_product_id: number
  quantity_in_transit: string
  value_in_transit: string
}

export function getInTransit(store_id?: number[]): Promise<InTransitRow[]> {
  return apiFetch<InTransitRow[]>(`/api/v1/reports/inventory/in-transit${buildQuery({ store_id })}`)
}

export interface GlReconciliationRow {
  label: string
  gl_balance: string
  operational_value: string
  discrepancy: string
}

export function getInTransitReconciliation(): Promise<GlReconciliationRow> {
  return apiFetch<GlReconciliationRow>('/api/v1/reports/inventory/in-transit-reconciliation')
}

export interface StockCountVarianceRow {
  stock_count_id: number
  product_id: number
  expected_quantity: string
  counted_quantity: string
  variance: string
}

export function getStockCountVariance(stockCountId: number): Promise<StockCountVarianceRow[]> {
  return apiFetch<StockCountVarianceRow[]>(
    `/api/v1/reports/inventory/stock-counts/${stockCountId}/variance`,
  )
}

// --- Purchasing & supplier analytics --------------------------------------

export interface PurchaseSpendRow {
  key: number
  label: string
  quantity_received: string
  spend: string
}

export function getPurchasingSpend(
  group_by: 'supplier' | 'store' | 'product',
  params: ReportDateRange = {},
): Promise<PurchaseSpendRow[]> {
  return apiFetch<PurchaseSpendRow[]>(
    `/api/v1/reports/purchasing/spend${buildQuery({ group_by, ...params })}`,
  )
}

export interface PoFulfillmentRow {
  purchase_order_id: number
  purchase_number: string
  status: string
  quantity_ordered: string
  quantity_received: string
  quantity_outstanding: string
}

export function getPoFulfillment(store_id?: number[]): Promise<PoFulfillmentRow[]> {
  return apiFetch<PoFulfillmentRow[]>(
    `/api/v1/reports/purchasing/po-fulfillment${buildQuery({ store_id })}`,
  )
}

export interface SupplierDeliveryPerformanceRow {
  supplier_id: number
  supplier_name: string
  receipt_count: number
  average_lead_time_days: string | null
}

export function getDeliveryPerformance(
  store_id?: number[],
): Promise<SupplierDeliveryPerformanceRow[]> {
  return apiFetch<SupplierDeliveryPerformanceRow[]>(
    `/api/v1/reports/purchasing/delivery-performance${buildQuery({ store_id })}`,
  )
}

export interface PurchasePriceVarianceRow {
  product_id: number | null
  total_variance: string
}

export function getPurchasePriceVariance(
  params: ReportDateRange = {},
): Promise<PurchasePriceVarianceRow[]> {
  return apiFetch<PurchasePriceVarianceRow[]>(
    `/api/v1/reports/purchasing/price-variance${buildQuery(params)}`,
  )
}

// --- Labor & payroll analytics --------------------------------------------

export interface HeadcountRow {
  store_id: number
  active_employee_count: number
}

export function getHeadcount(as_of?: string, store_id?: number[]): Promise<HeadcountRow[]> {
  return apiFetch<HeadcountRow[]>(
    `/api/v1/reports/payroll/headcount${buildQuery({ as_of, store_id })}`,
  )
}

export interface PayrollCostSummary {
  store_ids: number[] | null
  period_start: string
  period_end: string
  gross_pay: string
  deductions: string
  net_pay: string
  employer_contributions: string
  labor_cost: string
  regular_hours: string
  overtime_hours: string
  pending_period_count: number
  statutory_disclaimer: string
}

export function getPayrollCostSummary(
  period_start: string,
  period_end: string,
  store_id?: number[],
): Promise<PayrollCostSummary> {
  return apiFetch<PayrollCostSummary>(
    `/api/v1/reports/payroll/cost-summary${buildQuery({ period_start, period_end, store_id })}`,
  )
}

export interface LaborCostPercentRow {
  labor_cost: string
  net_sales: string
  labor_cost_percent: string | null
}

export function getLaborCostPercent(
  period_start: string,
  period_end: string,
  store_id?: number[],
): Promise<LaborCostPercentRow> {
  return apiFetch<LaborCostPercentRow>(
    `/api/v1/reports/payroll/labor-cost-percent${buildQuery({ period_start, period_end, store_id })}`,
  )
}

export function getPayrollReconciliation(store_id?: number[]): Promise<GlReconciliationRow> {
  return apiFetch<GlReconciliationRow>(
    `/api/v1/reports/payroll/reconciliation${buildQuery({ store_id })}`,
  )
}

// --- KPI dashboard ---------------------------------------------------------

export interface KpiDashboard {
  store_ids: number[] | null
  period_start: string
  period_end: string
  net_sales: string
  gross_margin_percent: string | null
  cogs_percent_of_net_sales: string | null
  average_transaction_value: string | null
  inventory_value: string
  inventory_turnover_note: string
  stockout_count: number
  shrinkage_units: string
  ap_outstanding: string
  ap_overdue: string
  purchase_spend: string
  labor_cost: string
  labor_cost_percent: string | null
  pending_payroll_periods: number
}

export function getKpiDashboard(
  period_start: string,
  period_end: string,
  store_id?: number[],
): Promise<KpiDashboard> {
  return apiFetch<KpiDashboard>(
    `/api/v1/reports/dashboard${buildQuery({ period_start, period_end, store_id })}`,
  )
}
