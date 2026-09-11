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
