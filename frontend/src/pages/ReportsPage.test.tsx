import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { ReportsPage } from './ReportsPage'

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

function stubFetch(
  currentUser: Record<string, unknown>,
  handler: (url: string, method: string) => Response | null,
) {
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
        return { ok: true, status: 200, json: async () => currentUser } as Response
      }
      const response = handler(url, method)
      if (response) return response
      throw new Error(`no route for ${method} ${url}`)
    }),
  )
}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

async function renderReports() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <ReportsPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Reports' })).toBeInTheDocument()
  })
  return utils
}

const SALES_SUMMARY = {
  store_ids: null,
  date_from: '2024-01-01',
  date_to: '2024-01-31',
  gross_sales: '1000.00',
  discounts: '10.00',
  returns: '5.00',
  void_count: 0,
  void_amount: '0.00',
  net_sales: '985.00',
  tax: '80.00',
  cogs: '600.00',
  gross_profit: '385.00',
  gross_margin_percent: '39.09',
  transaction_count: 42,
  units_sold: '120.000',
  average_transaction_value: '23.45',
}

const PNL = {
  store_id: null,
  date_from: '2024-01-01',
  date_to: '2024-01-31',
  net_sales: '985.00',
  cogs: '620.00',
  gross_profit: '365.00',
  other_income: '0.00',
  operating_expenses: '100.00',
  net_income: '265.00',
}

describe('ReportsPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('shows the operational sales summary distinctly labeled from GL-derived P&L', async () => {
    stubFetch(
      {
        id: 1,
        username: 'mgr',
        full_name: 'Manager',
        store_id: 7,
        permissions: ['reports.read', 'accounting.read'],
      },
      (url) => {
        if (url.includes('/reports/sales/summary')) return jsonResponse(200, SALES_SUMMARY)
        if (url.includes('/reports/sales/breakdown')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/by-payment-method')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/trend')) return jsonResponse(200, [])
        if (url.includes('/reports/financial/profit-loss')) return jsonResponse(200, PNL)
        return null
      },
    )
    await renderReports()

    await waitFor(() => expect(screen.getByText('385.00')).toBeInTheDocument())
    expect(screen.getByText('Gross profit (operational)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Financial' }))
    await waitFor(() => expect(screen.getByText('365.00')).toBeInTheDocument())
    expect(screen.getByText('Gross profit (GL-derived)')).toBeInTheDocument()
    // The two figures differ (385.00 operational vs 365.00 GL-derived) and
    // each carries its own distinct label -- never conflated as one truth.
    expect(screen.queryByText('385.00')).not.toBeInTheDocument()
  })

  it('only shows tabs the user actually has permission to view', async () => {
    stubFetch(
      {
        id: 2,
        username: 'clerk',
        full_name: 'Sales Clerk',
        store_id: 7,
        permissions: ['reports.read'],
      },
      (url) => {
        if (url.includes('/reports/sales/summary')) return jsonResponse(200, SALES_SUMMARY)
        if (url.includes('/reports/sales/breakdown')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/by-payment-method')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/trend')) return jsonResponse(200, [])
        return null
      },
    )
    await renderReports()

    await waitFor(() => expect(screen.getByText('Gross profit (operational)')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Sales' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dashboard' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Financial' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Inventory' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Purchasing' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Payroll' })).not.toBeInTheDocument()
  })

  it('surfaces a report API error instead of silently showing nothing', async () => {
    stubFetch(
      { id: 1, username: 'mgr', full_name: 'Manager', store_id: 7, permissions: ['reports.read'] },
      (url) => {
        if (url.includes('/reports/sales/summary')) {
          return {
            ok: false,
            status: 403,
            json: async () => ({
              error: { code: 'STORE_ACCESS_DENIED', message: 'Not authorized for this store.' },
            }),
          } as Response
        }
        if (url.includes('/reports/sales/breakdown')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/by-payment-method')) return jsonResponse(200, [])
        if (url.includes('/reports/sales/trend')) return jsonResponse(200, [])
        return null
      },
    )
    await renderReports()

    await waitFor(() =>
      expect(screen.getByText('Not authorized for this store.')).toBeInTheDocument(),
    )
  })
})
