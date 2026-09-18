import { useEffect, useState } from 'react'
import * as productsApi from '../api/products'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

function ProductForm({
  storeId,
  onCreated,
  onCancel,
}: {
  storeId: number
  onCreated: () => void
  onCancel: () => void
}) {
  const [sku, setSku] = useState('')
  const [name, setName] = useState('')
  const [price, setPrice] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)
    try {
      await productsApi.createProduct({ store_id: storeId, sku, name, current_price: price })
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create product.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mb-6 rounded border border-gray-200 bg-white p-4 shadow-sm"
    >
      <h2 className="text-sm font-semibold text-gray-900">New product</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <input
          placeholder="SKU"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={sku}
          onChange={(e) => setSku(e.target.value)}
          required
        />
        <input
          placeholder="Name"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          placeholder="Price"
          inputMode="decimal"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
          value={price}
          onChange={(e) => setPrice(e.target.value)}
          required
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

export function ProductsPage() {
  const { user, hasPermission } = useAuth()
  const [products, setProducts] = useState<productsApi.Product[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
    const [showForm, setShowForm] = useState(false)
    const [selectedStoreId, setSelectedStoreId] = useState<number | null>(null)
    const [availableStores, setAvailableStores] = useState<{ id: number; name: string }[]>([{ id: 1, name: 'Main Street Branch' }])


  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await productsApi.listProducts({
        store_id: user?.store_id ?? undefined,
        search: search || undefined,
      })
      setProducts(data)
    } catch {
      setError('Could not load products.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const timeout = setTimeout(load, 250) // debounce search
    return () => clearTimeout(timeout)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  async function toggleActive(product: productsApi.Product) {
    if (
      product.is_active &&
      !window.confirm(`Deactivate "${product.name}"? It will no longer be sellable.`)
    ) {
      return
    }
    await productsApi.setProductActive(product.id, !product.is_active)
    load()
  }

  const canWrite = hasPermission('products.write')

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-900">Products</h1>
        {canWrite && !showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
          >
            New product
          </button>
        )}
      </div>

          {showForm && (
              <div className="mt-4 rounded border border-gray-200 bg-gray-50 p-4">
                  {/* Dropdown for Cross-Store Admin if no branch is selected yet */}
                  {user?.store_id == null && !selectedStoreId && (
                      <div className="space-y-3">
                          <label className="block text-sm font-medium text-gray-700">Select Target Operating Branch:</label>
                          <select
                              className="w-full max-w-sm rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
                              onChange={(e) => setSelectedStoreId(Number(e.target.value) || null)}
                              defaultValue=""
                          >
                              <option value="" disabled>-- Choose a Location Branch --</option>
                              {availableStores.map(store => (
                                  <option key={store.id} value={store.id}>{store.name}</option>
                              ))}
                          </select>
                          <div>
                              <button
                                  onClick={() => setShowForm(false)}
                                  className="text-sm font-medium text-gray-600 hover:text-gray-800"
                              >
                                  Cancel
                              </button>
                          </div>
                      </div>
                  )}

                  {/* Render ProductForm once a valid store ID context is resolved */}
                  {(user?.store_id != null || selectedStoreId != null) && (
                      <ProductForm
                          storeId={user?.store_id ?? selectedStoreId!}
                          onCreated={() => {
                              setShowForm(false)
                              setSelectedStoreId(null)
                              load()
                          }}
                          onCancel={() => {
                              setShowForm(false)
                              setSelectedStoreId(null)
                          }}
                      />
                  )}
              </div>
          )}

      <input
        placeholder="Search by name or SKU…"
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
              <th className="py-2 pr-4">SKU</th>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Price</th>
              <th className="py-2 pr-4">Stock</th>
              <th className="py-2 pr-4">Status</th>
              {canWrite && <th className="py-2 pr-4" />}
            </tr>
          </thead>
          <tbody>
            {products.map((p) => (
              <tr key={p.id} className="border-b border-gray-100">
                <td className="py-2 pr-4 font-mono text-xs">{p.sku}</td>
                <td className="py-2 pr-4">{p.name}</td>
                <td className="py-2 pr-4">{p.current_price}</td>
                <td className="py-2 pr-4">{p.current_qty_on_hand}</td>
                <td className="py-2 pr-4">
                  <span
                    className={
                      p.is_active
                        ? 'rounded bg-green-100 px-2 py-0.5 text-xs text-green-800'
                        : 'rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600'
                    }
                  >
                    {p.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                {canWrite && (
                  <td className="py-2 pr-4">
                    <button
                      onClick={() => toggleActive(p)}
                      className="text-xs text-blue-600 hover:underline"
                    >
                      {p.is_active ? 'Deactivate' : 'Activate'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
            {products.length === 0 && (
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
