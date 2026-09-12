import { apiFetch, buildQuery } from './client'

export interface ReplenishmentSuggestion {
  product_id: number
  store_id: number
  sku: string
  name: string
  reorder_point: string
  current_qty_on_hand: string
  inbound_transfer_qty: string
  open_purchase_order_qty: string
  inventory_position: string
  shortfall: string
  suggested_transfer_quantity: string
  suggested_purchase_quantity: string
  sister_store_surplus_source_store_id: number | null
}

export function getReplenishmentSuggestions(
  params: { store_id?: number } = {},
): Promise<ReplenishmentSuggestion[]> {
  return apiFetch<ReplenishmentSuggestion[]>(
    `/api/v1/replenishment/suggestions${buildQuery(params)}`,
  )
}

// --- M9: supplier-product catalog / pricing --------------------------------

export interface SupplierProduct {
  id: number
  supplier_id: number
  product_id: number
  supplier_sku: string | null
  pack_size: string
  unit_cost: string
  minimum_order_quantity: string | null
  lead_time_days: number | null
  effective_date: string
  is_active: boolean
}

export interface SupplierProductCreateInput {
  supplier_id: number
  product_id: number
  supplier_sku?: string
  pack_size?: string
  unit_cost: string
  minimum_order_quantity?: string
  lead_time_days?: number
  effective_date?: string
}

export function listSupplierProducts(
  params: { supplier_id?: number; product_id?: number; is_active?: boolean } = {},
): Promise<SupplierProduct[]> {
  return apiFetch<SupplierProduct[]>(`/api/v1/replenishment/supplier-products${buildQuery(params)}`)
}

export function createSupplierProduct(input: SupplierProductCreateInput): Promise<SupplierProduct> {
  return apiFetch<SupplierProduct>('/api/v1/replenishment/supplier-products', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// --- M9: replenishment plans ------------------------------------------

export type ReplenishmentPlanStatus =
  'RECOMMENDED' | 'APPROVED' | 'EXECUTED' | 'STALE' | 'CANCELLED'

export interface ReplenishmentPlan {
  id: number
  generation_batch_id: string
  destination_store_id: number
  product_id: number
  needed_quantity: string
  suggested_quantity: string
  source_type: 'SUPPLIER' | 'TRANSFER'
  supplier_id: number | null
  source_store_id: number | null
  suggested_unit_cost: string | null
  urgency: 'NORMAL' | 'URGENT'
  reason: string
  status: ReplenishmentPlanStatus

  approved_by: number | null
  approved_at: string | null

  executed_by: number | null
  executed_at: string | null
  executed_quantity: string | null
  executed_unit_cost: string | null
  generated_purchase_order_id: number | null
  generated_transfer_id: number | null

  stale_detected_at: string | null
  stale_reason: string | null

  cancelled_by: number | null
  cancelled_at: string | null
  cancellation_reason: string | null

  created_at: string
}

export interface ReplenishmentPlanDetail extends ReplenishmentPlan {
  remaining_need: string
  fulfilled: boolean | null
}

export function generateReplenishmentPlans(input: {
  store_id?: number
  product_ids?: number[]
}): Promise<ReplenishmentPlan[]> {
  return apiFetch<ReplenishmentPlan[]>('/api/v1/replenishment/plans/generate', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function listReplenishmentPlans(
  params: { store_id?: number; status_filter?: string; generation_batch_id?: string } = {},
): Promise<ReplenishmentPlan[]> {
  return apiFetch<ReplenishmentPlan[]>(`/api/v1/replenishment/plans${buildQuery(params)}`)
}

export function getReplenishmentPlan(id: number): Promise<ReplenishmentPlanDetail> {
  return apiFetch<ReplenishmentPlanDetail>(`/api/v1/replenishment/plans/${id}`)
}

export function approveReplenishmentPlan(id: number): Promise<ReplenishmentPlan> {
  return apiFetch<ReplenishmentPlan>(`/api/v1/replenishment/plans/${id}/approve`, {
    method: 'POST',
  })
}

export function executeReplenishmentPlan(
  id: number,
  clientTransactionId: string,
): Promise<ReplenishmentPlan> {
  return apiFetch<ReplenishmentPlan>(`/api/v1/replenishment/plans/${id}/execute`, {
    method: 'POST',
    body: JSON.stringify({ client_transaction_id: clientTransactionId }),
  })
}

export function cancelReplenishmentPlan(id: number, reason?: string): Promise<ReplenishmentPlan> {
  return apiFetch<ReplenishmentPlan>(`/api/v1/replenishment/plans/${id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

// --- M9: metrics and exceptions -----------------------------------------

export interface SupplyChainMetrics {
  store_id: number | null
  open_purchase_order_qty: string
  inbound_transfer_qty: string
  products_below_reorder_point: number
  products_below_minimum: number
  overdue_purchase_order_count: number
}

export interface SupplyChainException {
  code: string
  product_id: number | null
  store_id: number | null
  purchase_order_id: number | null
  plan_id: number | null
  message: string
}

export function getSupplyChainMetrics(
  params: { store_id?: number } = {},
): Promise<SupplyChainMetrics> {
  return apiFetch<SupplyChainMetrics>(`/api/v1/replenishment/metrics${buildQuery(params)}`)
}

export function getSupplyChainExceptions(
  params: { store_id?: number } = {},
): Promise<SupplyChainException[]> {
  return apiFetch<SupplyChainException[]>(`/api/v1/replenishment/exceptions${buildQuery(params)}`)
}
