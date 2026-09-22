import { apiFetch } from './client'

export interface ShiftOpenInput {
  store_id: number
  opening_float: string
  /** Idempotency key — same convention as SaleCreateInput.client_transaction_id:
   * generate once per open attempt, resend the SAME value on any retry. */
  client_transaction_id: string
}

export interface ShiftCloseInput {
  closing_counted_amount: string
  /** A SEPARATE idempotency key from the shift's own open key. */
  client_transaction_id: string
}

export interface Shift {
  id: number
  store_id: number
  cashier_id: number
  status: 'OPEN' | 'CLOSED'
  opening_float: string
  opened_at: string
  closed_at: string | null
  closing_counted_amount: string | null
  expected_cash_amount: string | null
  variance_amount: string | null
  closed_by: number | null
  created_at: string
}

export function openShift(input: ShiftOpenInput): Promise<Shift> {
  return apiFetch<Shift>('/api/v1/shifts', { method: 'POST', body: JSON.stringify(input) })
}

/** The caller's own currently open shift, or null. */
export function getActiveShift(): Promise<Shift | null> {
  return apiFetch<Shift | null>('/api/v1/shifts/active')
}

export function getShift(id: number): Promise<Shift> {
  return apiFetch<Shift>(`/api/v1/shifts/${id}`)
}

export function closeShift(id: number, input: ShiftCloseInput): Promise<Shift> {
  return apiFetch<Shift>(`/api/v1/shifts/${id}/close`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export interface CashMovementInput {
  movement_type: 'PAID_IN' | 'PAID_OUT'
  amount: string
  reason: string
  client_transaction_id: string
}

export interface CashMovement {
  id: number
  shift_id: number
  movement_type: 'PAID_IN' | 'PAID_OUT'
  amount: string
  reason: string
  created_by: number
  created_at: string
}

export function recordCashMovement(
  shiftId: number,
  input: CashMovementInput,
): Promise<CashMovement> {
  return apiFetch<CashMovement>(`/api/v1/shifts/${shiftId}/cash-movements`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function listCashMovements(shiftId: number): Promise<CashMovement[]> {
  return apiFetch<CashMovement[]>(`/api/v1/shifts/${shiftId}/cash-movements`)
}
