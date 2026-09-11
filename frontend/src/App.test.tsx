import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import App from './App'
import { setAccessToken } from './api/client'
import { AuthProvider } from './auth/AuthContext'
import { mockFetchRoutes } from './test/mockFetch'

// MemoryRouter (not BrowserRouter) so each render gets its own isolated
// history — BrowserRouter shares jsdom's real window.history, which
// otherwise leaks navigation state (e.g. a redirect to /login) from one
// test into the next in the same file.
function renderApp() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('App routing/auth gate', () => {
  afterEach(() => {
    setAccessToken(null)
  })

  it('redirects an unauthenticated visitor to the login page', async () => {
    mockFetchRoutes([{ method: 'POST', path: '/auth/refresh', status: 401 }])

    renderApp()

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()
    })
  })

  it('shows the dashboard and nav for an already-signed-in user (silent refresh)', async () => {
    mockFetchRoutes([
      {
        method: 'POST',
        path: '/auth/refresh',
        json: { access_token: 'tok', token_type: 'bearer', expires_in_seconds: 900 },
      },
      {
        path: '/auth/me',
        json: {
          id: 1,
          username: 'admin',
          full_name: 'Admin User',
          store_id: 1,
          permissions: ['products.read', 'pos.use'],
        },
      },
      { path: '/health', json: { status: 'ok' } },
    ])

    renderApp()

    await waitFor(() => {
      expect(screen.getByText('Grocery ERP')).toBeInTheDocument()
    })
    expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'POS' })).toBeInTheDocument()
    // Inventory nav item is hidden — this user has no inventory.read.
    expect(screen.queryByRole('link', { name: 'Inventory' })).not.toBeInTheDocument()
  })
})
