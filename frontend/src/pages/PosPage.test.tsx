import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { PosPage } from './PosPage'

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
      username: 'cashier',
      full_name: 'Cash Ier',
      store_id: 7,
      permissions: ['pos.use', 'products.read'],
    },
  },
]

async function renderPos() {
  const utils = render(
    <AuthProvider>
      <PosPage />
    </AuthProvider>,
  )
  // Wait for the silent-refresh + /auth/me chain to resolve so
  // user.store_id is available before a test scans/checks out.
  await waitFor(() => {
    expect(screen.getByPlaceholderText(/ready to scan/i)).toBeInTheDocument()
  })
  return utils
}

describe('PosPage', () => {
  afterEach(() => {
    setAccessToken(null)
    vi.unstubAllGlobals()
  })

  it('adds a scanned product to the cart', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/111',
        json: {
          id: 5,
          store_id: 7,
          sku: 'SKU-5',
          name: 'Scanned Widget',
          current_price: '9.99',
          current_cost: '5.000000',
          current_qty_on_hand: '10.000',
          is_active: true,
          allow_negative_stock: false,
        },
      },
    ])
    await renderPos()

    const scanInput = screen.getByPlaceholderText(/ready to scan/i)
    fireEvent.change(scanInput, { target: { value: '111' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })

    await waitFor(() => {
      expect(screen.getByText('Scanned Widget')).toBeInTheDocument()
    })
    // Both the cart line and the subtotal show 9.99 (qty 1 * unit price).
    expect(screen.getAllByText('9.99').length).toBeGreaterThanOrEqual(2)
  })

  it('shows an error for an unknown barcode without adding a line', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/999',
        status: 404,
        json: { error: { code: 'UNKNOWN_BARCODE', message: 'not found' } },
      },
    ])
    await renderPos()

    const scanInput = screen.getByPlaceholderText(/ready to scan/i)
    fireEvent.change(scanInput, { target: { value: '999' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })

    await waitFor(() => {
      expect(screen.getByText(/no product found for barcode "999"/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/cart is empty/i)).toBeInTheDocument()
  })

  it('increments quantity when the same barcode is scanned twice', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/222',
        json: {
          id: 8,
          store_id: 7,
          sku: 'SKU-8',
          name: 'Repeat Item',
          current_price: '2.00',
          current_cost: '1.000000',
          current_qty_on_hand: '50.000',
          is_active: true,
          allow_negative_stock: false,
        },
      },
    ])
    await renderPos()
    const scanInput = screen.getByPlaceholderText(/ready to scan/i)

    fireEvent.change(scanInput, { target: { value: '222' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Repeat Item')).toBeInTheDocument())

    fireEvent.change(scanInput, { target: { value: '222' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })

    await waitFor(() => {
      const qtyInput = screen.getByDisplayValue('2')
      expect(qtyInput).toBeInTheDocument()
    })
  })

  it('completes checkout and shows a receipt', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/333',
        json: {
          id: 9,
          store_id: 7,
          sku: 'SKU-9',
          name: 'Checkout Item',
          current_price: '5.00',
          current_cost: '2.000000',
          current_qty_on_hand: '50.000',
          is_active: true,
          allow_negative_stock: false,
        },
      },
      {
        method: 'POST',
        path: '/api/v1/sales',
        status: 201,
        json: {
          id: 42,
          store_id: 7,
          sale_number: 'S7-TEST',
          cashier_id: 1,
          status: 'COMPLETED',
          subtotal: '5.00',
          discount_total: '0.00',
          tax_total: '0.00',
          grand_total: '5.00',
          amount_tendered: '5.00',
          change_due: '0.00',
          completed_at: '2026-01-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
          items: [
            {
              id: 1,
              product_id: 9,
              quantity: '1.000',
              unit_price_at_sale: '5.00',
              unit_cost_at_sale: '2.000000',
              discount_amount: '0.00',
              tax_rate_id: null,
              tax_amount: '0.00',
              line_total: '5.00',
              product_name: 'Checkout Item',
              product_sku: 'SKU-9',
            },
          ],
          payments: [{ id: 1, payment_method: 'CASH', amount: '5.00', reference: null }],
        },
      },
    ])
    await renderPos()
    const scanInput = screen.getByPlaceholderText(/ready to scan/i)
    fireEvent.change(scanInput, { target: { value: '333' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Checkout Item')).toBeInTheDocument())

    const amountInputs = screen.getAllByPlaceholderText('Amount')
    fireEvent.change(amountInputs[0], { target: { value: '5.00' } })
    fireEvent.click(screen.getByRole('button', { name: /complete sale/i }))

    await waitFor(() => {
      expect(screen.getByText('RECEIPT')).toBeInTheDocument()
    })
    expect(screen.getByText('S7-TEST')).toBeInTheDocument()
  })

  it('recalculates the subtotal when the cart quantity is edited', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/555',
        json: {
          id: 12,
          store_id: 7,
          sku: 'SKU-12',
          name: 'Qty Item',
          current_price: '3.00',
          current_cost: '1.000000',
          current_qty_on_hand: '50.000',
          is_active: true,
          allow_negative_stock: false,
        },
      },
    ])
    await renderPos()
    const scanInput = screen.getByPlaceholderText(/ready to scan/i)
    fireEvent.change(scanInput, { target: { value: '555' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Qty Item')).toBeInTheDocument())

    const qtyInput = screen.getByDisplayValue('1')
    fireEvent.change(qtyInput, { target: { value: '4' } })

    await waitFor(() => {
      // 4 * 3.00 = 12.00, shown as the subtotal.
      expect(screen.getByText('12.00')).toBeInTheDocument()
    })
  })

  it('shows an insufficient-stock error and keeps the cart on failed checkout', async () => {
    mockFetchRoutes([
      ...AUTH_ROUTES,
      {
        path: '/products/barcode/444',
        json: {
          id: 11,
          store_id: 7,
          sku: 'SKU-11',
          name: 'Scarce Item',
          current_price: '1.00',
          current_cost: '0.500000',
          current_qty_on_hand: '1.000',
          is_active: true,
          allow_negative_stock: false,
        },
      },
      {
        method: 'POST',
        path: '/api/v1/sales',
        status: 409,
        json: { error: { code: 'INSUFFICIENT_STOCK', message: 'Insufficient stock' } },
      },
    ])
    await renderPos()
    const scanInput = screen.getByPlaceholderText(/ready to scan/i)
    fireEvent.change(scanInput, { target: { value: '444' } })
    fireEvent.keyDown(scanInput, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Scarce Item')).toBeInTheDocument())

    const amountInputs = screen.getAllByPlaceholderText('Amount')
    fireEvent.change(amountInputs[0], { target: { value: '100.00' } })
    fireEvent.click(screen.getByRole('button', { name: /complete sale/i }))

    await waitFor(() => {
      expect(screen.getByText(/insufficient stock/i)).toBeInTheDocument()
    })
    // The cart line is still there — a failed checkout doesn't clear it.
    expect(screen.getByText('Scarce Item')).toBeInTheDocument()
  })
})
