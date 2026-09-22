import { apiFetch } from './client'

export interface SaleLineInput {
  product_id: number
  quantity: string
  discount_amount?: string
}

export interface PaymentInput {
  payment_method: 'CASH' | 'CARD' | 'MOBILE_MONEY' | 'BANK_TRANSFER' | 'OTHER'
  amount: string
  reference?: string
}

export interface SaleCreateInput {
  store_id: number
  /** Idempotency key: generate once per checkout attempt (crypto.randomUUID())
   * and resend the SAME value on any retry of that attempt, so a
   * double-click or a network retry can never create two sales. */
  client_transaction_id: string
  lines: SaleLineInput[]
  payments: PaymentInput[]
}

export interface SaleItem {
  id: number
  product_id: number
  quantity: string
  unit_price_at_sale: string
  unit_cost_at_sale: string
  discount_amount: string
  tax_rate_id: number | null
  tax_amount: string
  line_total: string
  product_name: string | null
  product_sku: string | null
}

export interface Payment {
  id: number
  payment_method: string
  amount: string
  reference: string | null
}

export interface Sale {
  id: number
  store_id: number
  sale_number: string
  client_transaction_id: string
  cashier_id: number
  status: string
  subtotal: string
  discount_total: string
  tax_total: string
  grand_total: string
  amount_tendered: string | null
  change_due: string | null
  completed_at: string | null
  created_at: string
  items: SaleItem[]
  payments: Payment[]
}

export function finalizeSale(input: SaleCreateInput): Promise<Sale> {
  return apiFetch<Sale>('/api/v1/sales', { method: 'POST', body: JSON.stringify(input) })
}

export function getSale(id: number): Promise<Sale> {
  return apiFetch<Sale>(`/api/v1/sales/${id}`)
}

// --- Sale returns / voids (M5) ------------------------------------------
//
// A return's dollar values (refund price, discount/tax refunded, refund
// amount) are always computed server-side from the ORIGINAL sale's frozen
// data — this client only ever sends identifiers and quantities, never a
// price or amount, matching the same "server is the only source of
// financial truth" rule finalizeSale already follows.

export interface SaleItemReturnEligibility {
  sale_item_id: number
  product_id: number
  quantity: string
  quantity_returned: string
  quantity_returnable: string
  product_name: string | null
  product_sku: string | null
}

export interface SaleReturnEligibility {
  sale_id: number
  sale_status: string
  items: SaleItemReturnEligibility[]
}

export interface SaleReturnLineInput {
  sale_item_id: number
  quantity: string
  restock?: boolean
}

export interface SaleReturnCreateInput {
  store_id: number
  return_date: string
  /** Idempotency key — same convention as SaleCreateInput.client_transaction_id:
   * generate once per return attempt, resend the SAME value on any retry. */
  client_transaction_id: string
  refund_method: PaymentInput['payment_method']
  reason?: string
  lines: SaleReturnLineInput[]
  /** M14: only consulted server-side if the computed refund total is at or
   * above the store's configured approval threshold. Omit on the first
   * attempt; the server rejects with error code APPROVAL_REQUIRED (and
   * names the threshold) if these turn out to be needed. */
  approver_username?: string
  approver_password?: string
}

export interface VoidSaleInput {
  store_id: number
  return_date: string
  client_transaction_id: string
  refund_method: PaymentInput['payment_method']
  reason?: string
  approver_username?: string
  approver_password?: string
}

export interface SaleReturnItem {
  id: number
  sale_item_id: number
  quantity: string
  unit_price_refunded: string
  discount_refunded: string
  tax_refunded: string
  unit_cost_refunded: string
  restock: boolean
  product_id: number | null
  product_name: string | null
  product_sku: string | null
}

export interface SaleReturn {
  id: number
  sale_id: number
  store_id: number
  return_number: string
  client_transaction_id: string
  reason: string | null
  refund_method: string
  refund_amount: string
  processed_by: number | null
  approval_required: boolean
  approved_by: number | null
  created_at: string
  items: SaleReturnItem[]
}

export function getReturnEligibility(saleId: number): Promise<SaleReturnEligibility> {
  return apiFetch<SaleReturnEligibility>(`/api/v1/sales/${saleId}/return-eligibility`)
}

export function createSaleReturn(
  saleId: number,
  input: SaleReturnCreateInput,
): Promise<SaleReturn> {
  return apiFetch<SaleReturn>(`/api/v1/sales/${saleId}/returns`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function voidSale(saleId: number, input: VoidSaleInput): Promise<SaleReturn> {
  return apiFetch<SaleReturn>(`/api/v1/sales/${saleId}/void`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
