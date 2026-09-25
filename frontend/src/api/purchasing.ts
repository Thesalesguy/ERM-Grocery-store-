import { apiFetch, buildQuery } from './client'

export interface Supplier {
  id: number
  name: string
  code: string | null
  contact_name: string | null
  phone: string | null
  email: string | null
  address: string | null
  tax_id: string | null
  is_active: boolean
}

export interface SupplierCreateInput {
  name: string
  code?: string
  contact_name?: string
  phone?: string
  email?: string
  address?: string
  tax_id?: string
}

export interface SupplierListParams {
  is_active?: boolean
  search?: string
}

export function listSuppliers(params: SupplierListParams = {}): Promise<Supplier[]> {
  return apiFetch<Supplier[]>(`/api/v1/purchasing/suppliers${buildQuery(params)}`)
}

export function createSupplier(input: SupplierCreateInput): Promise<Supplier> {
  return apiFetch<Supplier>('/api/v1/purchasing/suppliers', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function setSupplierActive(id: number, active: boolean): Promise<Supplier> {
  return apiFetch<Supplier>(
    `/api/v1/purchasing/suppliers/${id}/${active ? 'activate' : 'deactivate'}`,
    {
      method: 'POST',
    },
  )
}

export interface PurchaseOrderItemInput {
  product_id: number
  quantity_ordered: string
  unit_cost: string
}

export interface PurchaseOrderCreateInput {
  store_id: number
  supplier_id: number
  order_date: string
  expected_date?: string
  notes?: string
  client_transaction_id: string
  lines: PurchaseOrderItemInput[]
}

export interface PurchaseOrderItem {
  id: number
  product_id: number
  quantity_ordered: string
  quantity_received: string
  unit_cost: string
  quantity_remaining: string
  is_over_received: boolean
  product_name: string | null
  product_sku: string | null
}

export interface PurchaseOrder {
  id: number
  store_id: number
  supplier_id: number
  purchase_number: string
  status: 'DRAFT' | 'ORDERED' | 'PARTIALLY_RECEIVED' | 'RECEIVED' | 'CANCELLED'
  order_date: string
  expected_date: string | null
  notes: string | null
  created_by: number | null
  created_at: string
  items: PurchaseOrderItem[]
  supplier_name: string | null
}

export interface PurchaseOrderListParams {
  store_id?: number
  supplier_id?: number
  status_filter?: string
}

export function listPurchaseOrders(params: PurchaseOrderListParams = {}): Promise<PurchaseOrder[]> {
  return apiFetch<PurchaseOrder[]>(`/api/v1/purchasing/purchase-orders${buildQuery(params)}`)
}

export function getPurchaseOrder(id: number): Promise<PurchaseOrder> {
  return apiFetch<PurchaseOrder>(`/api/v1/purchasing/purchase-orders/${id}`)
}

export function createPurchaseOrder(input: PurchaseOrderCreateInput): Promise<PurchaseOrder> {
  return apiFetch<PurchaseOrder>('/api/v1/purchasing/purchase-orders', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function submitPurchaseOrder(id: number): Promise<PurchaseOrder> {
  return apiFetch<PurchaseOrder>(`/api/v1/purchasing/purchase-orders/${id}/submit`, {
    method: 'POST',
  })
}

export function cancelPurchaseOrder(id: number, reason?: string): Promise<PurchaseOrder> {
  return apiFetch<PurchaseOrder>(`/api/v1/purchasing/purchase-orders/${id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export interface GoodsReceiptLineInput {
  purchase_order_item_id: number
  quantity_received: string
  unit_cost: string
  condition_notes?: string
}

export interface GoodsReceiptCreateInput {
  received_date: string
  notes?: string
  client_transaction_id: string
  lines: GoodsReceiptLineInput[]
}

export interface GoodsReceiptItem {
  id: number
  purchase_order_item_id: number
  quantity_received: string
  unit_cost: string
  condition_notes: string | null
}

export interface GoodsReceipt {
  id: number
  purchase_order_id: number
  store_id: number
  client_transaction_id: string
  received_date: string
  received_by: number | null
  notes: string | null
  created_at: string
  items: GoodsReceiptItem[]
}

export function receiveGoods(
  purchaseOrderId: number,
  input: GoodsReceiptCreateInput,
): Promise<GoodsReceipt> {
  return apiFetch<GoodsReceipt>(`/api/v1/purchasing/purchase-orders/${purchaseOrderId}/receive`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export interface PurchaseReturnLineInput {
  product_id: number
  quantity: string
}

export interface PurchaseReturnCreateInput {
  store_id: number
  return_date: string
  reason?: string
  client_transaction_id: string
  lines: PurchaseReturnLineInput[]
}

export interface PurchaseReturn {
  id: number
  purchase_order_id: number
  store_id: number
  client_transaction_id: string
  return_date: string
  reason: string | null
  created_by: number | null
  created_at: string
  items: { id: number; product_id: number; quantity: string; unit_cost: string }[]
}

export function createPurchaseReturn(
  purchaseOrderId: number,
  input: PurchaseReturnCreateInput,
): Promise<PurchaseReturn> {
  return apiFetch<PurchaseReturn>(`/api/v1/purchasing/purchase-orders/${purchaseOrderId}/returns`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
