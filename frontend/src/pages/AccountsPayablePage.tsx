import { useEffect, useRef, useState } from 'react'
import * as apApi from '../api/ap'
import { ApiError } from '../api/client'
import * as purchasingApi from '../api/purchasing'
import { useAuth } from '../auth/AuthContext'

/** Quantity/price the operator is entering for one invoiceable PO line —
 * purely UI state. The invoice's actual dollar totals (subtotal, tax,
 * grand_total, and — at posting time — the matched clearing amount and
 * any price variance) are always computed server-side from this and the
 * original receipt data, never here. */
interface InvoiceLineDraft {
  quantity: string
  unitPrice: string
  include: boolean
}

const STATUS_LABEL: Record<string, string> = {
  DRAFT: 'Draft',
  POSTED: 'Posted',
  PARTIALLY_PAID: 'Partially Paid',
  PAID: 'Paid',
  VOIDED: 'Voided',
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === 'PAID'
      ? 'bg-green-100 text-green-800'
      : status === 'VOIDED'
        ? 'bg-red-100 text-red-800'
        : status === 'DRAFT'
          ? 'bg-gray-100 text-gray-700'
          : 'bg-blue-100 text-blue-800'
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${color}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function InvoiceDetail({ invoiceId, onBack }: { invoiceId: number; onBack: () => void }) {
  const { user, hasPermission } = useAuth()
  const [invoice, setInvoice] = useState<apApi.PurchaseInvoice | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [paymentAmount, setPaymentAmount] = useState('')
  const [paymentMethod, setPaymentMethod] =
    useState<apApi.SupplierPaymentCreateInput['payment_method']>('CASH')
  const idempotencyKeyRef = useRef<string | null>(null)

  const canPost = hasPermission('ap.post')
  const canPay = hasPermission('ap.pay')

  async function load() {
    try {
      setInvoice(await apApi.getInvoice(invoiceId))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load this invoice.')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invoiceId])

  async function handlePost() {
    setError(null)
    setIsSubmitting(true)
    try {
      await apApi.postInvoice(invoiceId)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to post the invoice.')
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handlePay() {
    if (!user?.store_id || !invoice) return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setError(null)
    setIsSubmitting(true)
    try {
      await apApi.createPayment(invoiceId, {
        store_id: user.store_id,
        payment_date: new Date().toISOString().slice(0, 10),
        payment_method: paymentMethod,
        amount: paymentAmount,
        client_transaction_id: idempotencyKeyRef.current,
      })
      idempotencyKeyRef.current = null
      setPaymentAmount('')
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to record the payment.')
    } finally {
      setIsSubmitting(false)
    }
  }

  if (error && !invoice) {
    return (
      <div>
        <p className="text-sm text-red-600">{error}</p>
        <button onClick={onBack} className="mt-2 text-sm text-blue-600 hover:underline">
          ← Back
        </button>
      </div>
    )
  }
  if (!invoice) return <p className="text-gray-500">Loading…</p>

  return (
    <div>
      <button onClick={onBack} className="text-sm text-blue-600 hover:underline">
        ← Back to invoices
      </button>
      <div className="mt-2 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">{invoice.invoice_number}</h1>
          <p className="text-sm text-gray-500">
            {invoice.supplier_name} · Invoice date {invoice.invoice_date} · Due {invoice.due_date}
          </p>
        </div>
        <StatusBadge status={invoice.status} />
      </div>

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-2 pr-4">Product</th>
            <th className="py-2 pr-4">Qty</th>
            <th className="py-2 pr-4">Unit price</th>
            <th className="py-2 pr-4">Line total</th>
          </tr>
        </thead>
        <tbody>
          {invoice.lines.map((line) => (
            <tr key={line.id} className="border-b border-gray-100">
              <td className="py-2 pr-4">
                {line.product_name ?? `#${line.product_id}`}
                <div className="font-mono text-xs text-gray-400">{line.product_sku}</div>
              </td>
              <td className="py-2 pr-4">{line.quantity_invoiced}</td>
              <td className="py-2 pr-4">{line.unit_price}</td>
              <td className="py-2 pr-4">{line.line_total}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-4 flex justify-end">
        <div className="w-72 rounded border border-gray-200 bg-white p-4 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-500">Grand total</span>
            <span className="font-medium">{invoice.grand_total}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Amount paid</span>
            <span>{invoice.amount_paid}</span>
          </div>
          <div className="mt-1 flex justify-between border-t border-gray-200 pt-1 font-semibold">
            <span>Balance due</span>
            <span>{invoice.balance_due}</span>
          </div>
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {invoice.status === 'DRAFT' && canPost && (
        <button
          onClick={handlePost}
          disabled={isSubmitting}
          className="mt-4 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {isSubmitting ? 'Posting…' : 'Post invoice'}
        </button>
      )}

      {(invoice.status === 'POSTED' || invoice.status === 'PARTIALLY_PAID') && canPay && (
        <div className="mt-4 rounded border border-gray-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Record a payment</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select
              value={paymentMethod}
              onChange={(e) =>
                setPaymentMethod(
                  e.target.value as apApi.SupplierPaymentCreateInput['payment_method'],
                )
              }
              className="rounded border border-gray-300 px-2 py-1.5 text-sm"
            >
              {(['CASH', 'BANK_TRANSFER', 'CHEQUE', 'OTHER'] as const).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <input
              value={paymentAmount}
              onChange={(e) => setPaymentAmount(e.target.value)}
              placeholder="Amount"
              className="w-32 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              onClick={handlePay}
              disabled={isSubmitting || !paymentAmount}
              className="rounded bg-green-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isSubmitting ? 'Recording…' : 'Record payment'}
            </button>
          </div>
          <p className="mt-2 text-xs text-gray-400">
            The remaining balance is calculated by the server — it is never entered manually.
          </p>
        </div>
      )}
    </div>
  )
}

function NewInvoiceForm({
  purchaseOrder,
  matching,
  onCreated,
}: {
  purchaseOrder: purchasingApi.PurchaseOrder
  matching: apApi.PurchaseOrderMatchingStatus
  onCreated: (invoice: apApi.PurchaseInvoice) => void
}) {
  const [invoiceNumber, setInvoiceNumber] = useState('')
  const [invoiceDate, setInvoiceDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [drafts, setDrafts] = useState<Record<number, InvoiceLineDraft>>({})
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const idempotencyKeyRef = useRef<string | null>(null)

  const invoiceableItems = matching.items.filter((item) => Number(item.quantity_invoiceable) > 0)

  function updateDraft(itemId: number, field: keyof InvoiceLineDraft, value: string | boolean) {
    setDrafts((prev) => ({
      ...prev,
      [itemId]: {
        quantity: prev[itemId]?.quantity ?? '',
        unitPrice: prev[itemId]?.unitPrice ?? '',
        include: prev[itemId]?.include ?? false,
        [field]: value,
      },
    }))
  }

  const selectedLines = Object.entries(drafts)
    .map(([itemId, draft]) => ({ itemId: Number(itemId), draft }))
    .filter(
      ({ draft }) => draft.include && draft.quantity.trim() !== '' && draft.unitPrice.trim() !== '',
    )

  async function handleSubmit() {
    if (!invoiceNumber.trim() || selectedLines.length === 0) return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setError(null)
    setIsSubmitting(true)
    try {
      const invoice = await apApi.createInvoice({
        store_id: purchaseOrder.store_id,
        supplier_id: purchaseOrder.supplier_id,
        purchase_order_id: purchaseOrder.id,
        invoice_number: invoiceNumber,
        invoice_date: invoiceDate,
        client_transaction_id: idempotencyKeyRef.current,
        lines: selectedLines.map(({ itemId, draft }) => ({
          purchase_order_item_id: itemId,
          quantity_invoiced: draft.quantity,
          unit_price: draft.unitPrice,
        })),
      })
      idempotencyKeyRef.current = null
      onCreated(invoice)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create the invoice.')
    } finally {
      setIsSubmitting(false)
    }
  }

  if (invoiceableItems.length === 0) {
    return <p className="mt-4 text-sm text-gray-500">Nothing on this PO is invoiceable yet.</p>
  }

  return (
    <div className="mt-4 rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Create supplier invoice</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <input
          value={invoiceNumber}
          onChange={(e) => setInvoiceNumber(e.target.value)}
          placeholder="Supplier's invoice number"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <input
          type="date"
          value={invoiceDate}
          onChange={(e) => setInvoiceDate(e.target.value)}
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
      </div>

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="text-gray-500">
            <th className="py-1 pr-2"></th>
            <th className="py-1 pr-2">Product</th>
            <th className="py-1 pr-2">Ordered</th>
            <th className="py-1 pr-2">Received</th>
            <th className="py-1 pr-2">Invoiced</th>
            <th className="py-1 pr-2">Invoiceable</th>
            <th className="py-1 pr-2">Qty to invoice</th>
            <th className="py-1 pr-2">Unit price</th>
          </tr>
        </thead>
        <tbody>
          {invoiceableItems.map((item) => (
            <tr key={item.purchase_order_item_id}>
              <td className="py-1 pr-2">
                <input
                  type="checkbox"
                  checked={drafts[item.purchase_order_item_id]?.include ?? false}
                  onChange={(e) =>
                    updateDraft(item.purchase_order_item_id, 'include', e.target.checked)
                  }
                />
              </td>
              <td className="py-1 pr-2">
                {item.product_name ?? `#${item.product_id}`}
                <div className="font-mono text-xs text-gray-400">{item.product_sku}</div>
              </td>
              <td className="py-1 pr-2">{item.quantity_ordered}</td>
              <td className="py-1 pr-2">{item.quantity_received}</td>
              <td className="py-1 pr-2">{item.quantity_invoiced}</td>
              <td className="py-1 pr-2 font-medium">{item.quantity_invoiceable}</td>
              <td className="py-1 pr-2">
                <input
                  value={drafts[item.purchase_order_item_id]?.quantity ?? ''}
                  onChange={(e) =>
                    updateDraft(item.purchase_order_item_id, 'quantity', e.target.value)
                  }
                  placeholder="0"
                  className="w-20 rounded border border-gray-300 px-2 py-1"
                />
              </td>
              <td className="py-1 pr-2">
                <input
                  value={drafts[item.purchase_order_item_id]?.unitPrice ?? ''}
                  onChange={(e) =>
                    updateDraft(item.purchase_order_item_id, 'unitPrice', e.target.value)
                  }
                  placeholder="0.00"
                  className="w-24 rounded border border-gray-300 px-2 py-1"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {error && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      <button
        onClick={handleSubmit}
        disabled={isSubmitting || !invoiceNumber.trim() || selectedLines.length === 0}
        className="mt-4 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {isSubmitting ? 'Creating…' : 'Create draft invoice'}
      </button>
      <p className="mt-2 text-xs text-gray-400">
        Line totals, tax, and the invoice grand total are calculated by the server from what you
        enter here — never displayed as an estimate before the server confirms them.
      </p>
    </div>
  )
}

function PurchaseOrderWorkflow({
  purchaseOrderId,
  onReset,
}: {
  purchaseOrderId: number
  onReset: () => void
}) {
  const [purchaseOrder, setPurchaseOrder] = useState<purchasingApi.PurchaseOrder | null>(null)
  const [matching, setMatching] = useState<apApi.PurchaseOrderMatchingStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeInvoiceId, setActiveInvoiceId] = useState<number | null>(null)

  async function load() {
    setLoadError(null)
    try {
      const [po, matchStatus] = await Promise.all([
        purchasingApi.getPurchaseOrder(purchaseOrderId),
        apApi.getMatchingStatus(purchaseOrderId),
      ])
      setPurchaseOrder(po)
      setMatching(matchStatus)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : 'Could not load this purchase order.')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [purchaseOrderId])

  if (activeInvoiceId !== null) {
    return (
      <InvoiceDetail
        invoiceId={activeInvoiceId}
        onBack={() => {
          setActiveInvoiceId(null)
          load()
        }}
      />
    )
  }

  if (loadError) {
    return (
      <div>
        <p className="text-sm text-red-600">{loadError}</p>
        <button onClick={onReset} className="mt-2 text-sm text-blue-600 hover:underline">
          ← Back
        </button>
      </div>
    )
  }
  if (!purchaseOrder || !matching) return <p className="text-gray-500">Loading…</p>

  return (
    <div>
      <button onClick={onReset} className="text-sm text-blue-600 hover:underline">
        ← Find another purchase order
      </button>
      <h1 className="mt-2 text-xl font-semibold text-gray-900">{purchaseOrder.purchase_number}</h1>
      <p className="text-sm text-gray-500">{purchaseOrder.supplier_name}</p>

      <NewInvoiceForm
        purchaseOrder={purchaseOrder}
        matching={matching}
        onCreated={(invoice) => setActiveInvoiceId(invoice.id)}
      />
    </div>
  )
}

export function AccountsPayablePage() {
  const { hasPermission } = useAuth()
  const [poIdInput, setPoIdInput] = useState('')
  const [activePoId, setActivePoId] = useState<number | null>(null)

  const canRead = hasPermission('ap.read')
  if (!canRead) {
    return (
      <div>
        <h1 className="text-2xl font-semibold text-gray-900">Accounts Payable</h1>
        <p className="mt-4 text-sm text-gray-500">
          Your account does not have permission to view Accounts Payable.
        </p>
      </div>
    )
  }

  if (activePoId !== null) {
    return (
      <PurchaseOrderWorkflow purchaseOrderId={activePoId} onReset={() => setActivePoId(null)} />
    )
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Accounts Payable</h1>
      <p className="mt-1 text-sm text-gray-500">
        Look up a purchase order to match a supplier invoice against its receipts, post it, and
        record payments.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          const id = Number(poIdInput)
          if (Number.isInteger(id) && id > 0) setActivePoId(id)
        }}
        className="mt-4 flex max-w-sm gap-2"
      >
        <input
          value={poIdInput}
          onChange={(e) => setPoIdInput(e.target.value)}
          placeholder="Purchase Order ID"
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Find PO
        </button>
      </form>
    </div>
  )
}
