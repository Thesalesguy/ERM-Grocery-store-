import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { SuppliersPage } from './SuppliersPage'

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
      permissions: ['purchasing.read', 'purchasing.write'],
    },
  },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderSuppliers() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <SuppliersPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByPlaceholderText(/search by name or code/i)).toBeInTheDocument()
  })
  return utils
}

describe('SuppliersPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists suppliers returned by the API', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/api/v1/purchasing/suppliers',
        json: [
          { id: 1, name: 'Acme Distributors', code: 'ACME', phone: '+255700', is_active: true },
        ],
      },
    ])
    await renderSuppliers()

    await waitFor(() => expect(screen.getByText('Acme Distributors')).toBeInTheDocument())
    expect(screen.getByText('ACME')).toBeInTheDocument()
  })

  it('creates a new supplier through the form', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      { path: '/api/v1/purchasing/suppliers', json: [] },
      {
        method: 'POST',
        path: '/api/v1/purchasing/suppliers',
        status: 201,
        json: { id: 2, name: 'New Supplier Co', code: null, phone: null, is_active: true },
      },
    ])
    await renderSuppliers()
    await waitFor(() => expect(screen.getByText(/no suppliers found/i)).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /new supplier/i }))
    fireEvent.change(screen.getByPlaceholderText('Name'), {
      target: { value: 'New Supplier Co' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => {
      expect(screen.queryByPlaceholderText('Name')).not.toBeInTheDocument()
    })
  })

  it('asks for confirmation before deactivating an active supplier', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/api/v1/purchasing/suppliers',
        json: [{ id: 3, name: 'Confirm Co', code: null, phone: null, is_active: true }],
      },
    ])
    await renderSuppliers()

    await waitFor(() => expect(screen.getByText('Confirm Co')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /deactivate/i }))

    expect(confirmSpy).toHaveBeenCalled()
    expect(screen.getByText('Active')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })
})
