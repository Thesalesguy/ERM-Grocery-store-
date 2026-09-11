import { apiFetch, buildQuery } from './client'

export interface StockLevel {
  id: number
  store_id: number
  sku: string
  name: string
  current_qty_on_hand: string
  current_cost: string
  reorder_point: string | null
  is_low_stock: boolean
  is_active: boolean
}

export interface InventoryMovement {
  id: number
  store_id: number
  product_id: number
  movement_type: string
  quantity_delta: string
  unit_cost_at_movement: string
  resulting_quantity_on_hand: string
  reference_type: string
  reference_id: number | null
  reason: string | null
  created_by: number | null
  created_at: string
}

export interface StockAdjustmentInput {
  product_id: number
  quantity_delta: string
  reason_code: 'DAMAGE' | 'THEFT' | 'EXPIRY' | 'STOCKTAKE_CORRECTION' | 'OTHER'
  notes?: string
}

export function listStock(
  params: { store_id?: number; search?: string; low_stock_only?: boolean } = {},
): Promise<StockLevel[]> {
  return apiFetch<StockLevel[]>(`/api/v1/inventory/stock${buildQuery(params)}`)
}

export function listMovements(params: { product_id?: number } = {}): Promise<InventoryMovement[]> {
  return apiFetch<InventoryMovement[]>(`/api/v1/inventory/movements${buildQuery(params)}`)
}

export function createAdjustment(input: StockAdjustmentInput) {
  return apiFetch('/api/v1/inventory/adjustments', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}
