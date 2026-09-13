import { useEffect, useState } from 'react'
import * as payrollApi from '../api/payroll'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const STATUS_STYLES: Record<payrollApi.PayrollPeriodStatus, string> = {
  DRAFT: 'bg-gray-100 text-gray-700',
  OPEN: 'bg-blue-100 text-blue-700',
  CALCULATED: 'bg-purple-100 text-purple-700',
  APPROVED: 'bg-amber-100 text-amber-700',
  POSTED: 'bg-green-100 text-green-700',
  CANCELLED: 'bg-red-100 text-red-700',
}

function StatusBadge({ status }: { status: payrollApi.PayrollPeriodStatus }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium uppercase ${STATUS_STYLES[status]}`}>
      {status}
    </span>
  )
}

function CreatePeriodForm({ storeId, onCreated }: { storeId: number; onCreated: () => void }) {
  const [periodStart, setPeriodStart] = useState('')
  const [periodEnd, setPeriodEnd] = useState('')
  const [payDate, setPayDate] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await payrollApi.createPayrollPeriod({
        store_id: storeId,
        period_start: periodStart,
        period_end: periodEnd,
        pay_date: payDate,
      })
      setPeriodStart('')
      setPeriodEnd('')
      setPayDate('')
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create payroll period.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-900">New payroll period</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <input
          type="date"
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={periodStart}
          onChange={(e) => setPeriodStart(e.target.value)}
          required
        />
        <input
          type="date"
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={periodEnd}
          onChange={(e) => setPeriodEnd(e.target.value)}
          required
        />
        <input
          type="date"
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={payDate}
          onChange={(e) => setPayDate(e.target.value)}
          required
        />
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={busy}
        className="mt-3 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {busy ? 'Creating…' : 'Create'}
      </button>
    </form>
  )
}

function PeriodDetail({
  period,
  onChanged,
}: {
  period: payrollApi.PayrollPeriod
  onChanged: () => void
}) {
  const { hasPermission } = useAuth()
  const [results, setResults] = useState<payrollApi.PayrollEmployeeResult[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const canCalculate = hasPermission('payroll.calculate')
  const canApprove = hasPermission('payroll.approve')
  const canPost = hasPermission('payroll.post')
  const canReverse = hasPermission('payroll.reverse')

  useEffect(() => {
    if (
      period.status === 'CALCULATED' ||
      period.status === 'APPROVED' ||
      period.status === 'POSTED'
    ) {
      payrollApi
        .listPayrollEmployeeResults(period.id)
        .then(setResults)
        .catch(() => {
          /* supplementary */
        })
    } else {
      // oxlint-disable-next-line react/set-state-in-effect
      setResults([])
    }
  }, [period.id, period.status])

  async function run(action: () => Promise<unknown>, failureMessage: string) {
    setError(null)
    setBusy(true)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : failureMessage)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Period #{period.id}</h2>
          <p className="mt-1 text-xs text-gray-500">
            {period.period_start} .. {period.period_end} · pay date {period.pay_date}
          </p>
        </div>
        <StatusBadge status={period.status} />
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs text-gray-500">Gross</dt>
          <dd className="font-medium">{period.total_gross}</dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">Deductions</dt>
          <dd className="font-medium">{period.total_deductions}</dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">Employer contrib.</dt>
          <dd className="font-medium">{period.total_employer_contributions}</dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">Net pay</dt>
          <dd className="font-medium">{period.total_net_pay}</dd>
        </div>
      </dl>

      {results.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold text-gray-700">Employee results</h3>
          <table className="mt-1 w-full text-left text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-gray-500">
                <th className="py-1 pr-3">Employee</th>
                <th className="py-1 pr-3">Gross</th>
                <th className="py-1 pr-3">Net</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r) => (
                <tr key={r.id} className="border-b border-gray-50">
                  <td className="py-1 pr-3">#{r.employee_id}</td>
                  <td className="py-1 pr-3">{r.gross_pay}</td>
                  <td className="py-1 pr-3">{r.net_pay}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}

      <div className="mt-4 flex flex-wrap gap-2">
        {period.status === 'DRAFT' && canCalculate && (
          <button
            disabled={busy}
            onClick={() =>
              run(() => payrollApi.openPayrollPeriod(period.id), 'Could not open period.')
            }
            className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            Open
          </button>
        )}
        {(period.status === 'OPEN' || period.status === 'CALCULATED') && canCalculate && (
          <button
            disabled={busy}
            onClick={() =>
              run(
                () => payrollApi.calculatePayrollPeriod(period.id, crypto.randomUUID()),
                'Could not calculate period.',
              )
            }
            className="rounded bg-purple-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-purple-700 disabled:opacity-50"
          >
            {period.status === 'CALCULATED' ? 'Recalculate' : 'Calculate'}
          </button>
        )}
        {period.status === 'CALCULATED' && canApprove && (
          <button
            disabled={busy}
            onClick={() =>
              run(() => payrollApi.approvePayrollPeriod(period.id), 'Could not approve period.')
            }
            className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
          >
            Approve
          </button>
        )}
        {period.status === 'APPROVED' && canPost && (
          <button
            disabled={busy}
            onClick={() =>
              run(
                () => payrollApi.postPayrollPeriod(period.id, crypto.randomUUID()),
                'Could not post period.',
              )
            }
            className="rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            Post
          </button>
        )}
        {period.status === 'POSTED' && canReverse && (
          <button
            disabled={busy}
            onClick={() =>
              run(
                () => payrollApi.reversePayrollPeriod(period.id, 'Reversed via UI'),
                'Could not reverse period.',
              )
            }
            className="rounded border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            Reverse
          </button>
        )}
        {['DRAFT', 'OPEN', 'CALCULATED', 'APPROVED'].includes(period.status) && canCalculate && (
          <button
            disabled={busy}
            onClick={() =>
              run(
                () => payrollApi.cancelPayrollPeriod(period.id, 'Cancelled via UI'),
                'Could not cancel period.',
              )
            }
            className="rounded border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}

export function PayrollPage() {
  const { user, hasPermission } = useAuth()
  const storeId = user?.store_id ?? null
  const [periods, setPeriods] = useState<payrollApi.PayrollPeriod[]>([])
  const [selected, setSelected] = useState<payrollApi.PayrollPeriod | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const canCalculate = hasPermission('payroll.calculate')

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await payrollApi.listPayrollPeriods()
      setPeriods(data)
      if (selected) {
        const refreshed = data.find((p) => p.id === selected.id)
        setSelected(refreshed ?? null)
      }
    } catch {
      setError('Could not load payroll periods.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Payroll</h1>

      {canCalculate && storeId != null && (
        <div className="mt-4">
          <CreatePeriodForm storeId={storeId} onCreated={load} />
        </div>
      )}

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-1">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="py-2 pr-4">Period</th>
                  <th className="py-2 pr-4">Status</th>
                </tr>
              </thead>
              <tbody>
                {periods.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => setSelected(p)}
                    className={`cursor-pointer border-b border-gray-100 hover:bg-gray-50 ${
                      selected?.id === p.id ? 'bg-blue-50' : ''
                    }`}
                  >
                    <td className="py-2 pr-4">
                      #{p.id} · {p.period_start}..{p.period_end}
                    </td>
                    <td className="py-2 pr-4">
                      <StatusBadge status={p.status} />
                    </td>
                  </tr>
                ))}
                {periods.length === 0 && (
                  <tr>
                    <td colSpan={2} className="py-6 text-center text-gray-400">
                      No payroll periods yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="lg:col-span-2">
            {selected ? (
              <PeriodDetail period={selected} onChanged={load} />
            ) : (
              <p className="text-sm text-gray-500">Select a period to view its details.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
