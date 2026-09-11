import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { ProductsPage } from './ProductsPage'

// ProductsPage reads `user` from context only in a mount-time effect, so —
// just like the real app's ProtectedRoute — it must not mount until the
// initial silent-refresh/isLoading check has resolved. Skipping this gate
// would let the page mount with `user: null` and send requests with no
// store_id filter.
function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

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
      permissions: ['products.read', 'products.write'],
    },
  },
]

async function renderProducts() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <ProductsPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByPlaceholderText(/search by name or sku/i)).toBeInTheDocument()
  })
  return utils
}

describe('ProductsPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists products returned by the API', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/api/v1/products?',
        json: [
          {
            id: 1,
            store_id: 7,
            sku: 'SKU-1',
            name: 'Bread',
            current_price: '2.50',
            current_qty_on_hand: '20.000',
            is_active: true,
          },
        ],
      },
    ])
    await renderProducts()

    await waitFor(() => {
      expect(screen.getByText('Bread')).toBeInTheDocument()
    })
    expect(screen.getByText('SKU-1')).toBeInTheDocument()
  })

  it('searches by typing into the search box (debounced)', async () => {
    let lastUrl: string | null = null
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
          return {
            ok: true,
            status: 200,
            json: async () => AUTH_ROUTES[1].json,
          } as Response
        }
        if (url.includes('/api/v1/products')) {
          lastUrl = url
          return { ok: true, status: 200, json: async () => [] } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    await renderProducts()

    fireEvent.change(screen.getByPlaceholderText(/search by name or sku/i), {
      target: { value: 'milk' },
    })

    await waitFor(
      () => {
        expect(lastUrl).toContain('search=milk')
      },
      { timeout: 1000 },
    )
  })

  it('asks for confirmation before deactivating a product', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/api/v1/products?',
        json: [
          {
            id: 1,
            store_id: 7,
            sku: 'SKU-1',
            name: 'Bread',
            current_price: '2.50',
            current_qty_on_hand: '20.000',
            is_active: true,
          },
        ],
      },
    ])
    await renderProducts()

    await waitFor(() => expect(screen.getByText('Bread')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /deactivate/i }))

    expect(confirmSpy).toHaveBeenCalled()
    // User declined the confirmation, so the product is still shown as active.
    expect(screen.getByText('Active')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })
})
