import { useEffect, useRef, useState } from 'react'
import * as salesApi from '../api/sales'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

/** Quantity + restock choice the cashier is entering for one returnable
 * line — purely UI state. The dollar amounts (refund price, discount/tax
 * refunded, refund total) are never computed here: they only ever come
 * from the server's SaleReturn response after a successful submit, same
 * rule PosPage follows for the cart total. */
interface ReturnLineDraft {
  quantity: string
  restock: boolean
}

function ReturnConfirmation({
  sale_return: saleReturn,
  isVoid,
  onDone,
}: {
  sale_return: salesApi.SaleReturn
  isVoid: boolean
  onDone: () => void
}) {
  return (
    <div className="max-w-lg rounded border border-gray-200 bg-white p-6 shadow-sm">
      <p className="text-sm font-semibold text-green-700">
        {isVoid ? 'Sale voided' : 'Return processed'}
      </p>
      <p className="mt-1 font-mono text-xs text-gray-500">{saleReturn.return_number}</p>
      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-1 pr-4">Product</th>
            <th className="py-1 pr-4">Qty</th>
            <th className="py-1 pr-4">Refunded</th>
            <th className="py-1 pr-4">Restocked</th>
          </tr>
        </thead>
        <tbody>
          {saleReturn.items.map((item) => {
            const refunded =
              Number(item.unit_price_refunded) * Number(item.quantity) -
              Number(item.discount_refunded) +
              Number(item.tax_refunded)
            return (
              <tr key={item.id} className="border-b border-gray-100">
                <td className="py-1 pr-4">{item.product_name ?? `#${item.product_id}`}</td>
                <td className="py-1 pr-4">{item.quantity}</td>
                <td className="py-1 pr-4">{refunded.toFixed(2)}</td>
                <td className="py-1 pr-4">{item.restock ? 'Yes' : 'No (write-off)'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="mt-3 flex justify-between border-t border-gray-200 pt-3 text-sm font-semibold">
        <span>Total refund ({saleReturn.refund_method})</span>
        <span>{saleReturn.refund_amount}</span>
      </div>
      {saleReturn.approval_required && (
        <p className="mt-2 text-xs text-amber-700">
          Manager/admin approved (user #{saleReturn.approved_by}).
        </p>
      )}
      <p className="mt-3 text-xs text-gray-400">
        The accounting entries for this {isVoid ? 'void' : 'return'} were posted automatically by
        the server and are not editable here.
      </p>
      <button
        onClick={onDone}
        className="mt-4 w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
      >
        Find another sale
      </button>
    </div>
  )
}

function SaleReturnWorkflow({ saleId, onReset }: { saleId: number; onReset: () => void }) {
  const { user, hasPermission } = useAuth()
  const [sale, setSale] = useState<salesApi.Sale | null>(null)
  const [eligibility, setEligibility] = useState<salesApi.SaleReturnEligibility | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<number, ReturnLineDraft>>({})
  const [refundMethod, setRefundMethod] =
    useState<salesApi.SaleReturnCreateInput['refund_method']>('CASH')
  const [reason, setReason] = useState('')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  // M14: the server is the only source of truth for whether this
  // operation's amount requires approval — the UI never predicts this
  // itself (it doesn't know the store's threshold). It learns this only
  // from an APPROVAL_REQUIRED response to an actual attempt, then shows
  // the approver fields and lets the SAME attempt (same idempotency key)
  // be resubmitted with them filled in.
  const [needsApproval, setNeedsApproval] = useState(false)
  const [approverUsername, setApproverUsername] = useState('')
  const [approverPassword, setApproverPassword] = useState('')
  const [result, setResult] = useState<{ saleReturn: salesApi.SaleReturn; isVoid: boolean } | null>(
    null,
  )
  // Idempotency key for the return/void attempt currently in progress —
  // same convention as PosPage's checkoutKeyRef: stable across retries of
  // this one attempt, reset only after it succeeds.
  const idempotencyKeyRef = useRef<string | null>(null)

  const canWrite = hasPermission('sales.return.write')
  const canVoid = hasPermission('sales.void')

  async function load() {
    setLoadError(null)
    try {
      const [saleData, eligibilityData] = await Promise.all([
        salesApi.getSale(saleId),
        salesApi.getReturnEligibility(saleId),
      ])
      setSale(saleData)
      setEligibility(eligibilityData)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : 'Could not load this sale.')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saleId])

  function updateDraft(saleItemId: number, field: keyof ReturnLineDraft, value: string | boolean) {
    setDrafts((prev) => ({
      ...prev,
      [saleItemId]: {
        quantity: prev[saleItemId]?.quantity ?? '',
        restock: prev[saleItemId]?.restock ?? true,
        [field]: value,
      },
    }))
  }

  const returnLines = Object.entries(drafts)
    .map(([saleItemId, draft]) => ({ saleItemId: Number(saleItemId), draft }))
    .filter(({ draft }) => draft.quantity.trim() !== '' && Number(draft.quantity) > 0)

  // M14: a failed approval attempt (wrong password, self-approval, no
  // permission, wrong store) is a submit failure like any other — shown
  // via submitError — but the approver fields STAY visible so the
  // cashier/manager can correct and retry, rather than being dropped
  // back to "no approval needed yet".
  function handleSubmitError(err: unknown, fallback: string) {
    if (err instanceof ApiError) {
      if (err.code === 'APPROVAL_REQUIRED') {
        setNeedsApproval(true)
      }
      setSubmitError(err.message)
      return
    }
    setSubmitError(fallback)
  }

  async function submitReturn() {
    if (!user?.store_id || returnLines.length === 0) return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setSubmitError(null)
    setIsSubmitting(true)
    try {
      const saleReturn = await salesApi.createSaleReturn(saleId, {
        store_id: user.store_id,
        return_date: new Date().toISOString().slice(0, 10),
        client_transaction_id: idempotencyKeyRef.current,
        refund_method: refundMethod,
        reason: reason.trim() || undefined,
        lines: returnLines.map(({ saleItemId, draft }) => ({
          sale_item_id: saleItemId,
          quantity: draft.quantity,
          restock: draft.restock,
        })),
        ...(needsApproval
          ? { approver_username: approverUsername, approver_password: approverPassword }
          : {}),
      })
      idempotencyKeyRef.current = null
      setResult({ saleReturn, isVoid: false })
    } catch (err) {
      handleSubmitError(err, 'Failed to process the return.')
    } finally {
      setIsSubmitting(false)
    }
  }

  async function submitVoid() {
    if (!user?.store_id) return
    if (
      !window.confirm('Void this entire sale? Every remaining unit will be returned and restocked.')
    )
      return
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = crypto.randomUUID()
    }
    setSubmitError(null)
    setIsSubmitting(true)
    try {
      const saleReturn = await salesApi.voidSale(saleId, {
        store_id: user.store_id,
        return_date: new Date().toISOString().slice(0, 10),
        client_transaction_id: idempotencyKeyRef.current,
        refund_method: refundMethod,
        reason: reason.trim() || undefined,
        ...(needsApproval
          ? { approver_username: approverUsername, approver_password: approverPassword }
          : {}),
      })
      idempotencyKeyRef.current = null
      setResult({ saleReturn, isVoid: true })
    } catch (err) {
      handleSubmitError(err, 'Failed to void the sale.')
    } finally {
      setIsSubmitting(false)
    }
  }

  if (result) {
    return (
      <ReturnConfirmation sale_return={result.saleReturn} isVoid={result.isVoid} onDone={onReset} />
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

  if (!sale || !eligibility) {
    return <p className="text-gray-500">Loading…</p>
  }

  const returnableItems = eligibility.items.filter((item) => Number(item.quantity_returnable) > 0)
  const nothingLeftToReturn = returnableItems.length === 0

  return (
    <div>
      <button onClick={onReset} className="text-sm text-blue-600 hover:underline">
        ← Find another sale
      </button>
      <div className="mt-2 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">{sale.sale_number}</h1>
          <p className="text-sm text-gray-500">
            {sale.completed_at && new Date(sale.completed_at).toLocaleString()}
          </p>
        </div>
        <span className="rounded bg-gray-100 px-2 py-1 text-xs font-medium text-gray-700">
          {eligibility.sale_status}
        </span>
      </div>

      {nothingLeftToReturn ? (
        <p className="mt-6 text-sm text-gray-500">
          Every unit on this sale has already been returned — nothing left to return or void.
        </p>
      ) : (
        <>
          <table className="mt-4 w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-gray-500">
                <th className="py-2 pr-4">Product</th>
                <th className="py-2 pr-4">Sold</th>
                <th className="py-2 pr-4">Already returned</th>
                <th className="py-2 pr-4">Returnable</th>
                <th className="py-2 pr-4">Return qty</th>
                <th className="py-2 pr-4">Restock</th>
              </tr>
            </thead>
            <tbody>
              {returnableItems.map((item) => (
                <tr key={item.sale_item_id} className="border-b border-gray-100">
                  <td className="py-2 pr-4">
                    {item.product_name ?? `#${item.product_id}`}
                    <div className="font-mono text-xs text-gray-400">{item.product_sku}</div>
                  </td>
                  <td className="py-2 pr-4">{item.quantity}</td>
                  <td className="py-2 pr-4">{item.quantity_returned}</td>
                  <td className="py-2 pr-4 font-medium">{item.quantity_returnable}</td>
                  <td className="py-2 pr-4">
                    <input
                      value={drafts[item.sale_item_id]?.quantity ?? ''}
                      onChange={(e) => updateDraft(item.sale_item_id, 'quantity', e.target.value)}
                      placeholder="0"
                      disabled={!canWrite}
                      className="w-20 rounded border border-gray-300 px-2 py-1 disabled:bg-gray-100"
                    />
                  </td>
                  <td className="py-2 pr-4">
                    <input
                      type="checkbox"
                      checked={drafts[item.sale_item_id]?.restock ?? true}
                      onChange={(e) => updateDraft(item.sale_item_id, 'restock', e.target.checked)}
                      disabled={!canWrite}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {canWrite && (
            <div className="mt-4 rounded border border-gray-200 bg-white p-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="block text-sm font-medium text-gray-700">Refund method</label>
                  <select
                    value={refundMethod}
                    onChange={(e) =>
                      setRefundMethod(
                        e.target.value as salesApi.SaleReturnCreateInput['refund_method'],
                      )
                    }
                    className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm"
                  >
                    {(['CASH', 'CARD', 'MOBILE_MONEY', 'BANK_TRANSFER', 'OTHER'] as const).map(
                      (m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ),
                    )}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">
                    Reason (optional)
                  </label>
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm"
                    placeholder="Customer changed mind, damaged item, …"
                  />
                </div>
              </div>

              {needsApproval && (
                <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3">
                  <p className="text-sm font-medium text-amber-800">
                    This amount requires manager/admin approval. Have an authorized approver (not
                    yourself) enter their credentials below.
                  </p>
                  <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div>
                      <label
                        htmlFor="approver-username"
                        className="block text-sm font-medium text-gray-700"
                      >
                        Approver username
                      </label>
                      <input
                        id="approver-username"
                        value={approverUsername}
                        onChange={(e) => setApproverUsername(e.target.value)}
                        className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm"
                      />
                    </div>
                    <div>
                      <label
                        htmlFor="approver-password"
                        className="block text-sm font-medium text-gray-700"
                      >
                        Approver password
                      </label>
                      <input
                        id="approver-password"
                        type="password"
                        value={approverPassword}
                        onChange={(e) => setApproverPassword(e.target.value)}
                        className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm"
                      />
                    </div>
                  </div>
                </div>
              )}

              {submitError && (
                <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
                  {submitError}
                </p>
              )}

              <div className="mt-4 flex gap-2">
                <button
                  onClick={submitReturn}
                  disabled={isSubmitting || returnLines.length === 0}
                  className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isSubmitting ? 'Processing…' : 'Process return'}
                </button>
                {canVoid && (
                  <button
                    onClick={submitVoid}
                    disabled={isSubmitting}
                    className="rounded border border-red-300 px-4 py-2 text-sm text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Void entire sale
                  </button>
                )}
              </div>
              <p className="mt-2 text-xs text-gray-400">
                The refund amount is calculated by the server from this sale's original price,
                discount, and tax — it is never entered manually.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export function SalesPage() {
  const { hasPermission } = useAuth()
  const [saleIdInput, setSaleIdInput] = useState('')
  const [activeSaleId, setActiveSaleId] = useState<number | null>(null)

  const canRead = hasPermission('sales.return.read')

  if (!canRead) {
    return (
      <div>
        <h1 className="text-2xl font-semibold text-gray-900">Sales History</h1>
        <p className="mt-4 text-sm text-gray-500">
          Your account does not have permission to view or process returns.
        </p>
      </div>
    )
  }

  if (activeSaleId !== null) {
    return <SaleReturnWorkflow saleId={activeSaleId} onReset={() => setActiveSaleId(null)} />
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Sales History</h1>
      <p className="mt-1 text-sm text-gray-500">
        Look up a completed sale to inspect it, process a return, or void it entirely.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          const id = Number(saleIdInput)
          if (Number.isInteger(id) && id > 0) setActiveSaleId(id)
        }}
        className="mt-4 flex max-w-sm gap-2"
      >
        <input
          value={saleIdInput}
          onChange={(e) => setSaleIdInput(e.target.value)}
          placeholder="Sale ID"
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Find sale
        </button>
      </form>
    </div>
  )
}
