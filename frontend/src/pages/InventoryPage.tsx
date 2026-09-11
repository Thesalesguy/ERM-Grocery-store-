import { useEffect, useState } from 'react'
import * as inventoryApi from '../api/inventory'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const REASON_CODES = ['DAMAGE', 'THEFT', 'EXPIRY', 'STOCKTAKE_CORRECTION', 'OTHER'] as const

function AdjustmentForm({ onDone }: { onDone: () => void }) {
  const [productId, setProductId] = useState('')
  const [delta, setDelta] = useState('')
  const [reason, setReason] = useState<(typeof REASON_CODES)[number]>('STOCKTAKE_CORRECTION')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    if (!window.confirm(`Adjust stock for product #${productId} by ${delta}?`)) return
    setIsSubmitting(true)
    try {
      await inventoryApi.createAdjustment({
        product_id: Number(productId),
        quantity_delta: delta,
        reason_code: reason,
        notes: notes || undefined,
      })
      setProductId('')
      setDelta('')
      setNotes('')
      onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Adjustment failed.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">Stock adjustment</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
        <input
          placeholder="Product ID"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={productId}
          onChange={(e) => setProductId(e.target.value)}
          required
        />
        <input
          placeholder="Delta (e.g. -2 or 5)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={delta}
          onChange={(e) => setDelta(e.target.value)}
          required
        />
        <select
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={reason}
          onChange={(e) => setReason(e.target.value as (typeof REASON_CODES)[number])}
        >
          {REASON_CODES.map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
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
        {isSubmitting ? 'Saving…' : 'Apply adjustment'}
      </button>
    </form>
  )
}

export function InventoryPage() {
  const { user, hasPermission } = useAuth()
  const [stock, setStock] = useState<inventoryApi.StockLevel[]>([])
  const [lowStockOnly, setLowStockOnly] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await inventoryApi.listStock({
        store_id: user?.store_id ?? undefined,
        low_stock_only: lowStockOnly || undefined,
      })
      setStock(data)
    } catch {
      setError('Could not load stock levels.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lowStockOnly])

  const canAdjust = hasPermission('inventory.adjust')

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Inventory</h1>

      {canAdjust && <div className="mt-4">{<AdjustmentForm onDone={load} />}</div>}

      <label className="mt-2 flex items-center gap-2 text-sm text-gray-700">
        <input
          type="checkbox"
          checked={lowStockOnly}
          onChange={(e) => setLowStockOnly(e.target.checked)}
        />
        Low stock only
      </label>

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-500">
              <th className="py-2 pr-4">ID</th>
              <th className="py-2 pr-4">SKU</th>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">On hand</th>
              <th className="py-2 pr-4">WAC</th>
              <th className="py-2 pr-4"></th>
            </tr>
          </thead>
          <tbody>
            {stock.map((s) => (
              <tr key={s.id} className="border-b border-gray-100">
                <td className="py-2 pr-4 text-gray-400">{s.id}</td>
                <td className="py-2 pr-4 font-mono text-xs">{s.sku}</td>
                <td className="py-2 pr-4">{s.name}</td>
                <td className="py-2 pr-4">{s.current_qty_on_hand}</td>
                <td className="py-2 pr-4">{s.current_cost}</td>
                <td className="py-2 pr-4">
                  {s.is_low_stock && (
                    <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                      Low stock
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {stock.length === 0 && (
              <tr>
                <td colSpan={6} className="py-6 text-center text-gray-400">
                  No products found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
