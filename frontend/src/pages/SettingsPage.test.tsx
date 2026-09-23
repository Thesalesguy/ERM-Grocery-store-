import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { SettingsPage } from './SettingsPage'

const SETTINGS = {
  id: 7,
  name: 'Downtown Store',
  address: '123 Main St',
  timezone: 'UTC',
  is_active: true,
  attendance_day_boundary_hour: 0,
  return_approval_threshold_amount: null,
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

function stubFetch(
  currentUser: Record<string, unknown>,
  handler: (url: string, method: string, init?: RequestInit) => Response | null,
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
      const response = handler(url, method, init)
      if (response) return response
      throw new Error(`no route for ${method} ${url}`)
    }),
  )
}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

async function renderSettings() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <SettingsPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
  })
  return utils
}

describe('SettingsPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it("loads the acting user's own store automatically and can save changes", async () => {
    let putBody: Record<string, unknown> | null = null
    stubFetch(
      {
        id: 1,
        username: 'mgr',
        full_name: 'Manager',
        store_id: 7,
        permissions: ['store.settings.read', 'store.settings.write'],
      },
      (url, method, init) => {
        if (url.includes('/stores/7/settings') && method === 'GET') {
          return jsonResponse(200, SETTINGS)
        }
        if (url.includes('/stores/7/settings') && method === 'PUT') {
          putBody = JSON.parse((init?.body as string) ?? '{}')
          return jsonResponse(200, { ...SETTINGS, attendance_day_boundary_hour: 6 })
        }
        return null
      },
    )
    await renderSettings()

    await waitFor(() => expect(screen.getByText('Downtown Store')).toBeInTheDocument())
    const hourInput = screen.getByLabelText(/Attendance day boundary hour/)
    fireEvent.change(hourInput, { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
    expect(putBody).toMatchObject({ attendance_day_boundary_hour: 6 })
  })

  it('hides the save control for a read-only (Auditor) permission set', async () => {
    stubFetch(
      {
        id: 2,
        username: 'auditor',
        full_name: 'Auditor',
        store_id: 7,
        permissions: ['store.settings.read'],
      },
      (url, method) => {
        if (url.includes('/stores/7/settings') && method === 'GET') {
          return jsonResponse(200, SETTINGS)
        }
        return null
      },
    )
    await renderSettings()

    await waitFor(() => expect(screen.getByText('Downtown Store')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(screen.getByLabelText(/Attendance day boundary hour/)).toBeDisabled()
  })

  it('lets a company-wide user pick a store id before loading settings', async () => {
    stubFetch(
      {
        id: 3,
        username: 'admin',
        full_name: 'Admin',
        store_id: null,
        permissions: ['store.settings.read', 'store.settings.write'],
      },
      (url, method) => {
        if (url.includes('/stores/9/settings') && method === 'GET') {
          return jsonResponse(200, { ...SETTINGS, id: 9, name: 'Uptown Store' })
        }
        return null
      },
    )
    await renderSettings()

    expect(screen.queryByText('Downtown Store')).not.toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('e.g. 1'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load' }))

    await waitFor(() => expect(screen.getByText('Uptown Store')).toBeInTheDocument())
  })
})
