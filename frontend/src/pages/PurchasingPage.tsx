import { useEffect, useRef, useState } from 'react'
import * as productsApi from '../api/products'
import * as purchasingApi from '../api/purchasing'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

function NewPurchaseOrderForm({
  storeId,
  onCreated,
  onCancel,
}: {
  storeId: number
  onCreated: (po: purchasingApi.PurchaseOrder) => void
  onCancel: () => void
}) {
  const [suppliers, setSuppliers] = useState<purchasingApi.Supplier[]>([])
  const [supplierId, setSupplierId] = useState<number | null>(null)
  const [orderDate, setOrderDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [lines, setLines] = useState<{ sku: string; quantity: string; unitCost: string }[]>([
    { sku: '', quantity: '', unitCost: '' },
  ])
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  useEffect(() => {
    purchasingApi.listSuppliers({ is_active: true }).then((list) => {
      setSuppliers(list)
      if (list.length > 0) setSupplierId(list[0].id)
    })
  }, [])

  function updateLine(index: number, field: 'sku' | 'quantity' | 'unitCost', value: string) {
    setLines((prev) => prev.map((l, i) => (i === index ? { ...l, [field]: value } : l)))
  }

  function addLine() {
    setLines((prev) => [...prev, { sku: '', quantity: '', unitCost: '' }])
  }

  function removeLine(index: number) {
    setLines((prev) => prev.filter((_, i) => i !== index))
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (supplierId === null) return
    setError(null)
    setIsSubmitting(true)
    try {
      // Resolve each line's SKU to a product ID server-side is not
      // available directly, so look products up by search first — kept
      // simple (exact SKU match via the existing search) rather than a
      // separate product-picker component for this first purchasing UI.
      const resolvedLines: purchasingApi.PurchaseOrderItemInput[] = []
      for (const line of lines) {
        if (!line.sku.trim()) continue
        const matches = await productsApi.listProducts({ store_id: storeId, search: line.sku })
        const product = matches.find((p) => p.sku.toLowerCase() === line.sku.trim().toLowerCase())
        if (!product) {
          throw new ApiError(404, 'PRODUCT_NOT_FOUND', `No product found with SKU "${line.sku}"`)
        }
        resolvedLines.push({
          product_id: product.id,
          quantity_ordered: line.quantity,
          unit_cost: line.unitCost,
        })
      }
      if (resolvedLines.length === 0) {
        throw new ApiError(422, 'EMPTY_PURCHASE_ORDER', 'Add at least one line.')
      }
      const po = await purchasingApi.createPurchaseOrder({
        store_id: storeId,
        supplier_id: supplierId,
        order_date: orderDate,
        lines: resolvedLines,
      })
      onCreated(po)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create purchase order.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">New purchase order</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <select
          value={supplierId ?? ''}
          onChange={(e) => setSupplierId(Number(e.target.value))}
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          required
        >
          <option value="" disabled>
            Select supplier…
          </option>
          {suppliers.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <input
          type="date"
          value={orderDate}
          onChange={(e) => setOrderDate(e.target.value)}
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          required
        />
      </div>

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="text-gray-500">
            <th className="py-1 pr-2">SKU</th>
            <th className="py-1 pr-2">Quantity</th>
            <th className="py-1 pr-2">Unit cost</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {lines.map((line, index) => (
            <tr key={index}>
              <td className="py-1 pr-2">
                <input
                  value={line.sku}
                  onChange={(e) => updateLine(index, 'sku', e.target.value)}
                  className="w-32 rounded border border-gray-300 px-2 py-1"
                  placeholder="SKU"
                />
              </td>
              <td className="py-1 pr-2">
                <input
                  value={line.quantity}
                  onChange={(e) => updateLine(index, 'quantity', e.target.value)}
                  className="w-24 rounded border border-gray-300 px-2 py-1"
                  placeholder="Qty"
                />
              </td>
              <td className="py-1 pr-2">
                <input
                  value={line.unitCost}
                  onChange={(e) => updateLine(index, 'unitCost', e.target.value)}
                  className="w-24 rounded border border-gray-300 px-2 py-1"
                  placeholder="Cost"
                />
              </td>
              <td className="py-1">
                {lines.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeLine(index)}
                    className="text-xs text-red-600 hover:underline"
                  >
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        type="button"
        onClick={addLine}
        className="mt-2 text-xs text-blue-600 hover:underline"
      >
        + Add line
      </button>

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <div className="mt-3 flex gap-2">
        <button
          type="submit"
          disabled={isSubmitting || supplierId === null}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {isSubmitting ? 'Saving…' : 'Save as draft'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

function ReceiveGoodsPanel({
  po,
  onReceived,
}: {
  po: purchasingApi.PurchaseOrder
  onReceived: () => void
}) {
  const [receiving, setReceiving] = useState<Record<number, { quantity: string; cost: string }>>({})
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  // One idempotency key per receiving attempt (M2/M3 hardening pattern —
  // see PosPage.tsx): stable across retries of the SAME attempt, reset
  // only after a successful receipt.
  const idempotencyKeyRef = useRef<string | null>(null)

  const receivableItems = po.items.filter((item) => Number(item.quantity_remaining) > 0)

  function updateReceiving(itemId: number, field: 'quantity' | 'cost', value: string) {
    setReceiving((prev) => ({
      ...prev,
      [itemId]: {
        quantity: prev[itemId]?.quantity ?? '',
        cost: prev[itemId]?.cost ?? '',
        [field]: value,
      },
    }))
  }

  const lines = receivableItems
    .map((item) => ({ item, entry: receiving[item.id] }))
    .filter(({ entry }) => entry && entry.quantity.trim() !== '')

  const total = lines.reduce((sum, { entry }) => {
    const qty = Number(entry!.quantity) || 0
    const cost = Number(entry!.cost) || 0
    return sum + qty * cost
  }, 0)

  async function handleReceive() {
    if (lines.length === 0) return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setError(null)
    setIsSubmitting(true)
    try {
      await purchasingApi.receiveGoods(po.id, {
        received_date: new Date().toISOString().slice(0, 10),
        client_transaction_id: idempotencyKeyRef.current,
        lines: lines.map(({ item, entry }) => ({
          purchase_order_item_id: item.id,
          quantity_received: entry!.quantity,
          unit_cost: entry!.cost || item.unit_cost,
        })),
      })
      idempotencyKeyRef.current = null
      setReceiving({})
      onReceived()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to record the receipt.')
    } finally {
      setIsSubmitting(false)
    }
  }

  if (receivableItems.length === 0) {
    return null
  }

  return (
    <div className="mt-4 rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Receive goods</h2>
      <table className="mt-2 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Product</th>
            <th className="py-2 pr-4">Ordered</th>
            <th className="py-2 pr-4">Received</th>
            <th className="py-2 pr-4">Remaining</th>
            <th className="py-2 pr-4">Receiving now</th>
            <th className="py-2 pr-4">Unit cost</th>
          </tr>
        </thead>
        <tbody>
          {receivableItems.map((item) => (
            <tr key={item.id} className="border-b border-gray-100">
              <td className="py-2 pr-4">
                {item.product_name}
                <div className="font-mono text-xs text-gray-400">{item.product_sku}</div>
              </td>
              <td className="py-2 pr-4">{item.quantity_ordered}</td>
              <td className="py-2 pr-4">{item.quantity_received}</td>
              <td className="py-2 pr-4 font-medium">{item.quantity_remaining}</td>
              <td className="py-2 pr-4">
                <input
                  value={receiving[item.id]?.quantity ?? ''}
                  onChange={(e) => updateReceiving(item.id, 'quantity', e.target.value)}
                  placeholder="0"
                  className="w-20 rounded border border-gray-300 px-2 py-1"
                />
              </td>
              <td className="py-2 pr-4">
                <input
                  value={receiving[item.id]?.cost ?? ''}
                  onChange={(e) => updateReceiving(item.id, 'cost', e.target.value)}
                  placeholder={item.unit_cost}
                  className="w-24 rounded border border-gray-300 px-2 py-1"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-2 flex items-center justify-between">
        <p className="text-xs text-gray-500">
          Resulting receipt total:{' '}
          <span className="font-medium text-gray-900">{total.toFixed(2)}</span>
        </p>
      </div>
      {error && (
        <p role="alert" className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      <button
        onClick={handleReceive}
        disabled={isSubmitting || lines.length === 0}
        className="mt-3 rounded bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {isSubmitting ? 'Recording…' : 'Confirm receipt'}
      </button>
    </div>
  )
}

function PurchaseOrderDetail({
  poId,
  onBack,
  canReceive,
  canWrite,
}: {
  poId: number
  onBack: () => void
  canReceive: boolean
  canWrite: boolean
}) {
  const [po, setPo] = useState<purchasingApi.PurchaseOrder | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    try {
      setPo(await purchasingApi.getPurchaseOrder(poId))
    } catch {
      setError('Could not load this purchase order.')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poId])

  if (error) return <p className="text-red-600">{error}</p>
  if (!po) return <p className="text-gray-500">Loading…</p>

  const canSubmit = canWrite && po.status === 'DRAFT'
  const canCancel = canWrite && ['DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED'].includes(po.status)
  const canReceiveNow = canReceive && ['ORDERED', 'PARTIALLY_RECEIVED'].includes(po.status)

  return (
    <div>
      <button onClick={onBack} className="text-sm text-blue-600 hover:underline">
        ← Back to purchase orders
      </button>
      <div className="mt-2 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">{po.purchase_number}</h1>
          <p className="text-sm text-gray-500">
            {po.supplier_name} · Ordered {po.order_date}
          </p>
        </div>
        <span className="rounded bg-gray-100 px-2 py-1 text-xs font-medium text-gray-700">
          {po.status}
        </span>
      </div>

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Product</th>
            <th className="py-2 pr-4">Ordered</th>
            <th className="py-2 pr-4">Received</th>
            <th className="py-2 pr-4">Remaining</th>
            <th className="py-2 pr-4">Unit cost</th>
          </tr>
        </thead>
        <tbody>
          {po.items.map((item) => (
            <tr key={item.id} className="border-b border-gray-100">
              <td className="py-2 pr-4">
                {item.product_name}
                <div className="font-mono text-xs text-gray-400">{item.product_sku}</div>
              </td>
              <td className="py-2 pr-4">{item.quantity_ordered}</td>
              <td className="py-2 pr-4">
                {item.quantity_received}
                {item.is_over_received && (
                  <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                    over-received
                  </span>
                )}
              </td>
              <td className="py-2 pr-4">{item.quantity_remaining}</td>
              <td className="py-2 pr-4">{item.unit_cost}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-4 flex gap-2">
        {canSubmit && (
          <button
            onClick={async () => {
              await purchasingApi.submitPurchaseOrder(po.id)
              load()
            }}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
          >
            Submit to supplier
          </button>
        )}
        {canCancel && (
          <button
            onClick={async () => {
              if (!window.confirm('Cancel this purchase order?')) return
              await purchasingApi.cancelPurchaseOrder(po.id)
              load()
            }}
            className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50"
          >
            Cancel order
          </button>
        )}
      </div>

      {canReceiveNow && <ReceiveGoodsPanel po={po} onReceived={load} />}
    </div>
  )
}

export function PurchasingPage() {
  const { user, hasPermission } = useAuth()
  const [orders, setOrders] = useState<purchasingApi.PurchaseOrder[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [selectedPoId, setSelectedPoId] = useState<number | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setOrders(await purchasingApi.listPurchaseOrders())
    } catch {
      setError('Could not load purchase orders.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // Intentional: load the list once on mount, the same pattern
    // InventoryPage/ProductsPage use for their initial fetch.
    // oxlint-disable-next-line react/set-state-in-effect
    load()
  }, [])

  const canWrite = hasPermission('purchasing.write')
  const canReceive = hasPermission('purchasing.receive')

  if (selectedPoId !== null) {
    return (
      <PurchaseOrderDetail
        poId={selectedPoId}
        onBack={() => {
          setSelectedPoId(null)
          load()
        }}
        canReceive={canReceive}
        canWrite={canWrite}
      />
    )
  }

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-900">Purchasing</h1>
        {canWrite && !showForm && user?.store_id != null && (
          <button
            onClick={() => setShowForm(true)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
          >
            New purchase order
          </button>
        )}
      </div>

      {showForm && user?.store_id != null && (
        <NewPurchaseOrderForm
          storeId={user.store_id}
          onCreated={(po) => {
            setShowForm(false)
            setSelectedPoId(po.id)
          }}
          onCancel={() => setShowForm(false)}
        />
      )}

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-500">
              <th className="py-2 pr-4">PO Number</th>
              <th className="py-2 pr-4">Supplier</th>
              <th className="py-2 pr-4">Order date</th>
              <th className="py-2 pr-4">Status</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((po) => (
              <tr
                key={po.id}
                onClick={() => setSelectedPoId(po.id)}
                className="cursor-pointer border-b border-gray-100 hover:bg-gray-50"
              >
                <td className="py-2 pr-4 font-mono text-xs">{po.purchase_number}</td>
                <td className="py-2 pr-4">{po.supplier_name}</td>
                <td className="py-2 pr-4">{po.order_date}</td>
                <td className="py-2 pr-4">
                  <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-700">
                    {po.status}
                  </span>
                </td>
              </tr>
            ))}
            {orders.length === 0 && (
              <tr>
                <td colSpan={4} className="py-6 text-center text-gray-400">
                  No purchase orders yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
