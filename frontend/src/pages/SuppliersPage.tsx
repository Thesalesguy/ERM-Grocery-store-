import { useEffect, useState } from 'react'
import * as purchasingApi from '../api/purchasing'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

function SupplierForm({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [phone, setPhone] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)
    try {
      await purchasingApi.createSupplier({
        name,
        code: code || undefined,
        phone: phone || undefined,
      })
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create supplier.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">New supplier</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <input
          placeholder="Name"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          placeholder="Code (optional)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={code}
          onChange={(e) => setCode(e.target.value)}
        />
        <input
          placeholder="Phone (optional)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <div className="mt-3 flex gap-2">
        <button
          type="submit"
          disabled={isSubmitting}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {isSubmitting ? 'Saving…' : 'Save'}
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

export function SuppliersPage() {
  const { hasPermission } = useAuth()
  const [suppliers, setSuppliers] = useState<purchasingApi.Supplier[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setSuppliers(await purchasingApi.listSuppliers({ search: search || undefined }))
    } catch {
      setError('Could not load suppliers.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const timeout = setTimeout(load, 250)
    return () => clearTimeout(timeout)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  async function toggleActive(supplier: purchasingApi.Supplier) {
    if (
      supplier.is_active &&
      !window.confirm(
        `Deactivate "${supplier.name}"? It will no longer be usable on new purchase orders.`,
      )
    ) {
      return
    }
    await purchasingApi.setSupplierActive(supplier.id, !supplier.is_active)
    load()
  }

  const canWrite = hasPermission('purchasing.write')

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-900">Suppliers</h1>
        {canWrite && !showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
          >
            New supplier
          </button>
        )}
      </div>

      {showForm && (
        <SupplierForm
          onCreated={() => {
            setShowForm(false)
            load()
          }}
          onCancel={() => setShowForm(false)}
        />
      )}

      <input
        placeholder="Search by name or code…"
        className="mt-4 w-full max-w-sm rounded border border-gray-300 px-3 py-2 text-sm"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-500">
              <th className="py-2 pr-4">Code</th>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Phone</th>
              <th className="py-2 pr-4">Status</th>
              {canWrite && <th className="py-2 pr-4" />}
            </tr>
          </thead>
          <tbody>
            {suppliers.map((s) => (
              <tr key={s.id} className="border-b border-gray-100">
                <td className="py-2 pr-4 font-mono text-xs">{s.code ?? '—'}</td>
                <td className="py-2 pr-4">{s.name}</td>
                <td className="py-2 pr-4">{s.phone ?? '—'}</td>
                <td className="py-2 pr-4">
                  <span
                    className={
                      s.is_active
                        ? 'rounded bg-green-100 px-2 py-0.5 text-xs text-green-800'
                        : 'rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600'
                    }
                  >
                    {s.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                {canWrite && (
                  <td className="py-2 pr-4">
                    <button
                      onClick={() => toggleActive(s)}
                      className="text-xs text-blue-600 hover:underline"
                    >
                      {s.is_active ? 'Deactivate' : 'Activate'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
            {suppliers.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-center text-gray-400">
                  No suppliers found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
