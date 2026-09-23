import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { SupplyChainPage } from './SupplyChainPage'

function authRoutes(permissions: string[]) {
  return [
    {
      method: 'POST',
      path: '/auth/refresh',
      json: { access_token: 'tok', token_type: 'bearer', expires_in_seconds: 900 },
    },
    {
      path: '/auth/me',
      json: {
        id: 1,
        username: 'planner',
        full_name: 'Supply Chain Planner',
        store_id: 7,
        permissions,
      },
    },
  ]
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderSupplyChain() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <SupplyChainPage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Supply Chain' })).toBeInTheDocument()
    expect(screen.queryByText(/loading/i)).not.toBeInTheDocument()
  })
  return utils
}

const SAMPLE_PLAN = {
  id: 501,
  generation_batch_id: 'batch-1',
  destination_store_id: 7,
  product_id: 55,
  needed_quantity: '20.000',
  suggested_quantity: '30.000',
  source_type: 'SUPPLIER' as const,
  supplier_id: 1,
  source_store_id: null,
  suggested_unit_cost: '5.000000',
  urgency: 'NORMAL' as const,
  reason: 'Below reorder point',
  status: 'RECOMMENDED' as const,
  approved_by: null,
  approved_at: null,
  executed_by: null,
  executed_at: null,
  executed_quantity: null,
  generated_purchase_order_id: null,
  generated_transfer_id: null,
  cancelled_by: null,
  cancelled_at: null,
  cancellation_reason: null,
  stale_reason: null,
  created_at: '2024-01-01T00:00:00Z',
}

const SAMPLE_PLAN_DETAIL = {
  ...SAMPLE_PLAN,
  remaining_need: '20.000',
  fulfilled: false,
}

const NO_METRICS = {
  method: 'GET',
  path: '/api/v1/replenishment/metrics',
  json: {
    store_id: 7,
    open_purchase_order_qty: '0',
    inbound_transfer_qty: '0',
    products_below_reorder_point: 0,
    products_below_minimum: 0,
    overdue_purchase_order_count: 0,
  },
}

const NO_EXCEPTIONS = { method: 'GET', path: '/api/v1/replenishment/exceptions', json: [] }

describe('SupplyChainPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('lists replenishment plans', async () => {
    mockFetchRoutes([
      ...authRoutes(['supply_chain.plan', 'supply_chain.approve', 'supply_chain.execute']),
      { method: 'GET', path: '/api/v1/replenishment/plans', json: [SAMPLE_PLAN] },
      NO_METRICS,
      NO_EXCEPTIONS,
    ])
    await renderSupplyChain()

    expect(screen.getByText(/#501/)).toBeInTheDocument()
    expect(screen.getByText('RECOMMENDED')).toBeInTheDocument()
  })

  it('shows the empty state when there are no plans', async () => {
    mockFetchRoutes([
      ...authRoutes(['supply_chain.plan']),
      { method: 'GET', path: '/api/v1/replenishment/plans', json: [] },
      NO_METRICS,
      NO_EXCEPTIONS,
    ])
    await renderSupplyChain()

    expect(screen.getByText('No replenishment plans yet.')).toBeInTheDocument()
  })

  it('shows a load error when the plans request fails', async () => {
    mockFetchRoutes([
      ...authRoutes(['supply_chain.plan']),
      { method: 'GET', path: '/api/v1/replenishment/plans', status: 500, json: {} },
      NO_METRICS,
      NO_EXCEPTIONS,
    ])
    await renderSupplyChain()

    expect(screen.getByText('Could not load replenishment plans.')).toBeInTheDocument()
  })

  it('hides the Approve/Execute/Cancel buttons for a user without those permissions', async () => {
    mockFetchRoutes([
      ...authRoutes([]),
      { method: 'GET', path: '/api/v1/replenishment/plans/501', json: SAMPLE_PLAN_DETAIL },
      { method: 'GET', path: '/api/v1/replenishment/plans', json: [SAMPLE_PLAN] },
      NO_METRICS,
      NO_EXCEPTIONS,
    ])
    await renderSupplyChain()

    expect(screen.queryByRole('button', { name: /generate recommendations/i })).toBeNull()
    fireEvent.click(screen.getByText(/#501/))
    await waitFor(() => expect(screen.getByText('Plan #501')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^cancel$/i })).toBeNull()
  })

  it('approves a recommended plan', async () => {
    const approvedPlan = { ...SAMPLE_PLAN, status: 'APPROVED' as const }
    const approvedDetail = { ...SAMPLE_PLAN_DETAIL, status: 'APPROVED' as const }
    let approved = false
    // mockFetchRoutes doesn't support responses that change across calls,
    // so use a manual fetch stub for this state-transition test, mirroring
    // PurchasingPage.test.tsx's "opens a purchase order detail view and
    // submits it" test.
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
            json: async () => ({
              id: 1,
              username: 'planner',
              full_name: 'Supply Chain Planner',
              store_id: 7,
              permissions: ['supply_chain.plan', 'supply_chain.approve'],
            }),
          } as Response
        }
        if (url.includes('/plans/501/approve') && method === 'POST') {
          approved = true
          return { ok: true, status: 200, json: async () => approvedPlan } as Response
        }
        if (url.includes('/plans/501') && method === 'GET') {
          return {
            ok: true,
            status: 200,
            json: async () => (approved ? approvedDetail : SAMPLE_PLAN_DETAIL),
          } as Response
        }
        if (url.includes('/replenishment/plans') && method === 'GET') {
          return { ok: true, status: 200, json: async () => [approved ? approvedPlan : SAMPLE_PLAN] } as Response
        }
        if (url.includes('/replenishment/metrics')) {
          return { ok: true, status: 200, json: async () => NO_METRICS.json } as Response
        }
        if (url.includes('/replenishment/exceptions')) {
          return { ok: true, status: 200, json: async () => [] } as Response
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )

    await renderSupplyChain()
    fireEvent.click(screen.getByText(/#501/))
    await waitFor(() => expect(screen.getByText('Plan #501')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    // Both the list row's badge and the detail panel's badge now say
    // APPROVED after the refetch -- assert on the button transition
    // instead of a text match that's ambiguous once there are two badges.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull(),
    )
    expect(screen.getAllByText('APPROVED').length).toBeGreaterThan(0)
  })
})
