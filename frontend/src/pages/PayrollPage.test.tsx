import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { PayrollPage } from './PayrollPage'

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
      permissions: ['payroll.read', 'payroll.calculate', 'payroll.approve'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderPayroll() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <PayrollPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByText(/^payroll$/i)).toBeInTheDocument()
  })
  return utils
}

describe('PayrollPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists payroll periods returned by the API', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/payroll/periods',
        json: [
          {
            id: 1,
            store_id: 7,
            payroll_run_id: null,
            period_start: '2024-01-01',
            period_end: '2024-01-15',
            pay_date: '2024-01-20',
            status: 'DRAFT',
            calculated_by: null,
            calculated_at: null,
            approved_by: null,
            approved_at: null,
            journal_entry_id: null,
            posted_by: null,
            posted_at: null,
            total_gross: '0.00',
            total_deductions: '0.00',
            total_employer_contributions: '0.00',
            total_net_pay: '0.00',
          },
        ],
      },
    ])

    await renderPayroll()

    await waitFor(() => {
      expect(screen.getByText(/2024-01-01\.\.2024-01-15/)).toBeInTheDocument()
    })
    expect(screen.getByText('DRAFT')).toBeInTheDocument()
  })

  it('shows an empty state when there are no periods', async () => {
    mockFetchRoutes([...AUTH_ROUTES, { path: '/payroll/periods', json: [] }])

    await renderPayroll()

    await waitFor(() => {
      expect(screen.getByText(/no payroll periods yet/i)).toBeInTheDocument()
    })
  })

  it('shows the create-period form when the user has payroll.calculate', async () => {
    mockFetchRoutes([...AUTH_ROUTES, { path: '/payroll/periods', json: [] }])

    await renderPayroll()

    await waitFor(() => {
      expect(screen.getByText(/new payroll period/i)).toBeInTheDocument()
    })
  })
})
