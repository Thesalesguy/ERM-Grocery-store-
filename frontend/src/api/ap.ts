import { apiFetch, buildQuery } from './client'

// --- Purchase invoices (M6) ------------------------------------------------
//
// A purchase invoice's dollar values are always computed server-side from
// its lines and, at posting time, from the ORIGINAL receipt cost data —
// this client only ever sends quantities/prices/dates, never a
// server-computed total, matching the same rule PosPage/SalesPage follow.

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
  purchase_order_id: number
  invoice_number: string
  invoice_date: string
  due_date?: string
  notes?: string
  /** Idempotency key — same convention as SaleCreateInput.client_transaction_id. */
  client_transaction_id: string
  lines: PurchaseInvoiceLineInput[]
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
}

export interface PurchaseInvoice {
  id: number
  store_id: number
  supplier_id: number
  purchase_order_id: number
  invoice_number: string
  invoice_date: string
  due_date: string
  status: 'DRAFT' | 'POSTED' | 'PARTIALLY_PAID' | 'PAID' | 'VOIDED'
  subtotal: string
  discount_total: string
  tax_total: string
  grand_total: string
  amount_paid: string
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
  product_id: number
  quantity_ordered: string
  quantity_received: string
  quantity_invoiced: string
  quantity_invoiceable: string
  product_name: string | null
  product_sku: string | null
}

export interface PurchaseOrderMatchingStatus {
  purchase_order_id: number
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

// --- Supplier payments -------------------------------------------------------

export interface SupplierPaymentCreateInput {
  store_id: number
  payment_date: string
  payment_method: 'CASH' | 'BANK_TRANSFER' | 'CHEQUE' | 'OTHER'
  amount: string
  reference?: string
  client_transaction_id: string
}

export interface SupplierPayment {
  id: number
  store_id: number
  supplier_id: number
  purchase_invoice_id: number
  payment_date: string
  payment_method: string
  amount: string
  reference: string | null
  client_transaction_id: string
  created_at: string
}

export function createPayment(
  purchaseInvoiceId: number,
  input: SupplierPaymentCreateInput,
): Promise<SupplierPayment> {
  return apiFetch<SupplierPayment>(`/api/v1/ap/invoices/${purchaseInvoiceId}/payments`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// --- Supplier AP subledger ----------------------------------------------------

export interface SupplierApSummary {
  supplier_id: number
  total_owed: string
  total_overdue: string
  total_current: string
  total_paid: string
  outstanding_purchase_clearing: string
}

export function getSupplierSummary(supplierId: number): Promise<SupplierApSummary> {
  return apiFetch<SupplierApSummary>(`/api/v1/ap/suppliers/${supplierId}/summary`)
}

export interface SupplierTransaction {
  transaction_type: 'INVOICE' | 'PAYMENT'
  id: number
  date: string
  reference: string
  amount: string
  status: string
}

export function getSupplierTransactions(supplierId: number): Promise<SupplierTransaction[]> {
  return apiFetch<SupplierTransaction[]>(`/api/v1/ap/suppliers/${supplierId}/transactions`)
}
