import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { SalesPage } from './SalesPage'

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
      username: 'cashier',
      full_name: 'Store Cashier',
      store_id: 7,
      permissions: ['sales.return.read', 'sales.return.write'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderSales() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <SalesPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Sales History' })).toBeInTheDocument()
  })
  return utils
}

const SAMPLE_SALE = {
  id: 55,
  store_id: 7,
  sale_number: 'SALE-TEST',
  client_transaction_id: 'txn-1',
  cashier_id: 1,
  status: 'COMPLETED',
  subtotal: '30.00',
  discount_total: '0.00',
  tax_total: '0.00',
  grand_total: '30.00',
  amount_tendered: null,
  change_due: null,
  completed_at: '2024-01-01T00:00:00Z',
  created_at: '2024-01-01T00:00:00Z',
  items: [
    {
      id: 200,
      product_id: 55,
      quantity: '3.000',
      unit_price_at_sale: '10.00',
      unit_cost_at_sale: '4.000000',
      discount_amount: '0.00',
      tax_rate_id: null,
      tax_amount: '0.00',
      line_total: '30.00',
      quantity_returned: '0.000',
      product_name: 'Widget',
      product_sku: 'WID-1',
    },
  ],
  payments: [{ id: 1, payment_method: 'CASH', amount: '30.00', reference: null }],
}

const SAMPLE_ELIGIBILITY = {
  sale_id: 55,
  sale_status: 'COMPLETED',
  items: [
    {
      sale_item_id: 200,
      product_id: 55,
      quantity: '3.000',
      quantity_returned: '0.000',
      quantity_returnable: '3.000',
      product_name: 'Widget',
      product_sku: 'WID-1',
    },
  ],
}

describe('SalesPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('finds a sale and shows its returnable lines', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/sales/55/return-eligibility', json: SAMPLE_ELIGIBILITY },
      { path: '/api/v1/sales/55', json: SAMPLE_SALE },
    ])
    await renderSales()

    fireEvent.change(screen.getByPlaceholderText('Sale ID'), { target: { value: '55' } })
    fireEvent.click(screen.getByRole('button', { name: /find sale/i }))

    await waitFor(() => expect(screen.getByText('SALE-TEST')).toBeInTheDocument())
    expect(screen.getByText('Widget')).toBeInTheDocument()
    expect(screen.getAllByText('3.000').length).toBeGreaterThan(0)
  })

  it('submits a return and shows the server-computed refund', async () => {
    const saleReturn = {
      id: 900,
      sale_id: 55,
      store_id: 7,
      return_number: 'RET7-TEST',
      client_transaction_id: 'ret-txn-1',
      reason: null,
      refund_method: 'CASH',
      refund_amount: '10.00',
      processed_by: 1,
      created_at: '2024-01-02T00:00:00Z',
      items: [
        {
          id: 1,
          sale_item_id: 200,
          quantity: '1.000',
          unit_price_refunded: '10.00',
          discount_refunded: '0.00',
          tax_refunded: '0.00',
          unit_cost_refunded: '4.000000',
          restock: true,
          product_id: 55,
          product_name: 'Widget',
          product_sku: 'WID-1',
        },
      ],
    }
    let returnCreated = false
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
        if (url.includes('/sales/55/returns') && method === 'POST') {
          returnCreated = true
          return { ok: true, status: 201, json: async () => saleReturn } as Response
        }
        if (url.includes('/sales/55/return-eligibility')) {
          return { ok: true, status: 200, json: async () => SAMPLE_ELIGIBILITY } as Response
        }
        if (url.includes('/sales/55')) {
          return { ok: true, status: 200, json: async () => SAMPLE_SALE } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    await renderSales()

    fireEvent.change(screen.getByPlaceholderText('Sale ID'), { target: { value: '55' } })
    fireEvent.click(screen.getByRole('button', { name: /find sale/i }))
    await waitFor(() => expect(screen.getByText('SALE-TEST')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText('0'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: /process return/i }))

    await waitFor(() => expect(screen.getByText('Return processed')).toBeInTheDocument())
    expect(screen.getByText('RET7-TEST')).toBeInTheDocument()
    expect(screen.getAllByText('10.00').length).toBeGreaterThan(0)
    expect(returnCreated).toBe(true)
  })

  it('does not show write controls for a read-only user', async () => {
    mockFetchRoutes([
      {
        method: 'POST',
        path: '/auth/refresh',
        json: { access_token: 'tok', token_type: 'bearer', expires_in_seconds: 900 },
      },
      {
        path: '/auth/me',
        json: {
          id: 2,
          username: 'auditor',
          full_name: 'Auditor',
          store_id: null,
          permissions: ['sales.return.read'],
        },
      },
      { path: '/api/v1/sales/55/return-eligibility', json: SAMPLE_ELIGIBILITY },
      { path: '/api/v1/sales/55', json: SAMPLE_SALE },
    ])
    await renderSales()

    fireEvent.change(screen.getByPlaceholderText('Sale ID'), { target: { value: '55' } })
    fireEvent.click(screen.getByRole('button', { name: /find sale/i }))
    await waitFor(() => expect(screen.getByText('SALE-TEST')).toBeInTheDocument())

    expect(screen.queryByRole('button', { name: /process return/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /void entire sale/i })).not.toBeInTheDocument()
  })
})
