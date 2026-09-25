import { useEffect, useState, type ReactNode } from 'react'
import * as reportsApi from '../api/reports'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

/**
 * M16: Reports screen. Every figure rendered here comes verbatim from
 * app/api/v1/endpoints/reports.py (backed by the tested M11 reporting
 * services) — nothing is recomputed in this file. Money/quantity fields
 * stay as the server's Decimal strings; the only arithmetic done here is
 * Number() comparisons for display coloring (e.g. "is this variance
 * negative"), never a sum, difference, or ratio presented as a figure.
 *
 * "Sales gross profit" (from /sales/summary — sale price vs. matched
 * unit cost) and "P&L gross profit" (from /financial/profit-loss — GL
 * revenue vs. COGS account balances) are two independently computed
 * numbers and are labeled and displayed separately; they are never
 * combined or presented as though they must agree.
 */

function fmt(value: string | null | undefined): string {
  return value ?? '—'
}

function money(value: string | null | undefined): string {
  return value ?? '—'
}

// --- shared building blocks -------------------------------------------

function Card({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded border border-gray-200 bg-white p-4">
      <div className="text-xs font-medium text-gray-500">{label}</div>
      <div className="mt-1 text-xl font-semibold text-gray-900">{value}</div>
      {hint && <div className="mt-1 text-xs text-gray-400">{hint}</div>}
    </div>
  )
}

interface Column<T> {
  header: string
  render: (row: T) => ReactNode
  align?: 'right'
}

function ReportTable<T>({
  columns,
  rows,
  keyFn,
}: {
  columns: Column<T>[]
  rows: T[]
  keyFn: (row: T, index: number) => string | number
}) {
  return (
    <table className="mt-3 w-full text-left text-sm">
      <thead>
        <tr className="border-b border-gray-200 text-gray-500">
          {columns.map((col) => (
            <th
              key={col.header}
              className={`py-2 pr-4 ${col.align === 'right' ? 'text-right' : ''}`}
            >
              {col.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={keyFn(row, i)} className="border-b border-gray-100">
            {columns.map((col) => (
              <td
                key={col.header}
                className={`py-2 pr-4 ${col.align === 'right' ? 'text-right' : ''}`}
              >
                {col.render(row)}
              </td>
            ))}
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={columns.length} className="py-6 text-center text-gray-400">
              No data for this period.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}

function ReportSection<T>({
  title,
  subtitle,
  load,
  deps,
  render,
}: {
  title: string
  subtitle?: string
  load: () => Promise<T>
  deps: unknown[]
  render: (data: T) => ReactNode
}) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    load()
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load report.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return (
    <div className="mt-4 rounded border border-gray-200 bg-white p-4">
      <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
      {subtitle && <p className="mt-0.5 text-xs text-gray-400">{subtitle}</p>}
      {loading && <p className="mt-3 text-sm text-gray-500">Loading…</p>}
      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      {!loading && !error && data !== null && render(data)}
    </div>
  )
}

function DateRangeFilter({
  dateFrom,
  dateTo,
  onChange,
}: {
  dateFrom: string
  dateTo: string
  onChange: (range: { dateFrom: string; dateTo: string }) => void
}) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-2">
      <label className="text-xs text-gray-500">
        From
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => onChange({ dateFrom: e.target.value, dateTo })}
          className="ml-2 rounded border border-gray-300 px-2 py-1 text-sm"
        />
      </label>
      <label className="text-xs text-gray-500">
        To
        <input
          type="date"
          value={dateTo}
          onChange={(e) => onChange({ dateFrom, dateTo: e.target.value })}
          className="ml-2 rounded border border-gray-300 px-2 py-1 text-sm"
        />
      </label>
    </div>
  )
}

function firstOfMonth(): string {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10)
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

// --- Sales & profitability ----------------------------------------------

function SalesTab() {
  const [dateFrom, setDateFrom] = useState(firstOfMonth())
  const [dateTo, setDateTo] = useState(today())
  const [dimension, setDimension] = useState<'store' | 'product'>('store')
  const range = { date_from: dateFrom, date_to: dateTo }

  return (
    <div>
      <DateRangeFilter
        dateFrom={dateFrom}
        dateTo={dateTo}
        onChange={(r) => {
          setDateFrom(r.dateFrom)
          setDateTo(r.dateTo)
        }}
      />

      <ReportSection
        title="Sales summary (operational)"
        subtitle="Computed from sale/return lines matched against unit cost at time of sale — not the GL. See Financial → Profit &amp; Loss for the GL-derived figure."
        load={() => reportsApi.getSalesSummary(range)}
        deps={[dateFrom, dateTo]}
        render={(s: reportsApi.SalesSummary) => (
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Card label="Gross sales" value={money(s.gross_sales)} />
            <Card label="Discounts" value={money(s.discounts)} />
            <Card label="Returns" value={money(s.returns)} />
            <Card label="Net sales" value={money(s.net_sales)} />
            <Card label="COGS" value={money(s.cogs)} />
            <Card label="Gross profit (operational)" value={money(s.gross_profit)} />
            <Card
              label="Gross margin"
              value={s.gross_margin_percent !== null ? `${s.gross_margin_percent}%` : '—'}
            />
            <Card label="Transactions" value={s.transaction_count} />
            <Card label="Units sold" value={fmt(s.units_sold)} />
            <Card label="Avg transaction value" value={money(s.average_transaction_value)} />
            <Card label="Voids" value={`${s.void_count} (${money(s.void_amount)})`} />
            <Card label="Tax collected" value={money(s.tax)} />
          </div>
        )}
      />

      <ReportSection
        title="Sales breakdown"
        load={() => reportsApi.getSalesBreakdown(dimension, range)}
        deps={[dimension, dateFrom, dateTo]}
        render={(rows: reportsApi.SalesByDimensionRow[]) => (
          <>
            <div className="mt-2 flex gap-2">
              {(['store', 'product'] as const).map((d) => (
                <button
                  key={d}
                  onClick={() => setDimension(d)}
                  className={`rounded px-2 py-1 text-xs font-medium ${
                    dimension === d ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  By {d}
                </button>
              ))}
            </div>
            <ReportTable
              columns={[
                { header: dimension === 'store' ? 'Store' : 'Product', render: (r) => r.label },
                { header: 'Gross sales', render: (r) => money(r.gross_sales), align: 'right' },
                { header: 'Returns', render: (r) => money(r.returns), align: 'right' },
                { header: 'Net sales', render: (r) => money(r.net_sales), align: 'right' },
                { header: 'COGS', render: (r) => money(r.cogs), align: 'right' },
                { header: 'Units', render: (r) => fmt(r.units_sold), align: 'right' },
                { header: 'Transactions', render: (r) => r.transaction_count, align: 'right' },
              ]}
              rows={rows}
              keyFn={(r) => r.key}
            />
          </>
        )}
      />

      <ReportSection
        title="Sales by payment method"
        load={() => reportsApi.getSalesByPaymentMethod(range)}
        deps={[dateFrom, dateTo]}
        render={(rows: reportsApi.PaymentMethodRow[]) => (
          <ReportTable
            columns={[
              { header: 'Method', render: (r) => r.payment_method },
              { header: 'Amount', render: (r) => money(r.amount), align: 'right' },
              { header: 'Payments', render: (r) => r.payment_count, align: 'right' },
            ]}
            rows={rows}
            keyFn={(r) => r.payment_method}
          />
        )}
      />

      <ReportSection
        title="Daily trend"
        load={() => reportsApi.getSalesTrend(dateFrom, dateTo)}
        deps={[dateFrom, dateTo]}
        render={(rows: reportsApi.SalesTrendPoint[]) => (
          <ReportTable
            columns={[
              { header: 'Date', render: (r) => r.sale_date },
              { header: 'Gross sales', render: (r) => money(r.gross_sales), align: 'right' },
              { header: 'Returns', render: (r) => money(r.returns), align: 'right' },
              { header: 'Net sales', render: (r) => money(r.net_sales), align: 'right' },
              { header: 'Transactions', render: (r) => r.transaction_count, align: 'right' },
            ]}
            rows={rows}
            keyFn={(r) => r.sale_date}
          />
        )}
      />
    </div>
  )
}

// --- Financial (GL-derived) ----------------------------------------------

type FinancialSubTab = 'trial-balance' | 'profit-loss' | 'balance-sheet' | 'cash-summary'

function FinancialTab() {
  const [sub, setSub] = useState<FinancialSubTab>('profit-loss')
  const [dateFrom, setDateFrom] = useState(firstOfMonth())
  const [dateTo, setDateTo] = useState(today())
  const [asOf, setAsOf] = useState(today())
  const range = { date_from: dateFrom, date_to: dateTo }

  const subTabs: { id: FinancialSubTab; label: string }[] = [
    { id: 'profit-loss', label: 'Profit & Loss' },
    { id: 'trial-balance', label: 'Trial Balance' },
    { id: 'balance-sheet', label: 'Balance Sheet' },
    { id: 'cash-summary', label: 'Cash Reconciliation' },
  ]

  return (
    <div>
      <div className="mt-4 flex gap-1">
        {subTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setSub(t.id)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              sub === t.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {sub !== 'balance-sheet' && (
        <DateRangeFilter
          dateFrom={dateFrom}
          dateTo={dateTo}
          onChange={(r) => {
            setDateFrom(r.dateFrom)
            setDateTo(r.dateTo)
          }}
        />
      )}
      {sub === 'balance-sheet' && (
        <div className="mt-4">
          <label className="text-xs text-gray-500">
            As of
            <input
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              className="ml-2 rounded border border-gray-300 px-2 py-1 text-sm"
            />
          </label>
        </div>
      )}

      {sub === 'profit-loss' && (
        <ReportSection
          title="Profit &amp; Loss (GL-derived)"
          subtitle="From posted journal entries only — the accounting source of truth. May differ from Sales → Sales summary's operational gross profit, which is computed from sale lines instead."
          load={() => reportsApi.getProfitAndLoss(range)}
          deps={[dateFrom, dateTo]}
          render={(pnl: reportsApi.ProfitAndLoss) => (
            <div className="mt-3 max-w-md">
              {(
                [
                  ['Net sales', pnl.net_sales, false],
                  ['Cost of goods sold', `(${pnl.cogs})`, false],
                  ['Gross profit (GL-derived)', pnl.gross_profit, true],
                  ['Other income', pnl.other_income, false],
                  ['Operating expenses', `(${pnl.operating_expenses})`, false],
                  ['Net income', pnl.net_income, true],
                ] as const
              ).map(([label, value, emphasize]) => (
                <div
                  key={label}
                  className={`flex justify-between border-b border-gray-100 py-2 ${
                    emphasize ? 'font-semibold' : ''
                  }`}
                >
                  <span>{label}</span>
                  <span>{value}</span>
                </div>
              ))}
            </div>
          )}
        />
      )}

      {sub === 'trial-balance' && (
        <ReportSection
          title="Trial balance"
          load={() => reportsApi.getTrialBalance(range)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.TrialBalanceRow[]) => (
            <ReportTable
              columns={[
                {
                  header: 'Account',
                  render: (r) => (
                    <>
                      <span className="font-mono text-xs text-gray-500">{r.account_code}</span>{' '}
                      {r.account_name}
                    </>
                  ),
                },
                { header: 'Type', render: (r) => r.account_type },
                { header: 'Debit', render: (r) => money(r.total_debit), align: 'right' },
                { header: 'Credit', render: (r) => money(r.total_credit), align: 'right' },
              ]}
              rows={rows}
              keyFn={(r) => r.account_code}
            />
          )}
        />
      )}

      {sub === 'balance-sheet' && (
        <ReportSection
          title="Balance sheet summary"
          load={() => reportsApi.getBalanceSheet({ as_of: asOf })}
          deps={[asOf]}
          render={(bs: reportsApi.BalanceSheetSummary) => (
            <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <h4 className="text-xs font-semibold text-gray-500">
                  Assets (total {money(bs.total_assets)})
                </h4>
                <ReportTable
                  columns={[
                    { header: 'Account', render: (r) => r.account_name },
                    { header: 'Balance', render: (r) => money(r.total_debit), align: 'right' },
                  ]}
                  rows={bs.assets}
                  keyFn={(r) => r.account_code}
                />
              </div>
              <div>
                <h4 className="text-xs font-semibold text-gray-500">
                  Liabilities (total {money(bs.total_liabilities)})
                </h4>
                <ReportTable
                  columns={[
                    { header: 'Account', render: (r) => r.account_name },
                    { header: 'Balance', render: (r) => money(r.total_credit), align: 'right' },
                  ]}
                  rows={bs.liabilities}
                  keyFn={(r) => r.account_code}
                />
              </div>
            </div>
          )}
        />
      )}

      {sub === 'cash-summary' && (
        <ReportSection
          title="Payment method reconciliation"
          subtitle="Compares operational payment totals against the corresponding GL cash/bank account balance."
          load={() => reportsApi.getCashSummary(range)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.PaymentMethodReconciliationRow[]) => (
            <ReportTable
              columns={[
                { header: 'Method', render: (r) => r.payment_method },
                {
                  header: 'Operational amount',
                  render: (r) => money(r.operational_amount),
                  align: 'right',
                },
                { header: 'GL account', render: (r) => r.gl_account_code },
                { header: 'GL balance', render: (r) => money(r.gl_balance), align: 'right' },
                {
                  header: 'Discrepancy',
                  render: (r) => (
                    <span
                      className={Number(r.discrepancy) === 0 ? 'text-green-700' : 'text-red-600'}
                    >
                      {r.discrepancy}
                    </span>
                  ),
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r) => r.payment_method}
            />
          )}
        />
      )}
    </div>
  )
}

// --- Inventory analytics ---------------------------------------------------

type InventorySubTab =
  | 'value'
  | 'movements'
  | 'shrinkage'
  | 'turnover'
  | 'slow-moving'
  | 'stockouts'
  | 'negative-stock'
  | 'in-transit'
  | 'stock-count-variance'

function InventoryTab() {
  const [sub, setSub] = useState<InventorySubTab>('value')
  const [dateFrom, setDateFrom] = useState(firstOfMonth())
  const [dateTo, setDateTo] = useState(today())
  const [groupBy, setGroupBy] = useState<'store' | 'category'>('store')
  const [stockCountId, setStockCountId] = useState('')
  const range = { date_from: dateFrom, date_to: dateTo }

  const subTabs: { id: InventorySubTab; label: string; needsDates?: boolean }[] = [
    { id: 'value', label: 'Value on hand' },
    { id: 'movements', label: 'Movements', needsDates: true },
    { id: 'shrinkage', label: 'Shrinkage', needsDates: true },
    { id: 'turnover', label: 'Turnover', needsDates: true },
    { id: 'slow-moving', label: 'Slow-moving' },
    { id: 'stockouts', label: 'Stockouts' },
    { id: 'negative-stock', label: 'Negative stock' },
    { id: 'in-transit', label: 'In-transit' },
    { id: 'stock-count-variance', label: 'Stock count variance' },
  ]

  return (
    <div>
      <div className="mt-4 flex flex-wrap gap-1">
        {subTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setSub(t.id)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              sub === t.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {(sub === 'movements' || sub === 'shrinkage' || sub === 'turnover') && (
        <DateRangeFilter
          dateFrom={dateFrom}
          dateTo={dateTo}
          onChange={(r) => {
            setDateFrom(r.dateFrom)
            setDateTo(r.dateTo)
          }}
        />
      )}

      {sub === 'value' && (
        <ReportSection
          title="Inventory value on hand"
          load={() => reportsApi.getInventoryValue(groupBy)}
          deps={[groupBy]}
          render={(rows: reportsApi.InventoryValueRow[]) => (
            <>
              <div className="mt-2 flex gap-2">
                {(['store', 'category'] as const).map((g) => (
                  <button
                    key={g}
                    onClick={() => setGroupBy(g)}
                    className={`rounded px-2 py-1 text-xs font-medium ${
                      groupBy === g ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    By {g}
                  </button>
                ))}
              </div>
              <ReportTable
                columns={[
                  { header: groupBy === 'store' ? 'Store' : 'Category', render: (r) => r.label },
                  { header: 'Qty on hand', render: (r) => fmt(r.quantity_on_hand), align: 'right' },
                  { header: 'Value', render: (r) => money(r.value), align: 'right' },
                ]}
                rows={rows}
                keyFn={(r) => r.key}
              />
            </>
          )}
        />
      )}

      {sub === 'movements' && (
        <ReportSection
          title="Movement summary"
          load={() => reportsApi.getInventoryMovements(range)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.MovementSummaryRow[]) => (
            <ReportTable
              columns={[
                { header: 'Movement type', render: (r) => r.movement_type },
                { header: 'Quantity', render: (r) => fmt(r.quantity), align: 'right' },
                { header: 'Count', render: (r) => r.movement_count, align: 'right' },
              ]}
              rows={rows}
              keyFn={(r) => r.movement_type}
            />
          )}
        />
      )}

      {sub === 'shrinkage' && (
        <ReportSection
          title="Shrinkage (stock adjustments)"
          load={() => reportsApi.getShrinkage(range)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.ShrinkageRow[]) => (
            <ReportTable
              columns={[
                { header: 'Store', render: (r) => `#${r.store_id}` },
                { header: 'Reason', render: (r) => r.reason_code },
                { header: 'Quantity', render: (r) => fmt(r.quantity), align: 'right' },
                { header: 'Adjustments', render: (r) => r.adjustment_count, align: 'right' },
              ]}
              rows={rows}
              keyFn={(r, i) => `${r.store_id}-${r.reason_code}-${i}`}
            />
          )}
        />
      )}

      {sub === 'turnover' && (
        <ReportSection
          title="Inventory turnover"
          load={() => reportsApi.getInventoryTurnover(dateFrom, dateTo)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.TurnoverRow[]) => (
            <ReportTable
              columns={[
                { header: 'Product', render: (r) => r.product_name },
                { header: 'COGS', render: (r) => money(r.cogs), align: 'right' },
                {
                  header: 'Avg inventory value',
                  render: (r) => money(r.average_inventory_value),
                  align: 'right',
                },
                { header: 'Turnover', render: (r) => fmt(r.turnover), align: 'right' },
                { header: 'Days on hand', render: (r) => fmt(r.days_on_hand), align: 'right' },
              ]}
              rows={rows}
              keyFn={(r) => r.product_id}
            />
          )}
        />
      )}

      {sub === 'slow-moving' && (
        <ReportSection
          title="Slow-moving products (90-day window)"
          load={() => reportsApi.getSlowMoving(90)}
          deps={[]}
          render={(rows: reportsApi.SlowMovingRow[]) => (
            <ReportTable
              columns={[
                { header: 'Product', render: (r) => r.product_name },
                { header: 'Store', render: (r) => `#${r.store_id}` },
                { header: 'Qty on hand', render: (r) => fmt(r.quantity_on_hand), align: 'right' },
                {
                  header: 'Units sold in window',
                  render: (r) => fmt(r.units_sold_in_window),
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r) => `${r.product_id}-${r.store_id}`}
            />
          )}
        />
      )}

      {sub === 'stockouts' && (
        <ReportSection
          title="Stockouts"
          load={() => reportsApi.getStockouts()}
          deps={[]}
          render={(rows: reportsApi.StockoutRow[]) => (
            <ReportTable
              columns={[
                { header: 'Product', render: (r) => r.product_name },
                { header: 'Store', render: (r) => `#${r.store_id}` },
              ]}
              rows={rows}
              keyFn={(r) => `${r.product_id}-${r.store_id}`}
            />
          )}
        />
      )}

      {sub === 'negative-stock' && (
        <ReportSection
          title="Negative stock"
          load={() => reportsApi.getNegativeStock()}
          deps={[]}
          render={(rows: reportsApi.NegativeStockRow[]) => (
            <ReportTable
              columns={[
                { header: 'Product', render: (r) => r.product_name },
                { header: 'Store', render: (r) => `#${r.store_id}` },
                { header: 'Qty on hand', render: (r) => fmt(r.quantity_on_hand), align: 'right' },
              ]}
              rows={rows}
              keyFn={(r) => `${r.product_id}-${r.store_id}`}
            />
          )}
        />
      )}

      {sub === 'in-transit' && (
        <>
          <ReportSection
            title="Inventory in transit"
            load={() => reportsApi.getInTransit()}
            deps={[]}
            render={(rows: reportsApi.InTransitRow[]) => (
              <ReportTable
                columns={[
                  { header: 'Transfer', render: (r) => `#${r.transfer_id}` },
                  {
                    header: 'From → To',
                    render: (r) => `Store #${r.from_store_id} → #${r.to_store_id}`,
                  },
                  {
                    header: 'Qty in transit',
                    render: (r) => fmt(r.quantity_in_transit),
                    align: 'right',
                  },
                  {
                    header: 'Value in transit',
                    render: (r) => money(r.value_in_transit),
                    align: 'right',
                  },
                ]}
                rows={rows}
                keyFn={(r) => r.transfer_id}
              />
            )}
          />
          <ReportSection
            title="In-transit GL reconciliation"
            subtitle="Company-wide by design — the GL account represents value in transit between stores, not within one."
            load={() => reportsApi.getInTransitReconciliation()}
            deps={[]}
            render={(r: reportsApi.GlReconciliationRow) => (
              <div className="mt-3 grid grid-cols-3 gap-3">
                <Card label="GL balance" value={money(r.gl_balance)} />
                <Card label="Operational value" value={money(r.operational_value)} />
                <Card
                  label="Discrepancy"
                  value={
                    <span
                      className={Number(r.discrepancy) === 0 ? 'text-green-700' : 'text-red-600'}
                    >
                      {r.discrepancy}
                    </span>
                  }
                />
              </div>
            )}
          />
        </>
      )}

      {sub === 'stock-count-variance' && (
        <div className="mt-4">
          <label className="text-xs text-gray-500">
            Stock count ID
            <input
              type="number"
              value={stockCountId}
              onChange={(e) => setStockCountId(e.target.value)}
              className="ml-2 w-28 rounded border border-gray-300 px-2 py-1 text-sm"
              placeholder="e.g. 12"
            />
          </label>
          {stockCountId.trim() !== '' && (
            <ReportSection
              title={`Variance for stock count #${stockCountId}`}
              load={() => reportsApi.getStockCountVariance(Number(stockCountId))}
              deps={[stockCountId]}
              render={(rows: reportsApi.StockCountVarianceRow[]) => (
                <ReportTable
                  columns={[
                    { header: 'Product', render: (r) => `#${r.product_id}` },
                    { header: 'Expected', render: (r) => fmt(r.expected_quantity), align: 'right' },
                    { header: 'Counted', render: (r) => fmt(r.counted_quantity), align: 'right' },
                    {
                      header: 'Variance',
                      render: (r) => (
                        <span className={Number(r.variance) === 0 ? '' : 'text-red-600'}>
                          {r.variance}
                        </span>
                      ),
                      align: 'right',
                    },
                  ]}
                  rows={rows}
                  keyFn={(r) => r.product_id}
                />
              )}
            />
          )}
        </div>
      )}
    </div>
  )
}

// --- Purchasing & supplier analytics ---------------------------------------

type PurchasingSubTab = 'spend' | 'po-fulfillment' | 'delivery-performance' | 'price-variance'

function PurchasingTab() {
  const [sub, setSub] = useState<PurchasingSubTab>('spend')
  const [dateFrom, setDateFrom] = useState(firstOfMonth())
  const [dateTo, setDateTo] = useState(today())
  const [groupBy, setGroupBy] = useState<'supplier' | 'store' | 'product'>('supplier')
  const range = { date_from: dateFrom, date_to: dateTo }

  const subTabs: { id: PurchasingSubTab; label: string }[] = [
    { id: 'spend', label: 'Spend' },
    { id: 'po-fulfillment', label: 'PO fulfillment' },
    { id: 'delivery-performance', label: 'Delivery performance' },
    { id: 'price-variance', label: 'Price variance' },
  ]

  return (
    <div>
      <div className="mt-4 flex flex-wrap gap-1">
        {subTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setSub(t.id)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              sub === t.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {(sub === 'spend' || sub === 'price-variance') && (
        <DateRangeFilter
          dateFrom={dateFrom}
          dateTo={dateTo}
          onChange={(r) => {
            setDateFrom(r.dateFrom)
            setDateTo(r.dateTo)
          }}
        />
      )}

      {sub === 'spend' && (
        <ReportSection
          title="Purchasing spend"
          load={() => reportsApi.getPurchasingSpend(groupBy, range)}
          deps={[groupBy, dateFrom, dateTo]}
          render={(rows: reportsApi.PurchaseSpendRow[]) => (
            <>
              <div className="mt-2 flex gap-2">
                {(['supplier', 'store', 'product'] as const).map((g) => (
                  <button
                    key={g}
                    onClick={() => setGroupBy(g)}
                    className={`rounded px-2 py-1 text-xs font-medium ${
                      groupBy === g ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    By {g}
                  </button>
                ))}
              </div>
              <ReportTable
                columns={[
                  { header: 'Label', render: (r) => r.label },
                  {
                    header: 'Qty received',
                    render: (r) => fmt(r.quantity_received),
                    align: 'right',
                  },
                  { header: 'Spend', render: (r) => money(r.spend), align: 'right' },
                ]}
                rows={rows}
                keyFn={(r) => r.key}
              />
            </>
          )}
        />
      )}

      {sub === 'po-fulfillment' && (
        <ReportSection
          title="Purchase order fulfillment"
          load={() => reportsApi.getPoFulfillment()}
          deps={[]}
          render={(rows: reportsApi.PoFulfillmentRow[]) => (
            <ReportTable
              columns={[
                { header: 'PO', render: (r) => r.purchase_number },
                { header: 'Status', render: (r) => r.status },
                { header: 'Ordered', render: (r) => fmt(r.quantity_ordered), align: 'right' },
                { header: 'Received', render: (r) => fmt(r.quantity_received), align: 'right' },
                {
                  header: 'Outstanding',
                  render: (r) => fmt(r.quantity_outstanding),
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r) => r.purchase_order_id}
            />
          )}
        />
      )}

      {sub === 'delivery-performance' && (
        <ReportSection
          title="Supplier delivery performance"
          load={() => reportsApi.getDeliveryPerformance()}
          deps={[]}
          render={(rows: reportsApi.SupplierDeliveryPerformanceRow[]) => (
            <ReportTable
              columns={[
                { header: 'Supplier', render: (r) => r.supplier_name },
                { header: 'Receipts', render: (r) => r.receipt_count, align: 'right' },
                {
                  header: 'Avg lead time (days)',
                  render: (r) => fmt(r.average_lead_time_days),
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r) => r.supplier_id}
            />
          )}
        />
      )}

      {sub === 'price-variance' && (
        <ReportSection
          title="Purchase price variance"
          load={() => reportsApi.getPurchasePriceVariance(range)}
          deps={[dateFrom, dateTo]}
          render={(rows: reportsApi.PurchasePriceVarianceRow[]) => (
            <ReportTable
              columns={[
                {
                  header: 'Product',
                  render: (r) => (r.product_id !== null ? `#${r.product_id}` : '—'),
                },
                {
                  header: 'Total variance',
                  render: (r) => (
                    <span
                      className={Number(r.total_variance) > 0 ? 'text-red-600' : 'text-green-700'}
                    >
                      {r.total_variance}
                    </span>
                  ),
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r, i) => r.product_id ?? `null-${i}`}
            />
          )}
        />
      )}
    </div>
  )
}

// --- Labor & payroll analytics ----------------------------------------------

type PayrollSubTab = 'headcount' | 'cost-summary' | 'labor-cost-percent' | 'reconciliation'

function PayrollTab() {
  const [sub, setSub] = useState<PayrollSubTab>('cost-summary')
  const [periodStart, setPeriodStart] = useState(firstOfMonth())
  const [periodEnd, setPeriodEnd] = useState(today())

  const subTabs: { id: PayrollSubTab; label: string }[] = [
    { id: 'cost-summary', label: 'Cost summary' },
    { id: 'labor-cost-percent', label: 'Labor cost % of sales' },
    { id: 'headcount', label: 'Headcount' },
    { id: 'reconciliation', label: 'GL reconciliation' },
  ]

  return (
    <div>
      <div className="mt-4 flex flex-wrap gap-1">
        {subTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setSub(t.id)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              sub === t.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {(sub === 'cost-summary' || sub === 'labor-cost-percent') && (
        <DateRangeFilter
          dateFrom={periodStart}
          dateTo={periodEnd}
          onChange={(r) => {
            setPeriodStart(r.dateFrom)
            setPeriodEnd(r.dateTo)
          }}
        />
      )}

      {sub === 'cost-summary' && (
        <ReportSection
          title="Payroll cost summary"
          subtitle="Includes only posted payroll periods within this window."
          load={() => reportsApi.getPayrollCostSummary(periodStart, periodEnd)}
          deps={[periodStart, periodEnd]}
          render={(s: reportsApi.PayrollCostSummary) => (
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Card label="Gross pay" value={money(s.gross_pay)} />
              <Card label="Deductions" value={money(s.deductions)} />
              <Card label="Net pay" value={money(s.net_pay)} />
              <Card label="Employer contributions" value={money(s.employer_contributions)} />
              <Card label="Total labor cost" value={money(s.labor_cost)} />
              <Card label="Regular hours" value={fmt(s.regular_hours)} />
              <Card label="Overtime hours" value={fmt(s.overtime_hours)} />
              <Card label="Pending periods" value={s.pending_period_count} />
              <div className="col-span-2 sm:col-span-4">
                <p className="mt-1 text-xs text-gray-400">{s.statutory_disclaimer}</p>
              </div>
            </div>
          )}
        />
      )}

      {sub === 'labor-cost-percent' && (
        <ReportSection
          title="Labor cost as % of net sales"
          load={() => reportsApi.getLaborCostPercent(periodStart, periodEnd)}
          deps={[periodStart, periodEnd]}
          render={(r: reportsApi.LaborCostPercentRow) => (
            <div className="mt-3 grid grid-cols-3 gap-3">
              <Card label="Labor cost" value={money(r.labor_cost)} />
              <Card label="Net sales" value={money(r.net_sales)} />
              <Card
                label="Labor cost %"
                value={r.labor_cost_percent !== null ? `${r.labor_cost_percent}%` : '—'}
              />
            </div>
          )}
        />
      )}

      {sub === 'headcount' && (
        <ReportSection
          title="Active headcount by store"
          load={() => reportsApi.getHeadcount()}
          deps={[]}
          render={(rows: reportsApi.HeadcountRow[]) => (
            <ReportTable
              columns={[
                { header: 'Store', render: (r) => `#${r.store_id}` },
                {
                  header: 'Active employees',
                  render: (r) => r.active_employee_count,
                  align: 'right',
                },
              ]}
              rows={rows}
              keyFn={(r) => r.store_id}
            />
          )}
        />
      )}

      {sub === 'reconciliation' && (
        <ReportSection
          title="Payroll GL reconciliation"
          load={() => reportsApi.getPayrollReconciliation()}
          deps={[]}
          render={(r: reportsApi.GlReconciliationRow) => (
            <div className="mt-3 grid grid-cols-3 gap-3">
              <Card label="GL balance" value={money(r.gl_balance)} />
              <Card label="Operational value" value={money(r.operational_value)} />
              <Card
                label="Discrepancy"
                value={
                  <span className={Number(r.discrepancy) === 0 ? 'text-green-700' : 'text-red-600'}>
                    {r.discrepancy}
                  </span>
                }
              />
            </div>
          )}
        />
      )}
    </div>
  )
}

// --- KPI dashboard -----------------------------------------------------------

function DashboardTab() {
  const [periodStart, setPeriodStart] = useState(firstOfMonth())
  const [periodEnd, setPeriodEnd] = useState(today())

  return (
    <div>
      <DateRangeFilter
        dateFrom={periodStart}
        dateTo={periodEnd}
        onChange={(r) => {
          setPeriodStart(r.dateFrom)
          setPeriodEnd(r.dateTo)
        }}
      />
      <ReportSection
        title="KPI dashboard"
        subtitle="Every figure here is a documented slice of the reports in the other tabs — nothing is a new computation."
        load={() => reportsApi.getKpiDashboard(periodStart, periodEnd)}
        deps={[periodStart, periodEnd]}
        render={(k: reportsApi.KpiDashboard) => (
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Card label="Net sales" value={money(k.net_sales)} />
            <Card
              label="Gross margin"
              value={k.gross_margin_percent !== null ? `${k.gross_margin_percent}%` : '—'}
            />
            <Card
              label="COGS % of net sales"
              value={k.cogs_percent_of_net_sales !== null ? `${k.cogs_percent_of_net_sales}%` : '—'}
            />
            <Card label="Avg transaction value" value={money(k.average_transaction_value)} />
            <Card
              label="Inventory value"
              value={money(k.inventory_value)}
              hint={k.inventory_turnover_note}
            />
            <Card label="Stockouts" value={k.stockout_count} />
            <Card label="Shrinkage units" value={fmt(k.shrinkage_units)} />
            <Card label="AP outstanding" value={money(k.ap_outstanding)} />
            <Card label="AP overdue" value={money(k.ap_overdue)} />
            <Card label="Purchase spend" value={money(k.purchase_spend)} />
            <Card label="Labor cost" value={money(k.labor_cost)} />
            <Card
              label="Labor cost %"
              value={k.labor_cost_percent !== null ? `${k.labor_cost_percent}%` : '—'}
            />
            <Card label="Pending payroll periods" value={k.pending_payroll_periods} />
          </div>
        )}
      />
    </div>
  )
}

// --- Top-level page -----------------------------------------------------------

type MainTab = 'sales' | 'financial' | 'inventory' | 'purchasing' | 'payroll' | 'dashboard'

export function ReportsPage() {
  const { hasPermission } = useAuth()
  const [tab, setTab] = useState<MainTab>('sales')

  const allTabs: { id: MainTab; label: string; permission: string }[] = [
    { id: 'sales', label: 'Sales', permission: 'reports.read' },
    { id: 'dashboard', label: 'Dashboard', permission: 'reports.read' },
    { id: 'financial', label: 'Financial', permission: 'accounting.read' },
    { id: 'inventory', label: 'Inventory', permission: 'inventory.read' },
    { id: 'purchasing', label: 'Purchasing', permission: 'purchasing.read' },
    { id: 'payroll', label: 'Payroll', permission: 'payroll.read' },
  ]
  const tabs = allTabs.filter((t) => hasPermission(t.permission))

  const activeTab = tabs.some((t) => t.id === tab) ? tab : tabs[0]?.id

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Reports</h1>
      <div className="mt-4 flex flex-wrap gap-1 border-b border-gray-200">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-sm font-medium ${
              activeTab === t.id
                ? 'border-b-2 border-blue-600 text-blue-600'
                : 'text-gray-500 hover:text-gray-800'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'sales' && <SalesTab />}
      {activeTab === 'dashboard' && <DashboardTab />}
      {activeTab === 'financial' && <FinancialTab />}
      {activeTab === 'inventory' && <InventoryTab />}
      {activeTab === 'purchasing' && <PurchasingTab />}
      {activeTab === 'payroll' && <PayrollTab />}
      {tabs.length === 0 && (
        <p className="mt-4 text-sm text-gray-500">
          You do not have permission to view any reports.
        </p>
      )}
    </div>
  )
}
