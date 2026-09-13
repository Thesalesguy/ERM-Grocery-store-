import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { HrPage } from './HrPage'

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
      permissions: ['hr.read', 'hr.write', 'hr.compensation.write'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderHr() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <HrPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByText(/hr \/ workforce/i)).toBeInTheDocument()
  })
  return utils
}

describe('HrPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists employees returned by the API', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/hr/employees',
        json: [
          {
            id: 1,
            employee_number: 'EMP-001',
            legal_name: 'Jane Doe',
            display_name: null,
            hire_date: '2024-01-01',
            user_id: null,
          },
        ],
      },
    ])

    await renderHr()

    await waitFor(() => {
      expect(screen.getByText('Jane Doe')).toBeInTheDocument()
    })
    expect(screen.getByText('#EMP-001')).toBeInTheDocument()
  })

  it('shows the hire form when the user has hr.write', async () => {
    mockFetchRoutes([...AUTH_ROUTES, { path: '/hr/employees', json: [] }])

    await renderHr()

    await waitFor(() => {
      expect(screen.getByText(/hire employee/i)).toBeInTheDocument()
    })
  })

  it('shows an empty state when there are no employees', async () => {
    mockFetchRoutes([...AUTH_ROUTES, { path: '/hr/employees', json: [] }])

    await renderHr()

    await waitFor(() => {
      expect(screen.getByText(/no employees yet/i)).toBeInTheDocument()
    })
  })
})
