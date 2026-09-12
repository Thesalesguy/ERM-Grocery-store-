import { useEffect, useState } from 'react'
import * as replenishmentApi from '../api/replenishment'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const STATUS_STYLES: Record<replenishmentApi.ReplenishmentPlanStatus, string> = {
  RECOMMENDED: 'bg-gray-100 text-gray-700',
  APPROVED: 'bg-blue-100 text-blue-700',
  EXECUTED: 'bg-green-100 text-green-700',
  STALE: 'bg-amber-100 text-amber-700',
  CANCELLED: 'bg-red-100 text-red-700',
}

function StatusBadge({ status }: { status: replenishmentApi.ReplenishmentPlanStatus }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium uppercase ${STATUS_STYLES[status]}`}>
      {status}
    </span>
  )
}

function MetricsBar({ storeId }: { storeId: number | null }) {
  const [metrics, setMetrics] = useState<replenishmentApi.SupplyChainMetrics | null>(null)

  useEffect(() => {
    let cancelled = false
    replenishmentApi
      .getSupplyChainMetrics(storeId ? { store_id: storeId } : {})
      .then((data) => {
        if (!cancelled) setMetrics(data)
      })
      .catch(() => {
        /* metrics are supplementary — a failure here shouldn't block the page */
      })
    return () => {
      cancelled = true
    }
  }, [storeId])

  if (!metrics) return null

  return (
    <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-5">
      <div className="rounded border border-gray-200 bg-white p-3">
        <p className="text-xs text-gray-500">Below reorder point</p>
        <p className="text-lg font-semibold text-gray-900">
          {metrics.products_below_reorder_point}
        </p>
      </div>
      <div className="rounded border border-gray-200 bg-white p-3">
        <p className="text-xs text-gray-500">Urgent (below minimum)</p>
        <p className="text-lg font-semibold text-gray-900">{metrics.products_below_minimum}</p>
      </div>
      <div className="rounded border border-gray-200 bg-white p-3">
        <p className="text-xs text-gray-500">Open PO qty</p>
        <p className="text-lg font-semibold text-gray-900">{metrics.open_purchase_order_qty}</p>
      </div>
      <div className="rounded border border-gray-200 bg-white p-3">
        <p className="text-xs text-gray-500">Inbound transfer qty</p>
        <p className="text-lg font-semibold text-gray-900">{metrics.inbound_transfer_qty}</p>
      </div>
      <div className="rounded border border-gray-200 bg-white p-3">
        <p className="text-xs text-gray-500">Overdue POs</p>
        <p className="text-lg font-semibold text-gray-900">
          {metrics.overdue_purchase_order_count}
        </p>
      </div>
    </div>
  )
}

function PlanDetail({
  plan,
  onChanged,
}: {
  plan: replenishmentApi.ReplenishmentPlanDetail
  onChanged: () => void
}) {
  const { hasPermission } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const canApprove = hasPermission('supply_chain.approve')
  const canExecute = hasPermission('supply_chain.execute')
  const canPlan = hasPermission('supply_chain.plan')

  async function handleApprove() {
    setError(null)
    setBusy(true)
    try {
      await replenishmentApi.approveReplenishmentPlan(plan.id)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not approve plan.')
    } finally {
      setBusy(false)
    }
  }

  async function handleExecute() {
    setError(null)
    setBusy(true)
    try {
      await replenishmentApi.executeReplenishmentPlan(plan.id, crypto.randomUUID())
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not execute plan.')
    } finally {
      setBusy(false)
    }
  }

  async function handleCancel() {
    setError(null)
    setBusy(true)
    try {
      await replenishmentApi.cancelReplenishmentPlan(plan.id, 'Cancelled by user')
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not cancel plan.')
    } finally {
      setBusy(false)
    }
  }

  const cancellable = ['RECOMMENDED', 'APPROVED', 'STALE'].includes(plan.status)

  return (
    <div className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Plan #{plan.id}</h2>
          <p className="mt-1 text-xs text-gray-500">
            Product {plan.product_id} · Store {plan.destination_store_id} ·{' '}
            {plan.source_type === 'SUPPLIER'
              ? `Supplier ${plan.supplier_id}`
              : `Transfer from store ${plan.source_store_id}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={plan.status} />
          {plan.urgency === 'URGENT' && (
            <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
              URGENT
            </span>
          )}
        </div>
      </div>

      <p className="mt-3 text-sm text-gray-700">{plan.reason}</p>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-gray-500">Needed</dt>
          <dd className="font-medium">{plan.needed_quantity}</dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">Suggested</dt>
          <dd className="font-medium">{plan.suggested_quantity}</dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">Remaining need</dt>
          <dd className="font-medium">{plan.remaining_need}</dd>
        </div>
        {plan.executed_quantity && (
          <div>
            <dt className="text-xs text-gray-500">Executed</dt>
            <dd className="font-medium">{plan.executed_quantity}</dd>
          </div>
        )}
        {plan.fulfilled !== null && (
          <div>
            <dt className="text-xs text-gray-500">Fulfilled</dt>
            <dd className="font-medium">{plan.fulfilled ? 'Yes' : 'Not yet'}</dd>
          </div>
        )}
        {plan.generated_purchase_order_id && (
          <div>
            <dt className="text-xs text-gray-500">Purchase order</dt>
            <dd className="font-medium">
              <a
                className="text-blue-600 hover:underline"
                href={`/purchasing?purchase_order_id=${plan.generated_purchase_order_id}`}
              >
                PO #{plan.generated_purchase_order_id}
              </a>
            </dd>
          </div>
        )}
        {plan.generated_transfer_id && (
          <div>
            <dt className="text-xs text-gray-500">Transfer</dt>
            <dd className="font-medium">
              <a
                className="text-blue-600 hover:underline"
                href={`/transfers?transfer_id=${plan.generated_transfer_id}`}
              >
                Transfer #{plan.generated_transfer_id}
              </a>
            </dd>
          </div>
        )}
      </dl>

      {plan.status === 'STALE' && plan.stale_reason && (
        <p className="mt-3 rounded bg-amber-50 p-2 text-xs text-amber-800">{plan.stale_reason}</p>
      )}

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}

      <div className="mt-4 flex gap-2">
        {plan.status === 'RECOMMENDED' && canApprove && (
          <button
            disabled={busy}
            onClick={handleApprove}
            className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {busy ? 'Approving…' : 'Approve'}
          </button>
        )}
        {plan.status === 'APPROVED' && canExecute && (
          <button
            disabled={busy}
            onClick={handleExecute}
            className="rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            {busy ? 'Executing…' : 'Execute'}
          </button>
        )}
        {cancellable && canPlan && (
          <button
            disabled={busy}
            onClick={handleCancel}
            className="rounded border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}

function ExceptionsList({ storeId }: { storeId: number | null }) {
  const [exceptions, setExceptions] = useState<replenishmentApi.SupplyChainException[]>([])

  useEffect(() => {
    let cancelled = false
    replenishmentApi
      .getSupplyChainExceptions(storeId ? { store_id: storeId } : {})
      .then((data) => {
        if (!cancelled) setExceptions(data)
      })
      .catch(() => {
        /* supplementary — non-fatal */
      })
    return () => {
      cancelled = true
    }
  }, [storeId])

  if (exceptions.length === 0) return null

  return (
    <div className="mt-6 rounded border border-amber-200 bg-amber-50 p-4">
      <h2 className="text-sm font-semibold text-amber-900">Exceptions</h2>
      <ul className="mt-2 space-y-1 text-xs text-amber-800">
        {exceptions.map((exc, i) => (
          <li key={i}>
            <span className="font-mono uppercase">{exc.code}</span> — {exc.message}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function SupplyChainPage() {
  const { user, hasPermission } = useAuth()
  const storeId = user?.store_id ?? null
  const [plans, setPlans] = useState<replenishmentApi.ReplenishmentPlan[]>([])
  const [selected, setSelected] = useState<replenishmentApi.ReplenishmentPlanDetail | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)

  const canPlan = hasPermission('supply_chain.plan')

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await replenishmentApi.listReplenishmentPlans({
        store_id: storeId ?? undefined,
        status_filter: statusFilter || undefined,
      })
      setPlans(data)
    } catch {
      setError('Could not load replenishment plans.')
    } finally {
      setLoading(false)
    }
  }

  async function loadSelected(id: number) {
    const detail = await replenishmentApi.getReplenishmentPlan(id)
    setSelected(detail)
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter])

  async function refreshAll() {
    await load()
    if (selected) await loadSelected(selected.id)
  }

  async function handleGenerate() {
    setError(null)
    setGenerating(true)
    try {
      await replenishmentApi.generateReplenishmentPlans({ store_id: storeId ?? undefined })
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not generate recommendations.')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-900">Supply Chain</h1>
        {canPlan && (
          <button
            disabled={generating}
            onClick={handleGenerate}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {generating ? 'Generating…' : 'Generate recommendations'}
          </button>
        )}
      </div>

      <MetricsBar storeId={storeId} />
      <ExceptionsList storeId={storeId} />

      <div className="mt-6 mb-3 flex items-center gap-2">
        <label className="text-xs text-gray-500" htmlFor="status-filter">
          Status
        </label>
        <select
          id="status-filter"
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">All</option>
          <option value="RECOMMENDED">Recommended</option>
          <option value="APPROVED">Approved</option>
          <option value="EXECUTED">Executed</option>
          <option value="STALE">Stale</option>
          <option value="CANCELLED">Cancelled</option>
        </select>
      </div>

      {loading && <p className="text-gray-500">Loading…</p>}
      {error && <p className="text-red-600">{error}</p>}

      {!loading && !error && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-1">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="py-2 pr-4">Plan</th>
                  <th className="py-2 pr-4">Status</th>
                </tr>
              </thead>
              <tbody>
                {plans.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => loadSelected(p.id)}
                    className={`cursor-pointer border-b border-gray-100 hover:bg-gray-50 ${
                      selected?.id === p.id ? 'bg-blue-50' : ''
                    }`}
                  >
                    <td className="py-2 pr-4">
                      #{p.id} · product {p.product_id}
                    </td>
                    <td className="py-2 pr-4">
                      <StatusBadge status={p.status} />
                    </td>
                  </tr>
                ))}
                {plans.length === 0 && (
                  <tr>
                    <td colSpan={2} className="py-6 text-center text-gray-400">
                      No replenishment plans yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="lg:col-span-2">
            {selected ? (
              <PlanDetail plan={selected} onChanged={refreshAll} />
            ) : (
              <p className="text-sm text-gray-500">Select a plan to view its details.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
