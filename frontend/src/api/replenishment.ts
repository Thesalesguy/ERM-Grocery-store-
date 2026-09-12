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
  return apiFetch<ReplenishmentSuggestion[]>(`/api/v1/replenishment/suggestions${buildQuery(params)}`)
}
