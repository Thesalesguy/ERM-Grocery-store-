import { apiFetch, buildQuery } from './client'

export interface TransferLine {
  id: number
  transfer_id: number
  source_product_id: number
  destination_product_id: number
  requested_quantity: string
  shipped_quantity: string
  received_quantity: string
  unit_cost_at_shipment: string | null
}

export interface Transfer {
  id: number
  from_store_id: number
  to_store_id: number
  transfer_number: string
  status: 'DRAFT' | 'SHIPPED' | 'CANCELLED'
  requested_date: string
  notes: string | null
  requested_by: number | null
  shipped_by: number | null
  shipped_at: string | null
  cancelled_by: number | null
  cancelled_at: string | null
  created_at: string
}

export interface TransferWithLines extends Transfer {
  lines: TransferLine[]
}

export interface TransferLineCreateInput {
  source_product_id: number
  requested_quantity: string
  destination_product_id?: number
}

export interface TransferCreateInput {
  from_store_id: number
  to_store_id: number
  requested_date: string
  notes?: string
  lines: TransferLineCreateInput[]
}

export interface TransferReceiptItem {
  id: number
  inter_store_transfer_line_id: number
  quantity_received: string
}

export interface TransferReceipt {
  id: number
  transfer_id: number
  store_id: number
  client_transaction_id: string
  received_date: string
  received_by: number | null
  notes: string | null
  created_at: string
  items: TransferReceiptItem[]
}

export interface InventoryInTransitReconciliation {
  gl_in_transit_balance: string
  outstanding_in_transit_total: string
  discrepancy: string
}

export function listTransfers(
  params: { from_store_id?: number; to_store_id?: number; status?: string } = {},
): Promise<Transfer[]> {
  return apiFetch<Transfer[]>(`/api/v1/transfers${buildQuery(params)}`)
}

export function getTransfer(id: number): Promise<TransferWithLines> {
  return apiFetch<TransferWithLines>(`/api/v1/transfers/${id}`)
}

export function createTransfer(input: TransferCreateInput): Promise<TransferWithLines> {
  return apiFetch<TransferWithLines>('/api/v1/transfers', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function shipTransfer(
  id: number,
  input: {
    client_transaction_id: string
    lines: { transfer_line_id: number; quantity_to_ship: string }[]
  },
): Promise<TransferWithLines> {
  return apiFetch<TransferWithLines>(`/api/v1/transfers/${id}/ship`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function receiveTransfer(
  id: number,
  input: {
    received_date: string
    notes?: string
    client_transaction_id: string
    lines: { transfer_line_id: number; quantity_received: string }[]
  },
): Promise<TransferReceipt> {
  return apiFetch<TransferReceipt>(`/api/v1/transfers/${id}/receipts`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function listTransferReceipts(id: number): Promise<TransferReceipt[]> {
  return apiFetch<TransferReceipt[]>(`/api/v1/transfers/${id}/receipts`)
}

export function cancelTransfer(id: number, reason?: string): Promise<Transfer> {
  return apiFetch<Transfer>(`/api/v1/transfers/${id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function getInventoryInTransitReconciliation(): Promise<InventoryInTransitReconciliation> {
  return apiFetch<InventoryInTransitReconciliation>(
    '/api/v1/transfers/reports/inventory-in-transit-reconciliation',
  )
}
