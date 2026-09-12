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

/** A dynamic list of (invoice id, amount) rows for a payment or credit
 * note that may allocate across more than one invoice — the server is the
 * only authority on whether the sum is valid; this component just collects
 * the rows. */
function AllocationRows({
  rows,
  onChange,
}: {
  rows: { invoiceId: string; amount: string }[]
  onChange: (rows: { invoiceId: string; amount: string }[]) => void
}) {
  return (
    <div className="mt-2 space-y-2">
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-2">
          <input
            value={row.invoiceId}
            onChange={(e) => {
              const next = [...rows]
              next[index] = { ...row, invoiceId: e.target.value }
              onChange(next)
            }}
            placeholder="Invoice ID"
            className="w-28 rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
          <input
            value={row.amount}
            onChange={(e) => {
              const next = [...rows]
              next[index] = { ...row, amount: e.target.value }
              onChange(next)
            }}
            placeholder="Amount"
            className="w-28 rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
          {rows.length > 1 && (
            <button
              type="button"
              onClick={() => onChange(rows.filter((_, i) => i !== index))}
              className="text-xs text-red-600 hover:underline"
            >
              Remove
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange([...rows, { invoiceId: '', amount: '' }])}
        className="text-xs text-blue-600 hover:underline"
      >
        + Allocate to another invoice
      </button>
    </div>
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
  const [extraAllocations, setExtraAllocations] = useState<{ invoiceId: string; amount: string }[]>(
    [],
  )
  const [creditReason, setCreditReason] =
    useState<apApi.SupplierCreditNoteReason>('COMMERCIAL_DISCOUNT')
  const [creditAmount, setCreditAmount] = useState('')
  const [creditNumber, setCreditNumber] = useState('')
  const idempotencyKeyRef = useRef<string | null>(null)
  const creditKeyRef = useRef<string | null>(null)

  const canPost = hasPermission('ap.post')
  const canPay = hasPermission('ap.pay')
  const canCredit = hasPermission('ap.credit')

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
      const allocations: apApi.PaymentAllocationInput[] = [
        { purchase_invoice_id: invoiceId, amount: paymentAmount },
        ...extraAllocations
          .filter((row) => row.invoiceId.trim() && row.amount.trim())
          .map((row) => ({
            purchase_invoice_id: Number(row.invoiceId),
            amount: row.amount,
          })),
      ]
      const total = allocations.reduce((sum, a) => sum + Number(a.amount || 0), 0).toFixed(2)
      await apApi.createPayment({
        store_id: user.store_id,
        supplier_id: invoice.supplier_id,
        payment_date: new Date().toISOString().slice(0, 10),
        payment_method: paymentMethod,
        amount: total,
        allocations,
        client_transaction_id: idempotencyKeyRef.current,
      })
      idempotencyKeyRef.current = null
      setPaymentAmount('')
      setExtraAllocations([])
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to record the payment.')
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handleCreditNote() {
    if (!user?.store_id || !invoice || !creditNumber.trim() || !creditAmount.trim()) return
    if (creditKeyRef.current === null) {
      creditKeyRef.current = crypto.randomUUID()
    }
    setError(null)
    setIsSubmitting(true)
    try {
      await apApi.createCreditNote({
        store_id: user.store_id,
        supplier_id: invoice.supplier_id,
        credit_number: creditNumber,
        credit_date: new Date().toISOString().slice(0, 10),
        reason: creditReason,
        lines: [{ description: `Credit note ${creditNumber}`, amount: creditAmount }],
        allocations: [{ purchase_invoice_id: invoiceId, amount: creditAmount }],
        client_transaction_id: creditKeyRef.current,
      })
      creditKeyRef.current = null
      setCreditAmount('')
      setCreditNumber('')
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create the credit note.')
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
            <th className="py-2 pr-4">Matched cost</th>
            <th className="py-2 pr-4">Variance</th>
          </tr>
        </thead>
        <tbody>
          {invoice.lines.map((line) => {
            const totalVariance = line.matches.reduce(
              (sum, m) => sum + Number(m.variance_amount),
              0,
            )
            const totalMatchedCost = line.matches.reduce(
              (sum, m) => sum + Number(m.matched_quantity) * Number(m.matched_unit_cost),
              0,
            )
            return (
              <tr key={line.id} className="border-b border-gray-100">
                <td className="py-2 pr-4">
                  {line.product_name ?? `#${line.product_id}`}
                  <div className="font-mono text-xs text-gray-400">{line.product_sku}</div>
                </td>
                <td className="py-2 pr-4">{line.quantity_invoiced}</td>
                <td className="py-2 pr-4">{line.unit_price}</td>
                <td className="py-2 pr-4">{line.line_total}</td>
                <td className="py-2 pr-4">
                  {line.matches.length > 0 ? totalMatchedCost.toFixed(2) : '—'}
                </td>
                <td
                  className={`py-2 pr-4 ${totalVariance > 0 ? 'text-red-600' : totalVariance < 0 ? 'text-green-600' : ''}`}
                >
                  {line.matches.length > 0 ? totalVariance.toFixed(2) : '—'}
                </td>
              </tr>
            )
          })}
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
          <div className="flex justify-between">
            <span className="text-gray-500">Amount credited</span>
            <span>{invoice.amount_credited}</span>
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
              placeholder="Amount for this invoice"
              className="w-40 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              onClick={handlePay}
              disabled={isSubmitting || !paymentAmount}
              className="rounded bg-green-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isSubmitting ? 'Recording…' : 'Record payment'}
            </button>
          </div>
          <p className="mt-2 text-xs text-gray-500">
            One real payment can settle several invoices — optionally allocate the rest of it to
            other invoices below.
          </p>
          <AllocationRows rows={extraAllocations} onChange={setExtraAllocations} />
          <p className="mt-2 text-xs text-gray-400">
            The remaining balance is calculated by the server — it is never entered manually.
          </p>
        </div>
      )}

      {(invoice.status === 'POSTED' || invoice.status === 'PARTIALLY_PAID') && canCredit && (
        <div className="mt-4 rounded border border-gray-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">Issue a supplier credit note</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              value={creditNumber}
              onChange={(e) => setCreditNumber(e.target.value)}
              placeholder="Credit note number"
              className="w-40 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            <select
              value={creditReason}
              onChange={(e) => setCreditReason(e.target.value as apApi.SupplierCreditNoteReason)}
              className="rounded border border-gray-300 px-2 py-1.5 text-sm"
            >
              <option value="COMMERCIAL_DISCOUNT">Commercial discount</option>
              <option value="GOODS_RETURN">Goods return</option>
            </select>
            <input
              value={creditAmount}
              onChange={(e) => setCreditAmount(e.target.value)}
              placeholder="Amount"
              className="w-32 rounded border border-gray-300 px-2 py-1.5 text-sm"
            />
            <button
              onClick={handleCreditNote}
              disabled={isSubmitting || !creditAmount || !creditNumber.trim()}
              className="rounded bg-amber-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isSubmitting ? 'Creating…' : 'Create credit note'}
            </button>
          </div>
          {creditReason === 'GOODS_RETURN' && (
            <p className="mt-2 text-xs text-amber-700">
              Goods-return credit notes require a purchase return reference — use the Purchasing
              page to record the physical return first, then reference it via the API.
            </p>
          )}
          <p className="mt-2 text-xs text-gray-400">
            A credit note is immutable once created and fully applied to this invoice immediately —
            never left as an unapplied balance.
          </p>
        </div>
      )}
    </div>
  )
}

function NewInvoiceForm({
  purchaseOrders,
  matching,
  onCreated,
}: {
  purchaseOrders: purchasingApi.PurchaseOrder[]
  matching: apApi.PurchaseOrderMatchingStatus
  onCreated: (invoice: apApi.PurchaseInvoice) => void
}) {
  const [invoiceNumber, setInvoiceNumber] = useState('')
  const [invoiceDate, setInvoiceDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [drafts, setDrafts] = useState<Record<number, InvoiceLineDraft>>({})
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const idempotencyKeyRef = useRef<string | null>(null)

  const primaryPo = purchaseOrders[0]
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
    if (!invoiceNumber.trim() || selectedLines.length === 0 || !primaryPo) return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setError(null)
    setIsSubmitting(true)
    try {
      const invoice = await apApi.createInvoice({
        store_id: primaryPo.store_id,
        supplier_id: primaryPo.supplier_id,
        // Omitted when the invoice spans more than one PO — the server
        // fills it in automatically when every line shares a single PO,
        // and never uses it to validate lines either way.
        purchase_order_id: purchaseOrders.length === 1 ? primaryPo.id : undefined,
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
            <th className="py-1 pr-2">PO</th>
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
              <td className="py-1 pr-2 font-mono text-xs text-gray-400">
                #{item.purchase_order_id}
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
        enter here — never displayed as an estimate before the server confirms them. Posting will be
        rejected (not silently accepted) if any line would exceed what was actually received.
      </p>
    </div>
  )
}

function PurchaseOrderWorkflow({
  purchaseOrderIds,
  onReset,
}: {
  purchaseOrderIds: number[]
  onReset: () => void
}) {
  const [purchaseOrders, setPurchaseOrders] = useState<purchasingApi.PurchaseOrder[] | null>(null)
  const [matching, setMatching] = useState<apApi.PurchaseOrderMatchingStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeInvoiceId, setActiveInvoiceId] = useState<number | null>(null)

  async function load() {
    setLoadError(null)
    try {
      const [pos, matchStatus] = await Promise.all([
        Promise.all(purchaseOrderIds.map((id) => purchasingApi.getPurchaseOrder(id))),
        purchaseOrderIds.length === 1
          ? apApi.getMatchingStatus(purchaseOrderIds[0])
          : apApi.getMatchingStatusMulti(purchaseOrderIds),
      ])
      setPurchaseOrders(pos)
      setMatching(matchStatus)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : 'Could not load this purchase order.')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [purchaseOrderIds.join(',')])

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
  if (!purchaseOrders || !matching) return <p className="text-gray-500">Loading…</p>

  return (
    <div>
      <button onClick={onReset} className="text-sm text-blue-600 hover:underline">
        ← Find another purchase order
      </button>
      <h1 className="mt-2 text-xl font-semibold text-gray-900">
        {purchaseOrders.map((po) => po.purchase_number).join(' + ')}
      </h1>
      <p className="text-sm text-gray-500">{purchaseOrders[0]?.supplier_name}</p>

      <NewInvoiceForm
        purchaseOrders={purchaseOrders}
        matching={matching}
        onCreated={(invoice) => setActiveInvoiceId(invoice.id)}
      />
    </div>
  )
}

function SupplierTools() {
  const [supplierIdInput, setSupplierIdInput] = useState('')
  const [supplierId, setSupplierId] = useState<number | null>(null)
  const [summary, setSummary] = useState<apApi.SupplierApSummary | null>(null)
  const [statement, setStatement] = useState<apApi.SupplierStatement | null>(null)
  const [aging, setAging] = useState<apApi.ApAgingRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (supplierId === null) return
    Promise.all([
      apApi.getSupplierSummary(supplierId),
      apApi.getSupplierStatement(supplierId),
      apApi.getApAging(),
    ])
      .then(([s, st, ag]) => {
        setSummary(s)
        setStatement(st)
        setAging(ag.filter((row) => row.supplier_id === supplierId))
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : 'Could not load supplier AP data.'),
      )
  }, [supplierId])

  return (
    <div className="mt-8 rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Supplier statement &amp; aging</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          const id = Number(supplierIdInput)
          if (Number.isInteger(id) && id > 0) {
            setError(null)
            setSupplierId(id)
          }
        }}
        className="mt-2 flex max-w-sm gap-2"
      >
        <input
          value={supplierIdInput}
          onChange={(e) => setSupplierIdInput(e.target.value)}
          placeholder="Supplier ID"
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Look up
        </button>
      </form>

      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

      {summary && (
        <div className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
          <div className="rounded border border-gray-100 p-2">
            <div className="text-gray-500">Total owed</div>
            <div className="font-semibold">{summary.total_owed}</div>
          </div>
          <div className="rounded border border-gray-100 p-2">
            <div className="text-gray-500">Overdue</div>
            <div className="font-semibold">{summary.total_overdue}</div>
          </div>
          <div className="rounded border border-gray-100 p-2">
            <div className="text-gray-500">Outstanding clearing</div>
            <div className="font-semibold">{summary.outstanding_purchase_clearing}</div>
          </div>
        </div>
      )}

      {aging && aging.length > 0 && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="py-1 pr-2">Current</th>
              <th className="py-1 pr-2">1-30</th>
              <th className="py-1 pr-2">31-60</th>
              <th className="py-1 pr-2">61-90</th>
              <th className="py-1 pr-2">90+</th>
              <th className="py-1 pr-2">Total</th>
            </tr>
          </thead>
          <tbody>
            {aging.map((row) => (
              <tr key={row.supplier_id}>
                <td className="py-1 pr-2">{row.current}</td>
                <td className="py-1 pr-2">{row.days_1_30}</td>
                <td className="py-1 pr-2">{row.days_31_60}</td>
                <td className="py-1 pr-2">{row.days_61_90}</td>
                <td className="py-1 pr-2">{row.days_over_90}</td>
                <td className="py-1 pr-2 font-semibold">{row.total}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {statement && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="py-1 pr-2">Date</th>
              <th className="py-1 pr-2">Type</th>
              <th className="py-1 pr-2">Reference</th>
              <th className="py-1 pr-2">Amount</th>
              <th className="py-1 pr-2">Running balance</th>
            </tr>
          </thead>
          <tbody>
            {statement.lines.map((line, i) => (
              <tr key={i} className="border-t border-gray-100">
                <td className="py-1 pr-2">{line.date}</td>
                <td className="py-1 pr-2">{line.transaction_type}</td>
                <td className="py-1 pr-2">{line.reference}</td>
                <td className="py-1 pr-2">{line.amount}</td>
                <td className="py-1 pr-2 font-medium">{line.running_balance}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export function AccountsPayablePage() {
  const { hasPermission } = useAuth()
  const [poIdInput, setPoIdInput] = useState('')
  const [activePoIds, setActivePoIds] = useState<number[] | null>(null)

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

  if (activePoIds !== null) {
    return (
      <PurchaseOrderWorkflow purchaseOrderIds={activePoIds} onReset={() => setActivePoIds(null)} />
    )
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Accounts Payable</h1>
      <p className="mt-1 text-sm text-gray-500">
        Look up one or more purchase orders (comma-separated) to match a supplier invoice against
        their receipts, post it, and record payments and credit notes.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          const ids = poIdInput
            .split(',')
            .map((part) => Number(part.trim()))
            .filter((id) => Number.isInteger(id) && id > 0)
          if (ids.length > 0) setActivePoIds(ids)
        }}
        className="mt-4 flex max-w-md gap-2"
      >
        <input
          value={poIdInput}
          onChange={(e) => setPoIdInput(e.target.value)}
          placeholder="Purchase Order ID(s), e.g. 12 or 12,13"
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Find PO
        </button>
      </form>

      <SupplierTools />
    </div>
  )
}
