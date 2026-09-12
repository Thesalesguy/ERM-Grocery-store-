import { useEffect, useState } from 'react'
import * as stockCountsApi from '../api/stockCounts'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

function CreateCountForm({ onCreated }: { onCreated: (count: stockCountsApi.StockCount) => void }) {
  const { user } = useAuth()
  const [storeId, setStoreId] = useState(user?.store_id?.toString() ?? '')
  const [categoryId, setCategoryId] = useState('')
  const [productIds, setProductIds] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)
    try {
      const count = await stockCountsApi.createStockCount({
        store_id: Number(storeId),
        category_id: categoryId ? Number(categoryId) : undefined,
        product_ids: productIds
          ? productIds
              .split(',')
              .map((s) => Number(s.trim()))
              .filter((n) => !Number.isNaN(n))
          : undefined,
        notes: notes || undefined,
      })
      setCategoryId('')
      setProductIds('')
      setNotes('')
      onCreated(count)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create stock count.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">New stock count</h2>
      <p className="mt-1 text-xs text-gray-500">
        Scope by category, an explicit product list, or both — at least one product must be in
        scope.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
        <input
          placeholder="Store ID"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={storeId}
          onChange={(e) => setStoreId(e.target.value)}
          required
        />
        <input
          placeholder="Category ID (optional)"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={categoryId}
          onChange={(e) => setCategoryId(e.target.value)}
        />
        <input
          placeholder="Product IDs, comma-separated"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={productIds}
          onChange={(e) => setProductIds(e.target.value)}
        />
        <input
          placeholder="Notes (optional)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={isSubmitting}
        className="mt-3 rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {isSubmitting ? 'Creating…' : 'Create count'}
      </button>
    </form>
  )
}

function CountEntryRow({
  line,
  countId,
  disabled,
  onCounted,
}: {
  line: stockCountsApi.StockCountLine
  countId: number
  disabled: boolean
  onCounted: () => void
}) {
  const [quantity, setQuantity] = useState(line.counted_quantity ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    setError(null)
    setBusy(true)
    try {
      await stockCountsApi.recordCountEntry(countId, {
        product_id: line.product_id,
        counted_quantity: quantity,
      })
      onCounted()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not record count.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <tr className="border-b border-gray-100">
      <td className="py-2 pr-4 text-gray-400">{line.product_id}</td>
      <td className="py-2 pr-4">{line.expected_quantity ?? '—'}</td>
      <td className="py-2 pr-4">
        <input
          className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          disabled={disabled}
        />
      </td>
      <td className="py-2 pr-4">{line.variance_quantity ?? '—'}</td>
      <td className="py-2 pr-4 text-gray-400">{line.recount_number}</td>
      <td className="py-2 pr-4">
        {!disabled && (
          <button
            onClick={submit}
            disabled={busy || !quantity}
            className="rounded bg-gray-800 px-2 py-1 text-xs font-medium text-white hover:bg-gray-900 disabled:opacity-50"
          >
            {busy ? 'Saving…' : line.counted_quantity !== null ? 'Recount' : 'Count'}
          </button>
        )}
        {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
      </td>
    </tr>
  )
}

function CountDetail({
  count,
  onChanged,
}: {
  count: stockCountsApi.StockCountWithLines
  onChanged: () => void
}) {
  const { hasPermission } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const canWrite = hasPermission('inventory.count.write')
  const canReview = hasPermission('inventory.count.review')
  const canPost = hasPermission('inventory.count.post')

  async function runAction(action: () => Promise<unknown>) {
    setError(null)
    setBusy(true)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Action failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">{count.count_number}</h2>
          <p className="text-xs text-gray-500">
            Store {count.store_id} · <span className="font-medium uppercase">{count.status}</span>
          </p>
        </div>
        <div className="flex gap-2">
          {count.status === 'DRAFT' && canWrite && (
            <button
              disabled={busy}
              onClick={() => runAction(() => stockCountsApi.openStockCount(count.id))}
              className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              Open
            </button>
          )}
          {count.status === 'OPEN' && canWrite && (
            <button
              disabled={busy}
              onClick={() => runAction(() => stockCountsApi.markStockCountCounted(count.id))}
              className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              Mark counted
            </button>
          )}
          {count.status === 'COUNTED' && canReview && (
            <button
              disabled={busy}
              onClick={() => runAction(() => stockCountsApi.reviewStockCount(count.id))}
              className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              Review
            </button>
          )}
          {count.status === 'REVIEWED' && canReview && (
            <button
              disabled={busy}
              onClick={() =>
                runAction(() => stockCountsApi.reopenStockCount(count.id, 'Reopened for recount'))
              }
              className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
            >
              Reopen for recount
            </button>
          )}
          {count.status === 'REVIEWED' && canPost && (
            <button
              disabled={busy}
              onClick={() => runAction(() => stockCountsApi.postStockCount(count.id))}
              className="rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
            >
              Post
            </button>
          )}
          {!['POSTED', 'CANCELLED'].includes(count.status) && canWrite && (
            <button
              disabled={busy}
              onClick={() =>
                runAction(() => stockCountsApi.cancelStockCount(count.id, 'Cancelled by user'))
              }
              className="rounded border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Product</th>
            <th className="py-2 pr-4">Expected</th>
            <th className="py-2 pr-4">Counted</th>
            <th className="py-2 pr-4">Variance</th>
            <th className="py-2 pr-4">Recounts</th>
            <th className="py-2 pr-4"></th>
          </tr>
        </thead>
        <tbody>
          {count.lines.map((line) => (
            <CountEntryRow
              key={line.id}
              line={line}
              countId={count.id}
              disabled={!canWrite || !['OPEN', 'COUNTED'].includes(count.status)}
              onCounted={onChanged}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function StockCountsPage() {
  const { user } = useAuth()
  const [counts, setCounts] = useState<stockCountsApi.StockCount[]>([])
  const [selected, setSelected] = useState<stockCountsApi.StockCountWithLines | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await stockCountsApi.listStockCounts({
        store_id: user?.store_id ?? undefined,
      })
      setCounts(data)
    } catch {
      setError('Could not load stock counts.')
    } finally {
      setLoading(false)
    }
  }

  async function loadSelected(id: number) {
    const detail = await stockCountsApi.getStockCount(id)
    setSelected(detail)
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function refreshAll() {
    await load()
    if (selected) await loadSelected(selected.id)
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Stock Counts</h1>

      <CreateCountForm
        onCreated={(count) => {
          load()
          loadSelected(count.id)
        }}
      />

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-1">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="py-2 pr-4">Count</th>
                  <th className="py-2 pr-4">Status</th>
                </tr>
              </thead>
              <tbody>
                {counts.map((c) => (
                  <tr
                    key={c.id}
                    onClick={() => loadSelected(c.id)}
                    className={`cursor-pointer border-b border-gray-100 hover:bg-gray-50 ${
                      selected?.id === c.id ? 'bg-blue-50' : ''
                    }`}
                  >
                    <td className="py-2 pr-4 font-mono text-xs">{c.count_number}</td>
                    <td className="py-2 pr-4 text-xs uppercase">{c.status}</td>
                  </tr>
                ))}
                {counts.length === 0 && (
                  <tr>
                    <td colSpan={2} className="py-6 text-center text-gray-400">
                      No stock counts yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="lg:col-span-2">
            {selected ? (
              <CountDetail count={selected} onChanged={refreshAll} />
            ) : (
              <p className="text-sm text-gray-500">Select a count to view its lines.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
