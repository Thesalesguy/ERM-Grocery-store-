import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { PurchasingPage } from './PurchasingPage'

const AUTH_ROUTES = [
  {
    method: 'POST',
    path: '/auth/refresh',
    json: { access_token: 'tok', token_type: 'bearer', expires_in_seconds: 900 },
  },
  {
    path: '/auth/me',
    json: {
      id: 1,
      username: 'manager',
      full_name: 'Store Manager',
      store_id: 7,
      permissions: ['purchasing.read', 'purchasing.write', 'purchasing.receive'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderPurchasing() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <PurchasingPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Purchasing' })).toBeInTheDocument()
    expect(screen.queryByText(/loading/i)).not.toBeInTheDocument()
  })
  return utils
}

const SAMPLE_PO = {
  id: 42,
  store_id: 7,
  supplier_id: 1,
  purchase_number: 'PO7-TEST',
  status: 'DRAFT',
  order_date: '2024-01-01',
  expected_date: null,
  notes: null,
  created_by: 1,
  created_at: '2024-01-01T00:00:00Z',
  supplier_name: 'Acme Distributors',
  items: [
    {
      id: 100,
      product_id: 55,
      quantity_ordered: '10.000',
      quantity_received: '0.000',
      unit_cost: '5.000000',
      quantity_remaining: '10.000',
      is_over_received: false,
      product_name: 'Widget',
      product_sku: 'WID-1',
    },
  ],
}

describe('PurchasingPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists purchase orders', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/purchasing/purchase-orders', json: [SAMPLE_PO] },
    ])
    await renderPurchasing()

    expect(screen.getByText('PO7-TEST')).toBeInTheDocument()
    expect(screen.getByText('Acme Distributors')).toBeInTheDocument()
  })

  it('opens a purchase order detail view and submits it', async () => {
    const submittedPo = { ...SAMPLE_PO, status: 'ORDERED' }
    let submitted = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        if (url.includes('/auth/refresh') && method === 'POST') {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              access_token: 'tok',
              token_type: 'bearer',
              expires_in_seconds: 900,
            }),
          } as Response
        }
        if (url.includes('/auth/me')) {
          return { ok: true, status: 200, json: async () => AUTH_ROUTES[1].json } as Response
        }
        if (url.includes('/purchase-orders/42/submit') && method === 'POST') {
          submitted = true
          return { ok: true, status: 200, json: async () => submittedPo } as Response
        }
        if (url.includes('/purchase-orders/42') && method === 'GET') {
          return {
            ok: true,
            status: 200,
            json: async () => (submitted ? submittedPo : SAMPLE_PO),
          } as Response
        }
        if (url.includes('/purchase-orders') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [SAMPLE_PO] } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    await renderPurchasing()

    fireEvent.click(screen.getByText('PO7-TEST'))
    await waitFor(() => expect(screen.getByText('Widget')).toBeInTheDocument())
    expect(screen.getByText('DRAFT')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /submit to supplier/i }))
    await waitFor(() => expect(screen.getByText('ORDERED')).toBeInTheDocument())
  })

  it('receives goods against an ordered purchase order', async () => {
    const orderedPo = { ...SAMPLE_PO, status: 'ORDERED' }
    const receivedPo = {
      ...orderedPo,
      status: 'RECEIVED',
      items: [
        {
          ...orderedPo.items[0],
          quantity_received: '10.000',
          quantity_remaining: '0.000',
        },
      ],
    }
    let receiveCallCount = 0
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/purchasing/purchase-orders', json: [orderedPo] },
      {
        method: 'GET',
        path: '/api/v1/purchasing/purchase-orders/42',
        json: orderedPo,
      },
    ])
    // Override the default mock for the receive call and the second
    // detail refetch (which must reflect the post-receipt state).
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        if (url.includes('/auth/refresh') && method === 'POST') {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              access_token: 'tok',
              token_type: 'bearer',
              expires_in_seconds: 900,
            }),
          } as Response
        }
        if (url.includes('/auth/me')) {
          return { ok: true, status: 200, json: async () => AUTH_ROUTES[1].json } as Response
        }
        if (url.includes('/api/v1/purchasing/purchase-orders/42/receive') && method === 'POST') {
          receiveCallCount += 1
          return {
            ok: true,
            status: 201,
            json: async () => ({
              id: 900,
              purchase_order_id: 42,
              store_id: 7,
              client_transaction_id: 'whatever',
              received_date: '2024-01-05',
              received_by: 1,
              notes: null,
              created_at: '2024-01-05T00:00:00Z',
              items: [],
            }),
          } as Response
        }
        if (url.includes('/api/v1/purchasing/purchase-orders/42') && method === 'GET') {
          return {
            ok: true,
            status: 200,
            json: async () => (receiveCallCount > 0 ? receivedPo : orderedPo),
          } as Response
        }
        if (url.includes('/api/v1/purchasing/purchase-orders') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [orderedPo] } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )

    await renderPurchasing()
    fireEvent.click(screen.getByText('PO7-TEST'))
    await waitFor(() => expect(screen.getByText('Receive goods')).toBeInTheDocument())

    const quantityInput = screen.getByPlaceholderText('0')
    fireEvent.change(quantityInput, { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: /confirm receipt/i }))

    await waitFor(() => expect(screen.getByText('RECEIVED')).toBeInTheDocument())
    expect(receiveCallCount).toBe(1)
  })

  it('creates a new purchase order via the form with a client_transaction_id', async () => {
    // Regression test for a real M19 defect: PurchaseOrderCreate became a
    // required client_transaction_id field, but this form's caller and its
    // TypeScript input type were never updated to send one -- every real
    // "New purchase order" submission would have 422'd. Asserts the actual
    // outgoing request body carries a non-empty client_transaction_id, not
    // just that the mocked call succeeds.
    const SUPPLIER = { id: 1, name: 'Acme Distributors', is_active: true }
    const PRODUCT = { id: 55, sku: 'WID-1', name: 'Widget' }
    const createdPo = { ...SAMPLE_PO, id: 43 }
    let capturedBody: Record<string, unknown> | null = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        if (url.includes('/auth/refresh') && method === 'POST') {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              access_token: 'tok',
              token_type: 'bearer',
              expires_in_seconds: 900,
            }),
          } as Response
        }
        if (url.includes('/auth/me')) {
          return { ok: true, status: 200, json: async () => AUTH_ROUTES[1].json } as Response
        }
        // More specific routes (the single-PO detail fetch) must be
        // checked before the general list/create routes below, since
        // '/purchase-orders/43' also includes '/purchase-orders'.
        if (url.includes('/api/v1/purchasing/purchase-orders/43') && method === 'GET') {
          return { ok: true, status: 200, json: async () => createdPo } as Response
        }
        if (url.includes('/api/v1/purchasing/purchase-orders') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [] } as Response
        }
        if (url.includes('/api/v1/purchasing/suppliers') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [SUPPLIER] } as Response
        }
        if (url.includes('/api/v1/products') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [PRODUCT] } as Response
        }
        if (url.includes('/api/v1/purchasing/purchase-orders') && method === 'POST') {
          capturedBody = JSON.parse(String(init?.body))
          return { ok: true, status: 201, json: async () => createdPo } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    await renderPurchasing()

    fireEvent.click(screen.getByRole('button', { name: /new purchase order/i }))
    await waitFor(() => expect(screen.getByText('New purchase order')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText('SKU'), { target: { value: 'WID-1' } })
    fireEvent.change(screen.getByPlaceholderText('Qty'), { target: { value: '10' } })
    fireEvent.change(screen.getByPlaceholderText('Cost'), { target: { value: '5.00' } })
    fireEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    await waitFor(() => expect(capturedBody).not.toBeNull())
    expect(typeof capturedBody!.client_transaction_id).toBe('string')
    expect((capturedBody!.client_transaction_id as string).length).toBeGreaterThan(0)

    // Let the resulting navigation to the new PO's detail view settle
    // (onCreated selects it) so no fetch is left in flight after the test.
    await waitFor(() => expect(screen.getByText('PO7-TEST')).toBeInTheDocument())
  })
})
