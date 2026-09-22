import { useEffect, useRef, useState } from 'react'
import * as productsApi from '../api/products'
import * as salesApi from '../api/sales'
import * as shiftsApi from '../api/shifts'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

interface CartLine {
  product: productsApi.Product
  quantity: string
  discountAmount: string
}

interface PaymentLine {
  method: salesApi.PaymentInput['payment_method']
  amount: string
}

const PAYMENT_METHODS: salesApi.PaymentInput['payment_method'][] = [
  'CASH',
  'CARD',
  'MOBILE_MONEY',
  'BANK_TRANSFER',
  'OTHER',
]

function cartSubtotal(cart: CartLine[]): number {
  return cart.reduce((sum, line) => {
    const qty = Number(line.quantity) || 0
    const price = Number(line.product.current_price) || 0
    const discount = Number(line.discountAmount) || 0
    return sum + qty * price - discount
  }, 0)
}

export function PosPage() {
  const { user, hasPermission } = useAuth()
  const scannerInputRef = useRef<HTMLInputElement>(null)
  // Idempotency key for the checkout currently being built (M2 hardening
  // audit Section 7): generated once when the cart first gets a line, and
  // reused as-is on every retry of "Complete sale" for that same cart —
  // a double-click or a retry after a dropped network response resends
  // the SAME key, so the server can recognize it as the same attempt
  // instead of creating a second sale. Cleared once the sale succeeds (a
  // new cart gets a new key). A ref, not state: it must never trigger a
  // re-render and must survive across renders without being reset.
  const checkoutKeyRef = useRef<string | null>(null)
  const [scanValue, setScanValue] = useState('')
  const [cart, setCart] = useState<CartLine[]>([])
  const [payments, setPayments] = useState<PaymentLine[]>([{ method: 'CASH', amount: '' }])
  const [scanError, setScanError] = useState<string | null>(null)
  const [checkoutError, setCheckoutError] = useState<string | null>(null)
  const [isCheckingOut, setIsCheckingOut] = useState(false)
  const [completedSale, setCompletedSale] = useState<salesApi.Sale | null>(null)
  const [searchTerm, setSearchTerm] = useState('')
  const [searchResults, setSearchResults] = useState<productsApi.Product[]>([])
  // M15: the cashier's own active till session, if any. Purely
  // informational/operational from the frontend's point of view — the
  // backend attributes a cash sale to it opportunistically and never
  // blocks checkout on its absence (docs/M15_DESIGN.md "Scope boundary").
  const [activeShift, setActiveShift] = useState<shiftsApi.Shift | null>(null)
  const [shiftLoaded, setShiftLoaded] = useState(false)

  async function refreshActiveShift() {
    try {
      const shift = await shiftsApi.getActiveShift()
      setActiveShift(shift)
    } catch {
      setActiveShift(null)
    } finally {
      setShiftLoaded(true)
    }
  }

  useEffect(() => {
    scannerInputRef.current?.focus()
  }, [])

  useEffect(() => {
    // AuthProvider resolves `user` asynchronously (a silent-refresh +
    // /auth/me round trip), so this must re-run once it does rather than
    // reading a stale "not logged in yet" permission set on first mount.
    if (!user) return
    if (hasPermission('shift.manage')) {
      refreshActiveShift()
    } else {
      setShiftLoaded(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  function addToCart(product: productsApi.Product) {
    setCart((prev) => {
      const existing = prev.find((line) => line.product.id === product.id)
      if (existing) {
        return prev.map((line) =>
          line.product.id === product.id
            ? { ...line, quantity: String(Number(line.quantity) + 1) }
            : line,
        )
      }
      return [...prev, { product, quantity: '1', discountAmount: '0' }]
    })
  }

  async function handleScanSubmit(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'Enter') return
    const code = scanValue.trim()
    setScanValue('')
    if (!code) return
    setScanError(null)
    try {
      const product = await productsApi.getProductByBarcode(code)
      addToCart(product)
    } catch (err) {
      setScanError(
        err instanceof ApiError && err.code === 'UNKNOWN_BARCODE'
          ? `No product found for barcode "${code}"`
          : 'Barcode lookup failed.',
      )
    }
  }

  async function handleSearch(term: string) {
    setSearchTerm(term)
    if (term.trim().length < 2) {
      setSearchResults([])
      return
    }
    try {
      const results = await productsApi.listProducts({
        store_id: user?.store_id ?? undefined,
        search: term,
        is_active: true,
      })
      setSearchResults(results)
    } catch {
      setSearchResults([])
    }
  }

  function updateLine(productId: number, field: 'quantity' | 'discountAmount', value: string) {
    setCart((prev) =>
      prev.map((line) => (line.product.id === productId ? { ...line, [field]: value } : line)),
    )
  }

  function removeLine(productId: number) {
    setCart((prev) => prev.filter((line) => line.product.id !== productId))
  }

  function updatePayment(index: number, field: keyof PaymentLine, value: string) {
    setPayments((prev) => prev.map((p, i) => (i === index ? { ...p, [field]: value } : p)))
  }

  function addPaymentLine() {
    setPayments((prev) => [...prev, { method: 'CASH', amount: '' }])
  }

  function removePaymentLine(index: number) {
    setPayments((prev) => prev.filter((_, i) => i !== index))
  }

  async function handleCheckout() {
    if (!user?.store_id) {
      setCheckoutError('Your account has no assigned store.')
      return
    }
    // Reused on every retry of this same attempt (see the ref's own
    // comment) — only a successful sale or an explicit new cart clears it.
    if (checkoutKeyRef.current === null) {
      checkoutKeyRef.current = crypto.randomUUID()
    }
    setCheckoutError(null)
    setIsCheckingOut(true)
    try {
      const sale = await salesApi.finalizeSale({
        store_id: user.store_id,
        client_transaction_id: checkoutKeyRef.current,
        lines: cart.map((line) => ({
          product_id: line.product.id,
          quantity: line.quantity,
          discount_amount: line.discountAmount || '0',
        })),
        payments: payments
          .filter((p) => p.amount.trim() !== '')
          .map((p) => ({ payment_method: p.method, amount: p.amount })),
      })
      checkoutKeyRef.current = null
      setCompletedSale(sale)
      setCart([])
      setPayments([{ method: 'CASH', amount: '' }])
    } catch (err) {
      setCheckoutError(err instanceof ApiError ? err.message : 'Checkout failed.')
    } finally {
      setIsCheckingOut(false)
      scannerInputRef.current?.focus()
    }
  }

  function startNewSale() {
    checkoutKeyRef.current = null
    setCompletedSale(null)
    scannerInputRef.current?.focus()
  }

  if (completedSale) {
    return <Receipt sale={completedSale} onNewSale={startNewSale} />
  }

  const subtotal = cartSubtotal(cart)

  return (
    <div
      onClick={(e) => {
        const target = e.target as HTMLElement
        // Stop the page from hijacking your cursor if clicking inside input or select boxes
        if (
          target.tagName === 'INPUT' ||
          target.tagName === 'SELECT' ||
          target.tagName === 'BUTTON'
        ) {
          return
        }
        scannerInputRef.current?.focus()
      }}
      className="min-h-screen" // Keep your existing styles here
    >
      <h1 className="text-2xl font-semibold text-gray-900">Point of Sale</h1>

      {hasPermission('shift.manage') && shiftLoaded && (
        <ShiftPanel
          storeId={user?.store_id ?? null}
          activeShift={activeShift}
          onShiftChanged={refreshActiveShift}
        />
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <label className="block text-sm font-medium text-gray-700">
            Scan barcode or type it and press Enter
          </label>
          <input
            ref={scannerInputRef}
            value={scanValue}
            onChange={(e) => setScanValue(e.target.value)}
            onKeyDown={handleScanSubmit}
            className="mt-1 w-full rounded border border-gray-300 px-3 py-2 font-mono text-sm focus:border-blue-500 focus:outline-none"
            placeholder="Ready to scan…"
            autoFocus
          />
          {scanError && <p className="mt-1 text-sm text-red-600">{scanError}</p>}
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700">Or search by name / SKU</label>
          <input
            value={searchTerm}
            onChange={(e) => handleSearch(e.target.value)}
            className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm"
            placeholder="Search products…"
          />
          {searchResults.length > 0 && (
            <ul className="mt-1 max-h-40 overflow-auto rounded border border-gray-200 bg-white text-sm shadow-sm">
              {searchResults.map((p) => (
                <li key={p.id}>
                  <button
                    onClick={() => {
                      addToCart(p)
                      setSearchTerm('')
                      setSearchResults([])
                      scannerInputRef.current?.focus()
                    }}
                    className="block w-full px-3 py-2 text-left hover:bg-gray-50"
                  >
                    {p.name} <span className="text-gray-400">({p.sku})</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <table className="mt-6 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Product</th>
            <th className="py-2 pr-4">Qty</th>
            <th className="py-2 pr-4">Price</th>
            <th className="py-2 pr-4">Discount</th>
            <th className="py-2 pr-4"></th>
          </tr>
        </thead>
        <tbody>
          {cart.map((line) => (
            <tr key={line.product.id} className="border-b border-gray-100">
              <td className="py-2 pr-4">
                {line.product.name}
                <div className="font-mono text-xs text-gray-400">{line.product.sku}</div>
              </td>
              <td className="py-2 pr-4">
                <input
                  value={line.quantity}
                  onChange={(e) => updateLine(line.product.id, 'quantity', e.target.value)}
                  className="w-16 rounded border border-gray-300 px-2 py-1"
                />
              </td>
              <td className="py-2 pr-4">{line.product.current_price}</td>
              <td className="py-2 pr-4">
                <input
                  value={line.discountAmount}
                  onChange={(e) => updateLine(line.product.id, 'discountAmount', e.target.value)}
                  className="w-20 rounded border border-gray-300 px-2 py-1"
                />
              </td>
              <td className="py-2 pr-4">
                <button
                  onClick={() => removeLine(line.product.id)}
                  className="text-xs text-red-600 hover:underline"
                >
                  Remove
                </button>
              </td>
            </tr>
          ))}
          {cart.length === 0 && (
            <tr>
              <td colSpan={5} className="py-6 text-center text-gray-400">
                Cart is empty — scan a barcode to begin.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="mt-4 flex justify-end">
        <div className="w-64 rounded border border-gray-200 bg-white p-4 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-500">Subtotal (before tax)</span>
            <span className="font-medium">{subtotal.toFixed(2)}</span>
          </div>
          <p className="mt-1 text-xs text-gray-400">
            Tax and the final total are calculated by the server at checkout.
          </p>
        </div>
      </div>

      <div className="mt-4 rounded border border-gray-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900">Payment</h2>
        {payments.map((payment, index) => (
          <div key={index} className="mt-2 flex items-center gap-2">
            <select
              value={payment.method}
              onChange={(e) => updatePayment(index, 'method', e.target.value)}
              className="rounded border border-gray-300 px-2 py-1.5 text-sm"
            >
              {PAYMENT_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <input
              value={payment.amount}
              onChange={(e) => updatePayment(index, 'amount', e.target.value)}
              placeholder="Amount"
              className="w-28 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            {payments.length > 1 && (
              <button
                onClick={() => removePaymentLine(index)}
                className="text-xs text-red-600 hover:underline"
              >
                Remove
              </button>
            )}
          </div>
        ))}
        <button onClick={addPaymentLine} className="mt-2 text-xs text-blue-600 hover:underline">
          + Add another payment method
        </button>

        {checkoutError && (
          <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
            {checkoutError}
          </p>
        )}

        <button
          onClick={handleCheckout}
          disabled={cart.length === 0 || isCheckingOut}
          className="mt-4 w-full rounded bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isCheckingOut ? 'Processing…' : 'Complete sale'}
        </button>
      </div>
    </div>
  )
}

function Receipt({ sale, onNewSale }: { sale: salesApi.Sale; onNewSale: () => void }) {
  return (
    <div className="max-w-md">
      <div className="rounded border border-gray-200 bg-white p-6 font-mono text-sm shadow-sm">
        <p className="text-center font-semibold">RECEIPT</p>
        <p className="text-center text-xs text-gray-500">{sale.sale_number}</p>
        <p className="text-center text-xs text-gray-500">
          {sale.completed_at && new Date(sale.completed_at).toLocaleString()}
        </p>
        <hr className="my-3" />
        {sale.items.map((item) => (
          <div key={item.id} className="mb-1 flex justify-between">
            <span>
              {item.quantity} × {item.product_name ?? `#${item.product_id}`}
            </span>
            <span>{item.line_total}</span>
          </div>
        ))}
        <hr className="my-3" />
        <div className="flex justify-between">
          <span>Subtotal</span>
          <span>{sale.subtotal}</span>
        </div>
        <div className="flex justify-between">
          <span>Discount</span>
          <span>-{sale.discount_total}</span>
        </div>
        <div className="flex justify-between">
          <span>Tax</span>
          <span>{sale.tax_total}</span>
        </div>
        <div className="flex justify-between font-semibold">
          <span>Total</span>
          <span>{sale.grand_total}</span>
        </div>
        <hr className="my-3" />
        {sale.payments.map((p) => (
          <div key={p.id} className="flex justify-between text-xs text-gray-600">
            <span>{p.payment_method}</span>
            <span>{p.amount}</span>
          </div>
        ))}
        {sale.change_due && Number(sale.change_due) > 0 && (
          <div className="mt-1 flex justify-between text-xs font-medium">
            <span>Change</span>
            <span>{sale.change_due}</span>
          </div>
        )}
      </div>
      <button
        onClick={onNewSale}
        className="mt-4 w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
      >
        New sale
      </button>
    </div>
  )
}

// --- M15: cashier till/shift session panel ---------------------------------
//
// Deliberately non-blocking: this panel lets a cashier open/close their
// own till and record cash movements, but never gates checkout on it —
// the backend attributes a cash sale to an active shift opportunistically
// (docs/M15_DESIGN.md "Scope boundary"). Every number shown after a close
// (expected cash, variance) comes straight from the server response; this
// component never computes one itself, matching the "frontend is never
// the source of financial truth" rule finalizeSale's own UI already
// follows.

function ShiftPanel({
  storeId,
  activeShift,
  onShiftChanged,
}: {
  storeId: number | null
  activeShift: shiftsApi.Shift | null
  onShiftChanged: () => void
}) {
  const [showOpenForm, setShowOpenForm] = useState(false)
  const [openingFloat, setOpeningFloat] = useState('')
  const [openError, setOpenError] = useState<string | null>(null)
  const [isOpening, setIsOpening] = useState(false)

  const [showCloseForm, setShowCloseForm] = useState(false)
  const [countedAmount, setCountedAmount] = useState('')
  const [closeError, setCloseError] = useState<string | null>(null)
  const [isClosing, setIsClosing] = useState(false)
  const [lastClosed, setLastClosed] = useState<shiftsApi.Shift | null>(null)

  const [showMovementForm, setShowMovementForm] = useState(false)
  const [movementType, setMovementType] =
    useState<shiftsApi.CashMovementInput['movement_type']>('PAID_IN')
  const [movementAmount, setMovementAmount] = useState('')
  const [movementReason, setMovementReason] = useState('')
  const [movementError, setMovementError] = useState<string | null>(null)
  const [isRecordingMovement, setIsRecordingMovement] = useState(false)

  async function handleOpenShift() {
    if (!storeId) {
      setOpenError('Your account has no assigned store.')
      return
    }
    setOpenError(null)
    setIsOpening(true)
    try {
      await shiftsApi.openShift({
        store_id: storeId,
        opening_float: openingFloat || '0',
        client_transaction_id: crypto.randomUUID(),
      })
      setShowOpenForm(false)
      setOpeningFloat('')
      onShiftChanged()
    } catch (err) {
      setOpenError(err instanceof ApiError ? err.message : 'Failed to open shift.')
    } finally {
      setIsOpening(false)
    }
  }

  async function handleCloseShift() {
    if (!activeShift) return
    setCloseError(null)
    setIsClosing(true)
    try {
      const closed = await shiftsApi.closeShift(activeShift.id, {
        closing_counted_amount: countedAmount || '0',
        client_transaction_id: crypto.randomUUID(),
      })
      setLastClosed(closed)
      setShowCloseForm(false)
      setCountedAmount('')
      onShiftChanged()
    } catch (err) {
      setCloseError(err instanceof ApiError ? err.message : 'Failed to close shift.')
    } finally {
      setIsClosing(false)
    }
  }

  async function handleRecordMovement() {
    if (!activeShift) return
    setMovementError(null)
    setIsRecordingMovement(true)
    try {
      await shiftsApi.recordCashMovement(activeShift.id, {
        movement_type: movementType,
        amount: movementAmount,
        reason: movementReason,
        client_transaction_id: crypto.randomUUID(),
      })
      setShowMovementForm(false)
      setMovementAmount('')
      setMovementReason('')
    } catch (err) {
      setMovementError(err instanceof ApiError ? err.message : 'Failed to record cash movement.')
    } finally {
      setIsRecordingMovement(false)
    }
  }

  if (lastClosed) {
    const variance = Number(lastClosed.variance_amount ?? '0')
    return (
      <div className="mt-4 rounded border border-gray-200 bg-white p-4 text-sm">
        <h2 className="font-semibold text-gray-900">Shift closed</h2>
        <div className="mt-2 flex justify-between">
          <span className="text-gray-500">Expected cash</span>
          <span className="font-medium">{lastClosed.expected_cash_amount}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Counted cash</span>
          <span className="font-medium">{lastClosed.closing_counted_amount}</span>
        </div>
        <div className="flex justify-between font-semibold">
          <span>Variance</span>
          <span
            className={
              variance === 0 ? 'text-gray-900' : variance > 0 ? 'text-green-700' : 'text-red-700'
            }
          >
            {variance === 0
              ? 'Exact'
              : variance > 0
                ? `+${variance.toFixed(2)}`
                : variance.toFixed(2)}
          </span>
        </div>
        <button
          onClick={() => setLastClosed(null)}
          className="mt-3 text-xs text-blue-600 hover:underline"
        >
          Dismiss
        </button>
      </div>
    )
  }

  if (!activeShift) {
    return (
      <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-4 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-amber-800">
            No active till session — cash sales won't be attributed to a shift.
          </span>
          {!showOpenForm && (
            <button
              onClick={() => setShowOpenForm(true)}
              className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700"
            >
              Open till
            </button>
          )}
        </div>
        {showOpenForm && (
          <div className="mt-3 flex items-center gap-2">
            <input
              value={openingFloat}
              onChange={(e) => setOpeningFloat(e.target.value)}
              placeholder="Opening float"
              className="w-32 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              onClick={handleOpenShift}
              disabled={isOpening}
              className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {isOpening ? 'Opening…' : 'Confirm'}
            </button>
            <button
              onClick={() => setShowOpenForm(false)}
              className="text-xs text-gray-600 hover:underline"
            >
              Cancel
            </button>
          </div>
        )}
        {openError && <p className="mt-2 text-xs text-red-600">{openError}</p>}
      </div>
    )
  }

  return (
    <div className="mt-4 rounded border border-green-200 bg-green-50 p-4 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-green-800">
          Till open — opening float {activeShift.opening_float}
        </span>
        <div className="flex gap-2">
          {!showMovementForm && (
            <button
              onClick={() => setShowMovementForm(true)}
              className="rounded border border-green-600 px-3 py-1.5 text-xs font-medium text-green-700 hover:bg-green-100"
            >
              Cash movement
            </button>
          )}
          {!showCloseForm && (
            <button
              onClick={() => setShowCloseForm(true)}
              className="rounded bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-800"
            >
              Close till
            </button>
          )}
        </div>
      </div>

      {showMovementForm && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <select
            value={movementType}
            onChange={(e) =>
              setMovementType(e.target.value as shiftsApi.CashMovementInput['movement_type'])
            }
            className="rounded border border-gray-300 px-2 py-1.5 text-sm"
          >
            <option value="PAID_IN">Paid in</option>
            <option value="PAID_OUT">Paid out</option>
          </select>
          <input
            value={movementAmount}
            onChange={(e) => setMovementAmount(e.target.value)}
            placeholder="Amount"
            className="w-24 rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
          <input
            value={movementReason}
            onChange={(e) => setMovementReason(e.target.value)}
            placeholder="Reason"
            className="w-40 rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
          <button
            onClick={handleRecordMovement}
            disabled={isRecordingMovement}
            className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {isRecordingMovement ? 'Saving…' : 'Confirm'}
          </button>
          <button
            onClick={() => setShowMovementForm(false)}
            className="text-xs text-gray-600 hover:underline"
          >
            Cancel
          </button>
        </div>
      )}
      {movementError && <p className="mt-2 text-xs text-red-600">{movementError}</p>}

      {showCloseForm && (
        <div className="mt-3 flex items-center gap-2">
          <input
            value={countedAmount}
            onChange={(e) => setCountedAmount(e.target.value)}
            placeholder="Counted cash"
            className="w-32 rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
          <button
            onClick={handleCloseShift}
            disabled={isClosing}
            className="rounded bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-800 disabled:opacity-50"
          >
            {isClosing ? 'Closing…' : 'Confirm close'}
          </button>
          <button
            onClick={() => setShowCloseForm(false)}
            className="text-xs text-gray-600 hover:underline"
          >
            Cancel
          </button>
        </div>
      )}
      {closeError && <p className="mt-2 text-xs text-red-600">{closeError}</p>}
    </div>
  )
}
