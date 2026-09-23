import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { UsersPage } from './UsersPage'

const ADMIN_USER = {
  id: 1,
  username: 'admin',
  full_name: 'Store Admin',
  store_id: null,
  permissions: ['users.manage'],
}

const CASHIER_ROW = {
  id: 2,
  username: 'cashier1',
  email: 'cashier1@example.com',
  full_name: 'Cash Ier',
  store_id: 7,
  is_active: true,
  role: 'Cashier',
}

const ADMIN_ROW = {
  id: 1,
  username: 'admin',
  email: 'admin@example.com',
  full_name: 'Store Admin',
  store_id: null,
  is_active: true,
  role: 'Admin',
}

const ROLES = [
  { name: 'Admin', description: 'Full access' },
  { name: 'Manager', description: 'Store management' },
  { name: 'Cashier', description: 'Point of sale' },
]

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

function stubFetch(handler: (url: string, method: string, init?: RequestInit) => Response | null) {
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
        return { ok: true, status: 200, json: async () => ADMIN_USER } as Response
      }
      const response = handler(url, method, init)
      if (response) return response
      throw new Error(`no route for ${method} ${url}`)
    }),
  )
}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

async function renderUsers() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <UsersPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Users' })).toBeInTheDocument()
  })
  return utils
}

describe('UsersPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it("lists users and disables role/deactivate controls for the acting user's own row", async () => {
    stubFetch((url) => {
      if (url.includes('/auth/users') && !url.includes('role') && !url.includes('reactivate')) {
        return jsonResponse(200, [ADMIN_ROW, CASHIER_ROW])
      }
      if (url.includes('/auth/roles')) return jsonResponse(200, ROLES)
      return null
    })
    await renderUsers()

    await waitFor(() => expect(screen.getByText('cashier1')).toBeInTheDocument())
    const adminRow = screen.getByText('admin').closest('tr')!
    const cashierRow = screen.getByText('cashier1').closest('tr')!

    expect(within(adminRow).getByText('(you)')).toBeInTheDocument()
    expect(within(adminRow).getByRole('combobox')).toBeDisabled()
    expect(within(adminRow).getByRole('button', { name: 'Deactivate' })).toBeDisabled()

    expect(within(cashierRow).getByRole('combobox')).not.toBeDisabled()
    expect(within(cashierRow).getByRole('button', { name: 'Deactivate' })).not.toBeDisabled()
  })

  it('creates a new user against the real API contract', async () => {
    let createdBody: Record<string, unknown> | null = null
    stubFetch((url, method, init) => {
      if (url.endsWith('/auth/users') && method === 'GET') return jsonResponse(200, [ADMIN_ROW])
      if (url.endsWith('/auth/users') && method === 'POST') {
        createdBody = JSON.parse((init?.body as string) ?? '{}')
        return jsonResponse(201, {
          id: 3,
          username: 'newclerk',
          email: 'newclerk@example.com',
          full_name: 'New Clerk',
          store_id: 7,
          is_active: true,
          role: 'Cashier',
        })
      }
      if (url.includes('/auth/roles')) return jsonResponse(200, ROLES)
      return null
    })
    await renderUsers()
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'newclerk' } })
    fireEvent.change(screen.getByPlaceholderText('Email'), {
      target: { value: 'newclerk@example.com' },
    })
    fireEvent.change(screen.getByPlaceholderText(/Temporary password/), {
      target: { value: 'a-strong-password-1' },
    })
    fireEvent.change(screen.getByPlaceholderText(/Store ID/), { target: { value: '7' } })
    fireEvent.change(screen.getByDisplayValue('Select role…'), { target: { value: 'Cashier' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create user' }))

    await waitFor(() => expect(screen.getByText('newclerk')).toBeInTheDocument())
    expect(createdBody).toMatchObject({ username: 'newclerk', role: 'Cashier', store_id: 7 })
  })

  it('surfaces the backend privilege-escalation refusal instead of hiding it', async () => {
    stubFetch((url, method) => {
      if (url.endsWith('/auth/users') && method === 'GET') {
        return jsonResponse(200, [ADMIN_ROW, CASHIER_ROW])
      }
      if (url.includes('/auth/roles')) return jsonResponse(200, ROLES)
      if (url.includes('/auth/users/2/role') && method === 'PUT') {
        return {
          ok: false,
          status: 403,
          json: async () => ({
            error: {
              code: 'PRIVILEGE_ESCALATION_DENIED',
              message: 'You cannot assign a role with permissions you do not hold.',
            },
          }),
        } as Response
      }
      return null
    })
    await renderUsers()
    await waitFor(() => expect(screen.getByText('cashier1')).toBeInTheDocument())

    const cashierRow = screen.getByText('cashier1').closest('tr')!
    fireEvent.change(within(cashierRow).getByRole('combobox'), { target: { value: 'Admin' } })

    await waitFor(() =>
      expect(
        within(cashierRow).getByText('You cannot assign a role with permissions you do not hold.'),
      ).toBeInTheDocument(),
    )
  })

  it('deactivates and reactivates a user', async () => {
    let active = true
    stubFetch((url, method) => {
      if (url.endsWith('/auth/users') && method === 'GET') {
        return jsonResponse(200, [ADMIN_ROW, { ...CASHIER_ROW, is_active: active }])
      }
      if (url.includes('/auth/roles')) return jsonResponse(200, ROLES)
      if (url.includes('/auth/users/2/deactivate') && method === 'POST') {
        active = false
        return jsonResponse(200, { id: 2, username: 'cashier1', is_active: false })
      }
      if (url.includes('/auth/users/2/reactivate') && method === 'POST') {
        active = true
        return jsonResponse(200, { id: 2, username: 'cashier1', is_active: true })
      }
      return null
    })
    await renderUsers()
    await waitFor(() => expect(screen.getByText('cashier1')).toBeInTheDocument())

    const cashierRow = screen.getByText('cashier1').closest('tr')!
    fireEvent.click(within(cashierRow).getByRole('button', { name: 'Deactivate' }))
    await waitFor(() =>
      expect(within(cashierRow).getByRole('button', { name: 'Reactivate' })).toBeInTheDocument(),
    )
    expect(within(cashierRow).getByText('Deactivated')).toBeInTheDocument()

    fireEvent.click(within(cashierRow).getByRole('button', { name: 'Reactivate' }))
    await waitFor(() =>
      expect(within(cashierRow).getByRole('button', { name: 'Deactivate' })).toBeInTheDocument(),
    )
  })
})
