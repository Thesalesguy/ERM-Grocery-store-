"""Pydantic read schemas for the M11 reports API. Every schema below
validates directly from the corresponding app.modules.reports.service
dataclass (`from_attributes=True`) — never a hand-copied re-derivation
of the same numbers."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class SalesSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_ids: list[int] | None
    date_from: date | None
    date_to: date | None
    gross_sales: Decimal
    discounts: Decimal
    returns: Decimal
    void_count: int
    void_amount: Decimal
    net_sales: Decimal
    tax: Decimal
    cogs: Decimal
    gross_profit: Decimal
    gross_margin_percent: Decimal | None
    transaction_count: int
    units_sold: Decimal
    average_transaction_value: Decimal | None


class SalesByDimensionRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: int | str
    label: str
    gross_sales: Decimal
    returns: Decimal
    net_sales: Decimal
    cogs: Decimal
    units_sold: Decimal
    transaction_count: int


class PaymentMethodRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_method: str
    amount: Decimal
    payment_count: int


class SalesTrendPointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sale_date: date
    gross_sales: Decimal
    returns: Decimal
    net_sales: Decimal
    transaction_count: int


class TrialBalanceRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_code: str
    account_name: str
    account_type: str
    normal_balance: str
    total_debit: Decimal
    total_credit: Decimal


class ProfitAndLossRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_id: int | None
    date_from: date | None
    date_to: date | None
    net_sales: Decimal
    cogs: Decimal
    gross_profit: Decimal
    other_income: Decimal
    operating_expenses: Decimal
    net_income: Decimal


class ProfitAndLossComparativeRead(BaseModel):
    current: ProfitAndLossRead
    prior: ProfitAndLossRead


class AccountTypeSummaryRead(BaseModel):
    account_type: str
    rows: list[TrialBalanceRowRead]
    total_debit: Decimal
    total_credit: Decimal


class BalanceSheetSummaryRead(BaseModel):
    store_ids: list[int] | None
    as_of: date | None
    assets: list[TrialBalanceRowRead]
    liabilities: list[TrialBalanceRowRead]
    total_assets: Decimal
    total_liabilities: Decimal


class PaymentMethodReconciliationRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_method: str
    operational_amount: Decimal
    gl_account_code: str
    gl_balance: Decimal
    discrepancy: Decimal


class GlReconciliationRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    label: str
    gl_balance: Decimal
    operational_value: Decimal
    discrepancy: Decimal


class InventoryValueRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: int
    label: str
    quantity_on_hand: Decimal
    value: Decimal


class MovementSummaryRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    movement_type: str
    quantity: Decimal
    movement_count: int


class ShrinkageRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_id: int
    reason_code: str
    quantity: Decimal
    adjustment_count: int


class TurnoverRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    cogs: Decimal
    average_inventory_value: Decimal | None
    turnover: Decimal | None
    days_on_hand: Decimal | None


class SlowMovingRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    store_id: int
    quantity_on_hand: Decimal
    units_sold_in_window: Decimal


class StockoutRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    store_id: int


class NegativeStockRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_name: str
    store_id: int
    quantity_on_hand: Decimal


class InTransitRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transfer_id: int
    from_store_id: int
    to_store_id: int
    source_product_id: int
    destination_product_id: int
    quantity_in_transit: Decimal
    value_in_transit: Decimal


class StockCountVarianceRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stock_count_id: int
    product_id: int
    expected_quantity: Decimal
    counted_quantity: Decimal
    variance: Decimal


class PurchaseSpendRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: int
    label: str
    quantity_received: Decimal
    spend: Decimal


class PoFulfillmentRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purchase_order_id: int
    purchase_number: str
    status: str
    quantity_ordered: Decimal
    quantity_received: Decimal
    quantity_outstanding: Decimal


class SupplierDeliveryPerformanceRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    supplier_id: int
    supplier_name: str
    receipt_count: int
    average_lead_time_days: Decimal | None


class PurchasePriceVarianceRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int | None
    total_variance: Decimal


class HeadcountRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_id: int
    active_employee_count: int


class PayrollCostSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_ids: list[int] | None
    period_start: date
    period_end: date
    gross_pay: Decimal
    deductions: Decimal
    net_pay: Decimal
    employer_contributions: Decimal
    labor_cost: Decimal
    regular_hours: Decimal
    overtime_hours: Decimal
    pending_period_count: int
    statutory_disclaimer: str


class LaborCostPercentRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    labor_cost: Decimal
    net_sales: Decimal
    labor_cost_percent: Decimal | None


class KpiDashboardRead(BaseModel):
    """One coherent management dashboard — every figure here is a
    documented, traceable slice of the reports already defined above
    (docs/M11_DESIGN.md Section 8): nothing here is a new computation."""

    store_ids: list[int] | None
    period_start: date
    period_end: date
    net_sales: Decimal
    gross_margin_percent: Decimal | None
    cogs_percent_of_net_sales: Decimal | None
    average_transaction_value: Decimal | None
    inventory_value: Decimal
    inventory_turnover_note: str
    stockout_count: int
    shrinkage_units: Decimal
    ap_outstanding: Decimal
    ap_overdue: Decimal
    purchase_spend: Decimal
    labor_cost: Decimal
    labor_cost_percent: Decimal | None
    pending_payroll_periods: int
