import { apiFetch, buildQuery } from './client'

export interface StockCountLine {
  id: number
  stock_count_id: number
  product_id: number
  expected_quantity: string | null
  expected_unit_cost: string | null
  counted_quantity: string | null
  counted_by: number | null
  counted_at: string | null
  recount_number: number
  variance_quantity: string | null
}

export interface StockCount {
  id: number
  store_id: number
  count_number: string
  status: 'DRAFT' | 'OPEN' | 'COUNTED' | 'REVIEWED' | 'POSTED' | 'CANCELLED'
  category_id: number | null
  notes: string | null
  created_by: number | null
  opened_by: number | null
  opened_at: string | null
  reviewed_by: number | null
  reviewed_at: string | null
  posted_by: number | null
  posted_at: string | null
  cancelled_by: number | null
  cancelled_at: string | null
  created_at: string
}

export interface StockCountWithLines extends StockCount {
  lines: StockCountLine[]
}

export interface StockCountCreateInput {
  store_id: number
  category_id?: number
  product_ids?: number[]
  notes?: string
}

export function listStockCounts(
  params: { store_id?: number; status?: string } = {},
): Promise<StockCount[]> {
  return apiFetch<StockCount[]>(`/api/v1/inventory/stock-counts${buildQuery(params)}`)
}

export function getStockCount(id: number): Promise<StockCountWithLines> {
  return apiFetch<StockCountWithLines>(`/api/v1/inventory/stock-counts/${id}`)
}

export function createStockCount(input: StockCountCreateInput): Promise<StockCountWithLines> {
  return apiFetch<StockCountWithLines>('/api/v1/inventory/stock-counts', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function openStockCount(id: number): Promise<StockCountWithLines> {
  return apiFetch<StockCountWithLines>(`/api/v1/inventory/stock-counts/${id}/open`, {
    method: 'POST',
  })
}

export function recordCountEntry(
  id: number,
  input: { product_id: number; counted_quantity: string },
): Promise<StockCountLine> {
  return apiFetch<StockCountLine>(`/api/v1/inventory/stock-counts/${id}/entries`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function markStockCountCounted(id: number): Promise<StockCount> {
  return apiFetch<StockCount>(`/api/v1/inventory/stock-counts/${id}/mark-counted`, {
    method: 'POST',
  })
}

export function reviewStockCount(id: number): Promise<StockCount> {
  return apiFetch<StockCount>(`/api/v1/inventory/stock-counts/${id}/review`, { method: 'POST' })
}

export function reopenStockCount(id: number, reason?: string): Promise<StockCount> {
  return apiFetch<StockCount>(`/api/v1/inventory/stock-counts/${id}/reopen`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function postStockCount(id: number): Promise<StockCount> {
  return apiFetch<StockCount>(`/api/v1/inventory/stock-counts/${id}/post`, { method: 'POST' })
}

export function cancelStockCount(id: number, reason?: string): Promise<StockCount> {
  return apiFetch<StockCount>(`/api/v1/inventory/stock-counts/${id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}
