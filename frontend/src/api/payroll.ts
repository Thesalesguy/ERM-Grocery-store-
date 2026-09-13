import { apiFetch, buildQuery } from './client'

export type PayrollPeriodStatus =
  'DRAFT' | 'OPEN' | 'CALCULATED' | 'APPROVED' | 'POSTED' | 'CANCELLED'

export interface PayrollPeriod {
  id: number
  store_id: number
  payroll_run_id: string | null
  period_start: string
  period_end: string
  pay_date: string
  status: PayrollPeriodStatus
  calculated_by: number | null
  calculated_at: string | null
  approved_by: number | null
  approved_at: string | null
  journal_entry_id: number | null
  posted_by: number | null
  posted_at: string | null
  total_gross: string
  total_deductions: string
  total_employer_contributions: string
  total_net_pay: string
}

export interface PayrollEmployeeResult {
  id: number
  payroll_period_id: number
  employee_id: number
  status: 'DRAFT' | 'FINAL'
  store_id: number
  department_id: number | null
  position_id: number | null
  pay_type: string
  pay_rate: string
  currency: string
  regular_hours: string
  overtime_hours: string
  gross_pay: string
  total_deductions: string
  total_employer_contributions: string
  net_pay: string
}

export function listPayrollPeriods(
  params: { status_filter?: string } = {},
): Promise<PayrollPeriod[]> {
  return apiFetch<PayrollPeriod[]>(`/api/v1/payroll/periods${buildQuery(params)}`)
}

export function getPayrollPeriod(id: number): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}`)
}

export function createPayrollPeriod(input: {
  store_id: number
  period_start: string
  period_end: string
  pay_date: string
  payroll_run_id?: string
}): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>('/api/v1/payroll/periods', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function openPayrollPeriod(id: number): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/open`, { method: 'POST' })
}

export function calculatePayrollPeriod(
  id: number,
  clientTransactionId?: string,
): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/calculate`, {
    method: 'POST',
    body: JSON.stringify({ client_transaction_id: clientTransactionId }),
  })
}

export function approvePayrollPeriod(id: number): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/approve`, { method: 'POST' })
}

export function cancelPayrollPeriod(id: number, reason: string): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function postPayrollPeriod(
  id: number,
  clientTransactionId?: string,
): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/post`, {
    method: 'POST',
    body: JSON.stringify({ client_transaction_id: clientTransactionId }),
  })
}

export function reversePayrollPeriod(id: number, reason: string): Promise<PayrollPeriod> {
  return apiFetch<PayrollPeriod>(`/api/v1/payroll/periods/${id}/reverse`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function listPayrollEmployeeResults(
  payrollPeriodId: number,
): Promise<PayrollEmployeeResult[]> {
  return apiFetch<PayrollEmployeeResult[]>(`/api/v1/payroll/periods/${payrollPeriodId}/results`)
}
