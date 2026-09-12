import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { AccountingPage } from './AccountingPage'

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
      permissions: ['accounting.read', 'accounting.reverse'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderAccounting() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <AccountingPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Accounting' })).toBeInTheDocument()
  })
  return utils
}

const SAMPLE_ENTRY = {
  id: 500,
  journal_number: 'JE7-TEST',
  store_id: 7,
  posting_date: '2024-01-05',
  entry_type: 'STANDARD',
  source_type: 'SALE',
  source_id: 42,
  reversal_of_id: null,
  memo: 'Sale S7-TEST',
  created_by: 1,
  created_at: '2024-01-05T00:00:00Z',
  is_reversed: false,
  lines: [
    {
      id: 1,
      account_id: 1,
      account_code: '1000',
      account_name: 'Cash on Hand',
      debit: '10.000000',
      credit: '0.000000',
      product_id: null,
      description: null,
    },
    {
      id: 2,
      account_id: 2,
      account_code: '4000',
      account_name: 'Sales Revenue',
      debit: '0.000000',
      credit: '10.000000',
      product_id: null,
      description: null,
    },
  ],
}

describe('AccountingPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists journal entries by default', async () => {
    mockFetchRoutes([...AUTH_ROUTES, { path: '/api/v1/accounting/journals', json: [SAMPLE_ENTRY] }])
    await renderAccounting()

    await waitFor(() => expect(screen.getByText('JE7-TEST')).toBeInTheDocument())
    expect(screen.getByText('POSTED')).toBeInTheDocument()
  })

  it('shows the trial balance totals when that tab is selected', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/accounting/journals', json: [] },
      {
        path: '/api/v1/accounting/reports/trial-balance',
        json: {
          store_id: null,
          date_from: null,
          date_to: null,
          rows: [
            {
              account_code: '1000',
              account_name: 'Cash on Hand',
              account_type: 'ASSET',
              normal_balance: 'DEBIT',
              total_debit: '10.000000',
              total_credit: '0.000000',
            },
          ],
          total_debit: '10.000000',
          total_credit: '10.000000',
        },
      },
    ])
    await renderAccounting()
    fireEvent.click(screen.getByRole('button', { name: 'Trial Balance' }))

    await waitFor(() => expect(screen.getByText('Cash on Hand')).toBeInTheDocument())
    expect(screen.getAllByText('10.000000')).toHaveLength(3) // debit cell, total debit, total credit
  })

  it('opens a journal entry and reverses it', async () => {
    // Reversal is only offered in the UI for MANUAL entries — reversing
    // an automated source (SALE, etc.) is refused by the backend
    // (docs/M4_HARDENING_AUDIT.md Section 1), so this scenario uses a
    // manual entry to exercise the reversal button/flow at all.
    const manualEntry = { ...SAMPLE_ENTRY, source_type: 'MANUAL', source_id: null }
    const reversedEntry = { ...manualEntry, is_reversed: true }
    let reverseCalled = false
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
        if (url.includes('/api/v1/accounting/journals/500/reverse') && method === 'POST') {
          reverseCalled = true
          return {
            ok: true,
            status: 200,
            json: async () => ({ ...manualEntry, entry_type: 'REVERSAL' }),
          } as Response
        }
        if (url.includes('/api/v1/accounting/journals/500') && method === 'GET') {
          return {
            ok: true,
            status: 200,
            json: async () => (reverseCalled ? reversedEntry : manualEntry),
          } as Response
        }
        if (url.includes('/api/v1/accounting/journals') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [manualEntry] } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    vi.spyOn(window, 'prompt').mockReturnValue('made a mistake')

    await renderAccounting()
    await waitFor(() => expect(screen.getByText('JE7-TEST')).toBeInTheDocument())
    fireEvent.click(screen.getByText('JE7-TEST'))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /reverse entry/i })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /reverse entry/i }))

    await waitFor(() => expect(reverseCalled).toBe(true))
  })

  it('does not offer reversal for an automatically-posted SALE entry', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/accounting/journals/500', json: SAMPLE_ENTRY },
      { path: '/api/v1/accounting/journals', json: [SAMPLE_ENTRY] },
    ])
    await renderAccounting()
    await waitFor(() => expect(screen.getByText('JE7-TEST')).toBeInTheDocument())
    fireEvent.click(screen.getByText('JE7-TEST'))

    await waitFor(() => expect(screen.getByText(/posted automatically/i)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /reverse entry/i })).not.toBeInTheDocument()
  })
})
