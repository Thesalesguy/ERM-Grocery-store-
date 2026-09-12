import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import { AccountsPayablePage } from './AccountsPayablePage'

const AUTH_ROUTES_JSON = {
  id: 1,
  username: 'manager',
  full_name: 'Store Manager',
  store_id: 7,
  permissions: ['ap.read', 'ap.write', 'ap.post', 'ap.pay', 'ap.credit'],
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isLoading } = useAuth()
  if (isLoading) return null
  return <>{children}</>
}

async function renderAp() {
  const utils = render(
    <AuthProvider>
      <AuthGate>
        <AccountsPayablePage />
      </AuthGate>
    </AuthProvider>,
  )
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'Accounts Payable' })).toBeInTheDocument()
  })
  return utils
}

const SAMPLE_PO = {
  id: 42,
  store_id: 7,
  supplier_id: 3,
  purchase_number: 'PO7-TEST',
  status: 'PARTIALLY_RECEIVED',
  order_date: '2024-01-01',
  expected_date: null,
  notes: null,
  created_by: 1,
  created_at: '2024-01-01T00:00:00Z',
  supplier_name: 'Acme Distributors',
  items: [],
}

const SAMPLE_MATCHING = {
  purchase_order_ids: [42],
  items: [
    {
      purchase_order_item_id: 100,
      purchase_order_id: 42,
      product_id: 55,
      quantity_ordered: '10.000',
      quantity_received: '10.000',
      quantity_invoiced: '0.000',
      quantity_invoiceable: '10.000',
      product_name: 'Widget',
      product_sku: 'WID-1',
    },
  ],
}

const SAMPLE_DRAFT_INVOICE = {
  id: 900,
  store_id: 7,
  supplier_id: 3,
  purchase_order_id: 42,
  invoice_number: 'INV-TEST',
  invoice_date: '2024-01-05',
  due_date: '2024-01-05',
  status: 'DRAFT',
  subtotal: '100.00',
  discount_total: '0.00',
  tax_total: '0.00',
  grand_total: '100.00',
  amount_paid: '0.00',
  amount_credited: '0.00',
  balance_due: '100.00',
  client_transaction_id: 'itxn-1',
  notes: null,
  posted_at: null,
  voided_at: null,
  created_at: '2024-01-05T00:00:00Z',
  supplier_name: 'Acme Distributors',
  lines: [
    {
      id: 1,
      purchase_order_item_id: 100,
      product_id: 55,
      description: 'Widget',
      quantity_invoiced: '10.000',
      unit_price: '10.00',
      discount_amount: '0.00',
      tax_amount: '0.00',
      line_total: '100.00',
      product_name: 'Widget',
      product_sku: 'WID-1',
      matches: [],
    },
  ],
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
        return { ok: true, status: 200, json: async () => AUTH_ROUTES_JSON } as Response
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

describe('AccountsPayablePage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('finds a PO and shows invoiceable lines', async () => {
    stubFetch((url) => {
      if (url.includes('/ap/purchase-orders/42/matching-status')) {
        return jsonResponse(200, SAMPLE_MATCHING)
      }
      if (url.includes('/purchasing/purchase-orders/42')) {
        return jsonResponse(200, SAMPLE_PO)
      }
      return null
    })
    await renderAp()

    fireEvent.change(screen.getByPlaceholderText(/Purchase Order ID/), { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: /find po/i }))

    await waitFor(() => expect(screen.getByText('PO7-TEST')).toBeInTheDocument())
    expect(screen.getByText('Widget')).toBeInTheDocument()
    expect(screen.getAllByText('10.000').length).toBeGreaterThan(0)
  })

  it('creates a draft invoice and posts it', async () => {
    let created = false
    let posted = false
    stubFetch((url, method) => {
      if (url.includes('/ap/purchase-orders/42/matching-status')) {
        return jsonResponse(200, SAMPLE_MATCHING)
      }
      if (url.includes('/purchasing/purchase-orders/42')) {
        return jsonResponse(200, SAMPLE_PO)
      }
      if (url.endsWith('/ap/invoices') && method === 'POST') {
        created = true
        return jsonResponse(201, SAMPLE_DRAFT_INVOICE)
      }
      if (url.includes('/ap/invoices/900/post') && method === 'POST') {
        posted = true
        return jsonResponse(200, {
          ...SAMPLE_DRAFT_INVOICE,
          status: 'POSTED',
          posted_at: '2024-01-06T00:00:00Z',
        })
      }
      if (url.includes('/ap/invoices/900') && method === 'GET') {
        return jsonResponse(
          200,
          posted ? { ...SAMPLE_DRAFT_INVOICE, status: 'POSTED' } : SAMPLE_DRAFT_INVOICE,
        )
      }
      return null
    })
    await renderAp()

    fireEvent.change(screen.getByPlaceholderText(/Purchase Order ID/), { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: /find po/i }))
    await waitFor(() => expect(screen.getByText('Widget')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText("Supplier's invoice number"), {
      target: { value: 'INV-TEST' },
    })
    const checkbox = screen.getByRole('checkbox')
    fireEvent.click(checkbox)
    fireEvent.change(screen.getByPlaceholderText('0'), { target: { value: '10' } })
    fireEvent.change(screen.getByPlaceholderText('0.00'), { target: { value: '10.00' } })
    fireEvent.click(screen.getByRole('button', { name: /create draft invoice/i }))

    await waitFor(() => expect(screen.getByText('INV-TEST')).toBeInTheDocument())
    expect(created).toBe(true)
    expect(screen.getByText('Draft')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /post invoice/i }))
    await waitFor(() => expect(screen.getByText('Posted')).toBeInTheDocument())
    expect(posted).toBe(true)
  })

  it('records a multi-allocation payment and issues a credit note against a posted invoice', async () => {
    let paymentBody: Record<string, unknown> | null = null
    let creditNoteBody: Record<string, unknown> | null = null
    const postedInvoice = { ...SAMPLE_DRAFT_INVOICE, status: 'POSTED' }
    stubFetch((url, method, init) => {
      if (url.includes('/ap/purchase-orders/42/matching-status')) {
        return jsonResponse(200, SAMPLE_MATCHING)
      }
      if (url.includes('/purchasing/purchase-orders/42')) {
        return jsonResponse(200, SAMPLE_PO)
      }
      if (url.endsWith('/ap/invoices') && method === 'POST') {
        return jsonResponse(201, postedInvoice)
      }
      if (url.includes('/ap/invoices/900') && method === 'GET') {
        return jsonResponse(200, postedInvoice)
      }
      if (url.endsWith('/ap/payments') && method === 'POST') {
        paymentBody = JSON.parse((init?.body as string) ?? '{}')
        return jsonResponse(201, {
          id: 1,
          store_id: 7,
          supplier_id: 3,
          payment_date: '2024-01-10',
          payment_method: 'CASH',
          amount: '55.00',
          reference: null,
          client_transaction_id: 'ptxn-1',
          created_at: '2024-01-10T00:00:00Z',
          allocations: [
            { id: 1, purchase_invoice_id: 900, amount: '40.00' },
            { id: 2, purchase_invoice_id: 901, amount: '15.00' },
          ],
        })
      }
      if (url.endsWith('/ap/credit-notes') && method === 'POST') {
        creditNoteBody = JSON.parse((init?.body as string) ?? '{}')
        return jsonResponse(201, {
          id: 5,
          store_id: 7,
          supplier_id: 3,
          credit_number: 'CN-1',
          credit_date: '2024-01-12',
          reason: 'COMMERCIAL_DISCOUNT',
          purchase_return_id: null,
          grand_total: '10.00',
          amount_allocated: '10.00',
          client_transaction_id: 'ctxn-1',
          notes: null,
          created_at: '2024-01-12T00:00:00Z',
          voided_at: null,
          supplier_name: 'Acme Distributors',
          lines: [],
          allocations: [],
        })
      }
      return null
    })
    await renderAp()

    fireEvent.change(screen.getByPlaceholderText(/Purchase Order ID/), { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: /find po/i }))
    await waitFor(() => expect(screen.getByText('Widget')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText("Supplier's invoice number"), {
      target: { value: 'INV-TEST' },
    })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByPlaceholderText('0'), { target: { value: '10' } })
    fireEvent.change(screen.getByPlaceholderText('0.00'), { target: { value: '10.00' } })
    fireEvent.click(screen.getByRole('button', { name: /create draft invoice/i }))
    await waitFor(() => expect(screen.getByText('Posted')).toBeInTheDocument())

    // Record a payment allocating to this invoice AND another invoice.
    fireEvent.change(screen.getByPlaceholderText('Amount for this invoice'), {
      target: { value: '40.00' },
    })
    fireEvent.click(screen.getByText('+ Allocate to another invoice'))
    fireEvent.change(screen.getByPlaceholderText('Invoice ID'), { target: { value: '901' } })
    // Two "Amount" fields exist simultaneously: the new allocation row's,
    // and the credit-note section's own — the allocation row renders first.
    fireEvent.change(screen.getAllByPlaceholderText('Amount')[0], { target: { value: '15.00' } })
    fireEvent.click(screen.getByRole('button', { name: /record payment/i }))

    await waitFor(() => expect(paymentBody).not.toBeNull())
    expect(paymentBody).toMatchObject({
      amount: '55.00',
      allocations: [
        { purchase_invoice_id: 900, amount: '40.00' },
        { purchase_invoice_id: 901, amount: '15.00' },
      ],
    })

    // Issue a credit note against the same invoice.
    fireEvent.change(screen.getByPlaceholderText('Credit note number'), {
      target: { value: 'CN-1' },
    })
    fireEvent.change(screen.getAllByPlaceholderText('Amount')[0], { target: { value: '10.00' } })
    fireEvent.click(screen.getByRole('button', { name: /create credit note/i }))

    await waitFor(() => expect(creditNoteBody).not.toBeNull())
    expect(creditNoteBody).toMatchObject({
      reason: 'COMMERCIAL_DISCOUNT',
      allocations: [{ purchase_invoice_id: 900, amount: '10.00' }],
    })
  })

  it('does not show write controls for a read-only user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        const url = String(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        if (url.includes('/auth/refresh') && method === 'POST') {
          return jsonResponse(200, {
            access_token: 'tok',
            token_type: 'bearer',
            expires_in_seconds: 900,
          })
        }
        if (url.includes('/auth/me')) {
          return jsonResponse(200, {
            id: 2,
            username: 'auditor',
            full_name: 'Auditor',
            store_id: null,
            permissions: ['ap.read'],
          })
        }
        throw new Error(`no route for ${method} ${url}`)
      }),
    )
    await renderAp()
    expect(screen.queryByPlaceholderText("Supplier's invoice number")).not.toBeInTheDocument()
  })
})
