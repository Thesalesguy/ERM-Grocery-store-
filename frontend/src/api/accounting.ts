import { apiFetch, buildQuery } from './client'

export interface Account {
  id: number
  code: string
  name: string
  account_type: 'ASSET' | 'LIABILITY' | 'EQUITY' | 'REVENUE' | 'EXPENSE'
  normal_balance: 'DEBIT' | 'CREDIT'
  is_system: boolean
  is_active: boolean
  description: string | null
}

export function listAccounts(): Promise<Account[]> {
  return apiFetch<Account[]>('/api/v1/accounting/accounts')
}

export interface JournalLine {
  id: number
  account_id: number
  account_code: string
  account_name: string
  debit: string
  credit: string
  product_id: number | null
  description: string | null
}

export interface JournalEntry {
  id: number
  journal_number: string
  store_id: number
  posting_date: string
  entry_type: 'STANDARD' | 'REVERSAL'
  source_type: string
  source_id: number | null
  reversal_of_id: number | null
  memo: string | null
  created_by: number | null
  created_at: string
  is_reversed: boolean
  lines: JournalLine[]
}

export interface JournalListParams {
  store_id?: number
  source_type?: string
  date_from?: string
  date_to?: string
}

export function listJournals(params: JournalListParams = {}): Promise<JournalEntry[]> {
  return apiFetch<JournalEntry[]>(`/api/v1/accounting/journals${buildQuery(params)}`)
}

export function getJournal(id: number): Promise<JournalEntry> {
  return apiFetch<JournalEntry>(`/api/v1/accounting/journals/${id}`)
}

export function reverseJournal(id: number, reason: string): Promise<JournalEntry> {
  return apiFetch<JournalEntry>(`/api/v1/accounting/journals/${id}/reverse`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export interface TrialBalanceRow {
  account_code: string
  account_name: string
  account_type: string
  normal_balance: string
  total_debit: string
  total_credit: string
}

export interface TrialBalance {
  store_id: number | null
  date_from: string | null
  date_to: string | null
  rows: TrialBalanceRow[]
  total_debit: string
  total_credit: string
}

export interface ReportParams {
  store_id?: number
  date_from?: string
  date_to?: string
}

export function getTrialBalance(params: ReportParams = {}): Promise<TrialBalance> {
  return apiFetch<TrialBalance>(`/api/v1/accounting/reports/trial-balance${buildQuery(params)}`)
}

export interface ProfitAndLoss {
  store_id: number | null
  date_from: string | null
  date_to: string | null
  net_sales: string
  cogs: string
  gross_profit: string
  other_income: string
  operating_expenses: string
  net_income: string
}

export function getProfitAndLoss(params: ReportParams = {}): Promise<ProfitAndLoss> {
  return apiFetch<ProfitAndLoss>(`/api/v1/accounting/reports/profit-loss${buildQuery(params)}`)
}

export interface InventoryReconciliationRow {
  store_id: number
  gl_inventory_balance: string
  operational_valuation: string
  discrepancy: string
}

export function getInventoryReconciliation(
  params: { store_id?: number; as_of?: string } = {},
): Promise<{ rows: InventoryReconciliationRow[] }> {
  return apiFetch<{ rows: InventoryReconciliationRow[] }>(
    `/api/v1/accounting/reports/inventory-reconciliation${buildQuery(params)}`,
  )
}
