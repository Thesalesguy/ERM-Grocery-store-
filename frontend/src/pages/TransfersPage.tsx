import { useEffect, useState } from 'react'
import * as transfersApi from '../api/transfers'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

function CreateTransferForm({ onCreated }: { onCreated: (t: transfersApi.TransferWithLines) => void }) {
  const [fromStoreId, setFromStoreId] = useState('')
  const [toStoreId, setToStoreId] = useState('')
  const [sourceProductId, setSourceProductId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)
    try {
      const transfer = await transfersApi.createTransfer({
        from_store_id: Number(fromStoreId),
        to_store_id: Number(toStoreId),
        requested_date: new Date().toISOString().slice(0, 10),
        lines: [{ source_product_id: Number(sourceProductId), requested_quantity: quantity }],
      })
      setSourceProductId('')
      setQuantity('')
      onCreated(transfer)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create transfer.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">New transfer</h2>
      <p className="mt-1 text-xs text-gray-500">
        The destination product is matched by SKU automatically — it must already exist in the
        destination store's catalog.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
        <input
          placeholder="From store ID"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={fromStoreId}
          onChange={(e) => setFromStoreId(e.target.value)}
          required
        />
        <input
          placeholder="To store ID"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={toStoreId}
          onChange={(e) => setToStoreId(e.target.value)}
          required
        />
        <input
          placeholder="Source product ID"
          inputMode="numeric"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={sourceProductId}
          onChange={(e) => setSourceProductId(e.target.value)}
          required
        />
        <input
          placeholder="Quantity"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          required
        />
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={isSubmitting}
        className="mt-3 rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {isSubmitting ? 'Creating…' : 'Create transfer'}
      </button>
    </form>
  )
}

function TransferDetail({
  transfer,
  onChanged,
}: {
  transfer: transfersApi.TransferWithLines
  onChanged: () => void
}) {
  const { hasPermission } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [shipQuantities, setShipQuantities] = useState<Record<number, string>>({})
  const [receiveQuantities, setReceiveQuantities] = useState<Record<number, string>>({})

  const canWrite = hasPermission('inventory.transfer.write')
  const canShip = hasPermission('inventory.transfer.ship')
  const canReceive = hasPermission('inventory.transfer.receive')

  async function handleShip() {
    setError(null)
    setBusy(true)
    try {
      const lines = transfer.lines
        .filter((l) => shipQuantities[l.id])
        .map((l) => ({ transfer_line_id: l.id, quantity_to_ship: shipQuantities[l.id] }))
      if (lines.length === 0) {
        setError('Enter at least one quantity to ship.')
        return
      }
      await transfersApi.shipTransfer(transfer.id, {
        client_transaction_id: crypto.randomUUID(),
        lines,
      })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not ship transfer.')
    } finally {
      setBusy(false)
    }
  }

  async function handleReceive() {
    setError(null)
    setBusy(true)
    try {
      const lines = transfer.lines
        .filter((l) => receiveQuantities[l.id])
        .map((l) => ({ transfer_line_id: l.id, quantity_received: receiveQuantities[l.id] }))
      if (lines.length === 0) {
        setError('Enter at least one quantity to receive.')
        return
      }
      await transfersApi.receiveTransfer(transfer.id, {
        received_date: new Date().toISOString().slice(0, 10),
        client_transaction_id: crypto.randomUUID(),
        lines,
      })
      setReceiveQuantities({})
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not receive transfer.')
    } finally {
      setBusy(false)
    }
  }

  async function handleCancel() {
    setError(null)
    setBusy(true)
    try {
      await transfersApi.cancelTransfer(transfer.id, 'Cancelled by user')
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not cancel transfer.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">{transfer.transfer_number}</h2>
          <p className="text-xs text-gray-500">
            Store {transfer.from_store_id} → Store {transfer.to_store_id} ·{' '}
            <span className="font-medium uppercase">{transfer.status}</span>
          </p>
        </div>
        {transfer.status === 'DRAFT' && canWrite && (
          <button
            disabled={busy}
            onClick={handleCancel}
            className="rounded border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Line</th>
            <th className="py-2 pr-4">Requested</th>
            <th className="py-2 pr-4">Shipped</th>
            <th className="py-2 pr-4">Received</th>
            {transfer.status === 'DRAFT' && canShip && <th className="py-2 pr-4">Ship qty</th>}
            {transfer.status === 'SHIPPED' && canReceive && (
              <th className="py-2 pr-4">Receive qty</th>
            )}
          </tr>
        </thead>
        <tbody>
          {transfer.lines.map((line) => (
            <tr key={line.id} className="border-b border-gray-100">
              <td className="py-2 pr-4 text-gray-400">#{line.id}</td>
              <td className="py-2 pr-4">{line.requested_quantity}</td>
              <td className="py-2 pr-4">{line.shipped_quantity}</td>
              <td className="py-2 pr-4">{line.received_quantity}</td>
              {transfer.status === 'DRAFT' && canShip && (
                <td className="py-2 pr-4">
                  <input
                    className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
                    value={shipQuantities[line.id] ?? ''}
                    onChange={(e) =>
                      setShipQuantities((prev) => ({ ...prev, [line.id]: e.target.value }))
                    }
                  />
                </td>
              )}
              {transfer.status === 'SHIPPED' && canReceive && (
                <td className="py-2 pr-4">
                  <input
                    className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
                    value={receiveQuantities[line.id] ?? ''}
                    onChange={(e) =>
                      setReceiveQuantities((prev) => ({ ...prev, [line.id]: e.target.value }))
                    }
                  />
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>

      {transfer.status === 'DRAFT' && canShip && (
        <button
          disabled={busy}
          onClick={handleShip}
          className="mt-3 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {busy ? 'Shipping…' : 'Ship'}
        </button>
      )}
      {transfer.status === 'SHIPPED' && canReceive && (
        <button
          disabled={busy}
          onClick={handleReceive}
          className="mt-3 rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
        >
          {busy ? 'Receiving…' : 'Receive'}
        </button>
      )}
    </div>
  )
}

export function TransfersPage() {
  const [transfers, setTransfers] = useState<transfersApi.Transfer[]>([])
  const [selected, setSelected] = useState<transfersApi.TransferWithLines | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await transfersApi.listTransfers()
      setTransfers(data)
    } catch {
      setError('Could not load transfers.')
    } finally {
      setLoading(false)
    }
  }

  async function loadSelected(id: number) {
    const detail = await transfersApi.getTransfer(id)
    setSelected(detail)
  }

  useEffect(() => {
    // Intentional: load the list once on mount, the same pattern
    // InventoryPage/ProductsPage use for their initial fetch.
    // oxlint-disable-next-line react/set-state-in-effect
    load()
  }, [])

  async function refreshAll() {
    await load()
    if (selected) await loadSelected(selected.id)
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Inter-Store Transfers</h1>

      <CreateTransferForm
        onCreated={(t) => {
          load()
          loadSelected(t.id)
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
                  <th className="py-2 pr-4">Transfer</th>
                  <th className="py-2 pr-4">Status</th>
                </tr>
              </thead>
              <tbody>
                {transfers.map((t) => (
                  <tr
                    key={t.id}
                    onClick={() => loadSelected(t.id)}
                    className={`cursor-pointer border-b border-gray-100 hover:bg-gray-50 ${
                      selected?.id === t.id ? 'bg-blue-50' : ''
                    }`}
                  >
                    <td className="py-2 pr-4 font-mono text-xs">{t.transfer_number}</td>
                    <td className="py-2 pr-4 text-xs uppercase">{t.status}</td>
                  </tr>
                ))}
                {transfers.length === 0 && (
                  <tr>
                    <td colSpan={2} className="py-6 text-center text-gray-400">
                      No transfers yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="lg:col-span-2">
            {selected ? (
              <TransferDetail transfer={selected} onChanged={refreshAll} />
            ) : (
              <p className="text-sm text-gray-500">Select a transfer to view its lines.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
