import { apiFetch, buildQuery } from './client'

// --- Purchase invoices (M6/M7) ----------------------------------------------
//
// A purchase invoice's dollar values are always computed server-side from
// its lines and, at posting time, from the ORIGINAL receipt cost data —
// this client only ever sends quantities/prices/dates, never a
// server-computed total, matching the same rule PosPage/SalesPage follow.
//
// M7: `purchase_order_id` is optional (an invoice's lines may span several
// purchase orders — the server validates each line independently), and
// every invoice line carries its persisted `matches` (which receipt lot(s)
// it consumed, at what cost, and the resulting variance).

export interface PurchaseInvoiceLineInput {
  purchase_order_item_id: number
  quantity_invoiced: string
  unit_price: string
  discount_amount?: string
  tax_amount?: string
  description?: string
}

export interface PurchaseInvoiceCreateInput {
  store_id: number
  supplier_id: number
  purchase_order_id?: number
  invoice_number: string
  invoice_date: string
  due_date?: string
  notes?: string
  /** Idempotency key — same convention as SaleCreateInput.client_transaction_id. */
  client_transaction_id: string
  lines: PurchaseInvoiceLineInput[]
}

export interface PurchaseInvoiceReceiptMatch {
  id: number
  purchase_invoice_line_id: number
  goods_receipt_item_id: number
  matched_quantity: string
  matched_unit_cost: string
  variance_amount: string
}

export interface PurchaseInvoiceLine {
  id: number
  purchase_order_item_id: number
  product_id: number
  description: string
  quantity_invoiced: string
  unit_price: string
  discount_amount: string
  tax_amount: string
  line_total: string
  product_name: string | null
  product_sku: string | null
  matches: PurchaseInvoiceReceiptMatch[]
}

export interface PurchaseInvoice {
  id: number
  store_id: number
  supplier_id: number
  purchase_order_id: number | null
  invoice_number: string
  invoice_date: string
  due_date: string
  status: 'DRAFT' | 'POSTED' | 'PARTIALLY_PAID' | 'PAID' | 'VOIDED'
  subtotal: string
  discount_total: string
  tax_total: string
  grand_total: string
  amount_paid: string
  amount_credited: string
  balance_due: string
  client_transaction_id: string
  notes: string | null
  posted_at: string | null
  voided_at: string | null
  created_at: string
  supplier_name: string | null
  lines: PurchaseInvoiceLine[]
}

export interface PurchaseOrderItemMatchStatus {
  purchase_order_item_id: number
  purchase_order_id: number
  product_id: number
  quantity_ordered: string
  quantity_received: string
  quantity_invoiced: string
  quantity_invoiceable: string
  product_name: string | null
  product_sku: string | null
}

export interface PurchaseOrderMatchingStatus {
  purchase_order_ids: number[]
  items: PurchaseOrderItemMatchStatus[]
}

export function listInvoices(
  params: {
    store_id?: number
    supplier_id?: number
    status?: string
  } = {},
): Promise<PurchaseInvoice[]> {
  return apiFetch<PurchaseInvoice[]>(`/api/v1/ap/invoices${buildQuery(params)}`)
}

export function getInvoice(id: number): Promise<PurchaseInvoice> {
  return apiFetch<PurchaseInvoice>(`/api/v1/ap/invoices/${id}`)
}

export function createInvoice(input: PurchaseInvoiceCreateInput): Promise<PurchaseInvoice> {
  return apiFetch<PurchaseInvoice>('/api/v1/ap/invoices', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function postInvoice(id: number): Promise<PurchaseInvoice> {
  return apiFetch<PurchaseInvoice>(`/api/v1/ap/invoices/${id}/post`, { method: 'POST' })
}

export function voidInvoice(id: number, reason?: string): Promise<PurchaseInvoice> {
  return apiFetch<PurchaseInvoice>(`/api/v1/ap/invoices/${id}/void`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function getMatchingStatus(purchaseOrderId: number): Promise<PurchaseOrderMatchingStatus> {
  return apiFetch<PurchaseOrderMatchingStatus>(
    `/api/v1/ap/purchase-orders/${purchaseOrderId}/matching-status`,
  )
}

/** M7: matching preview across MULTIPLE purchase orders at once, for an
 * invoice whose lines will span more than one PO. */
export function getMatchingStatusMulti(
  purchaseOrderIds: number[],
): Promise<PurchaseOrderMatchingStatus> {
  return apiFetch<PurchaseOrderMatchingStatus>(
    `/api/v1/ap/purchase-orders/matching-status?purchase_order_ids=${purchaseOrderIds.join(',')}`,
  )
}

// --- Supplier payments (M7: header + allocations) ---------------------------

export interface PaymentAllocationInput {
  purchase_invoice_id: number
  amount: string
}

export interface SupplierPaymentCreateInput {
  store_id: number
  supplier_id: number
  payment_date: string
  payment_method: 'CASH' | 'BANK_TRANSFER' | 'CHEQUE' | 'OTHER'
  amount: string
  /** Must sum to exactly `amount` — an unapplied remainder is rejected,
   * never silently accepted as an advance. */
  allocations: PaymentAllocationInput[]
  reference?: string
  client_transaction_id: string
}

export interface PaymentAllocation {
  id: number
  purchase_invoice_id: number
  amount: string
}

export interface SupplierPayment {
  id: number
  store_id: number
  supplier_id: number
  payment_date: string
  payment_method: string
  amount: string
  reference: string | null
  client_transaction_id: string
  created_at: string
  allocations: PaymentAllocation[]
}

export function createPayment(input: SupplierPaymentCreateInput): Promise<SupplierPayment> {
  return apiFetch<SupplierPayment>('/api/v1/ap/payments', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function listPayments(
  params: { store_id?: number; supplier_id?: number; purchase_invoice_id?: number } = {},
): Promise<SupplierPayment[]> {
  return apiFetch<SupplierPayment[]>(`/api/v1/ap/payments${buildQuery(params)}`)
}

// --- Supplier credit notes (M7) ----------------------------------------------

export type SupplierCreditNoteReason = 'GOODS_RETURN' | 'COMMERCIAL_DISCOUNT'

export interface SupplierCreditNoteLineInput {
  description: string
  amount: string
  product_id?: number
  quantity?: string
  unit_cost?: string
}

export interface CreditAllocationInput {
  purchase_invoice_id: number
  amount: string
}

export interface SupplierCreditNoteCreateInput {
  store_id: number
  supplier_id: number
  credit_number: string
  credit_date: string
  reason: SupplierCreditNoteReason
  purchase_return_id?: number
  lines: SupplierCreditNoteLineInput[]
  /** Must sum to exactly the credit's total — no unapplied credit balance. */
  allocations: CreditAllocationInput[]
  notes?: string
  client_transaction_id: string
}

export interface SupplierCreditNoteLine {
  id: number
  product_id: number | null
  description: string
  quantity: string | null
  unit_cost: string | null
  amount: string
}

export interface SupplierCreditAllocation {
  id: number
  purchase_invoice_id: number
  amount: string
}

export interface SupplierCreditNote {
  id: number
  store_id: number
  supplier_id: number
  credit_number: string
  credit_date: string
  reason: SupplierCreditNoteReason
  purchase_return_id: number | null
  grand_total: string
  amount_allocated: string
  client_transaction_id: string
  notes: string | null
  created_at: string
  voided_at: string | null
  supplier_name: string | null
  lines: SupplierCreditNoteLine[]
  allocations: SupplierCreditAllocation[]
}

export function createCreditNote(
  input: SupplierCreditNoteCreateInput,
): Promise<SupplierCreditNote> {
  return apiFetch<SupplierCreditNote>('/api/v1/ap/credit-notes', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function listCreditNotes(
  params: { store_id?: number; supplier_id?: number } = {},
): Promise<SupplierCreditNote[]> {
  return apiFetch<SupplierCreditNote[]>(`/api/v1/ap/credit-notes${buildQuery(params)}`)
}

// --- Supplier AP subledger ----------------------------------------------------

export interface SupplierApSummary {
  supplier_id: number
  total_owed: string
  total_overdue: string
  total_current: string
  total_paid: string
  total_credited: string
  outstanding_purchase_clearing: string
}

export function getSupplierSummary(supplierId: number): Promise<SupplierApSummary> {
  return apiFetch<SupplierApSummary>(`/api/v1/ap/suppliers/${supplierId}/summary`)
}

export interface SupplierTransaction {
  transaction_type: 'INVOICE' | 'PAYMENT' | 'CREDIT_NOTE'
  id: number
  date: string
  reference: string
  amount: string
  status: string
}

export function getSupplierTransactions(supplierId: number): Promise<SupplierTransaction[]> {
  return apiFetch<SupplierTransaction[]>(`/api/v1/ap/suppliers/${supplierId}/transactions`)
}

export interface SupplierStatementLine {
  date: string
  transaction_type: 'INVOICE' | 'PAYMENT' | 'CREDIT_NOTE'
  reference: string
  amount: string
  running_balance: string
}

export interface SupplierStatement {
  supplier_id: number
  date_from: string | null
  date_to: string | null
  opening_balance: string
  lines: SupplierStatementLine[]
  closing_balance: string
}

export function getSupplierStatement(
  supplierId: number,
  params: { date_from?: string; date_to?: string } = {},
): Promise<SupplierStatement> {
  return apiFetch<SupplierStatement>(
    `/api/v1/ap/suppliers/${supplierId}/statement${buildQuery(params)}`,
  )
}

export interface ApAgingRow {
  supplier_id: number
  supplier_name: string | null
  current: string
  days_1_30: string
  days_31_60: string
  days_61_90: string
  days_over_90: string
  total: string
}

export function getApAging(
  params: { store_id?: number; as_of?: string } = {},
): Promise<ApAgingRow[]> {
  return apiFetch<ApAgingRow[]>(`/api/v1/ap/aging${buildQuery(params)}`)
}
