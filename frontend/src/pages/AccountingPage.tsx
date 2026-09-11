import { useEffect, useState } from 'react'
import * as accountingApi from '../api/accounting'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

type Tab = 'accounts' | 'journals' | 'trial-balance' | 'profit-loss' | 'reconciliation'

const TABS: { id: Tab; label: string }[] = [
  { id: 'journals', label: 'Journal' },
  { id: 'accounts', label: 'Chart of Accounts' },
  { id: 'trial-balance', label: 'Trial Balance' },
  { id: 'profit-loss', label: 'Profit & Loss' },
  { id: 'reconciliation', label: 'Inventory Reconciliation' },
]

function StatusBadge({ entry }: { entry: accountingApi.JournalEntry }) {
  if (entry.entry_type === 'REVERSAL') {
    return (
      <span className="rounded bg-purple-100 px-2 py-0.5 text-xs text-purple-800">REVERSAL</span>
    )
  }
  if (entry.is_reversed) {
    return <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-700">REVERSED</span>
  }
  return <span className="rounded bg-green-100 px-2 py-0.5 text-xs text-green-800">POSTED</span>
}

function AccountsTab() {
  const [accounts, setAccounts] = useState<accountingApi.Account[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    accountingApi
      .listAccounts()
      .then(setAccounts)
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <p className="mt-4 text-gray-500">Loading…</p>

  return (
    <table className="mt-4 w-full text-left text-sm">
      <thead>
        <tr className="border-b border-gray-200 text-gray-500">
          <th className="py-2 pr-4">Code</th>
          <th className="py-2 pr-4">Name</th>
          <th className="py-2 pr-4">Type</th>
          <th className="py-2 pr-4">Normal balance</th>
        </tr>
      </thead>
      <tbody>
        {accounts.map((a) => (
          <tr key={a.id} className="border-b border-gray-100">
            <td className="py-2 pr-4 font-mono text-xs">{a.code}</td>
            <td className="py-2 pr-4">{a.name}</td>
            <td className="py-2 pr-4">{a.account_type}</td>
            <td className="py-2 pr-4">{a.normal_balance}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function JournalDetail({
  entryId,
  onClose,
  onReversed,
  canReverse,
}: {
  entryId: number
  onClose: () => void
  onReversed: () => void
  canReverse: boolean
}) {
  const [entry, setEntry] = useState<accountingApi.JournalEntry | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reversing, setReversing] = useState(false)

  function load() {
    accountingApi
      .getJournal(entryId)
      .then(setEntry)
      .catch(() => setError('Could not load journal entry.'))
  }

  useEffect(() => {
    // Intentional: load on mount and whenever the selected entry changes,
    // the same pattern PurchasingPage's initial fetch uses.
    // oxlint-disable-next-line react/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entryId])

  async function handleReverse() {
    const reason = window.prompt('Reason for reversal?')
    if (!reason) return
    setReversing(true)
    setError(null)
    try {
      await accountingApi.reverseJournal(entryId, reason)
      load()
      onReversed()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Reversal failed.')
    } finally {
      setReversing(false)
    }
  }

  if (error) return <p className="mt-4 text-red-600">{error}</p>
  if (!entry) return <p className="mt-4 text-gray-500">Loading…</p>

  return (
    <div className="mt-4 rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-mono text-sm font-semibold text-gray-900">{entry.journal_number}</h3>
          <p className="text-xs text-gray-500">
            {entry.source_type} #{entry.source_id} · {entry.posting_date}
          </p>
        </div>
        <StatusBadge entry={entry} />
      </div>
      {entry.memo && <p className="mt-2 text-sm text-gray-600">{entry.memo}</p>}

      <table className="mt-4 w-full text-left text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-gray-500">
            <th className="py-1 pr-4">Account</th>
            <th className="py-1 pr-4 text-right">Debit</th>
            <th className="py-1 pr-4 text-right">Credit</th>
          </tr>
        </thead>
        <tbody>
          {entry.lines.map((line) => (
            <tr key={line.id} className="border-b border-gray-100">
              <td className="py-1 pr-4">
                <span className="font-mono text-xs text-gray-500">{line.account_code}</span>{' '}
                {line.account_name}
              </td>
              <td className="py-1 pr-4 text-right">
                {Number(line.debit) > 0 ? line.debit : ''}
              </td>
              <td className="py-1 pr-4 text-right">
                {Number(line.credit) > 0 ? line.credit : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {canReverse &&
        entry.entry_type === 'STANDARD' &&
        !entry.is_reversed &&
        entry.source_type !== 'MANUAL' && (
          <p className="mt-2 text-xs text-gray-500">
            This entry was posted automatically from a {entry.source_type.toLowerCase()} and
            cannot be reversed here — doing so would correct the accounting without undoing the
            operational transaction (inventory, payment, stock) that produced it.
          </p>
        )}
      <div className="mt-4 flex gap-2">
        {canReverse &&
          entry.entry_type === 'STANDARD' &&
          !entry.is_reversed &&
          entry.source_type === 'MANUAL' && (
            <button
              onClick={handleReverse}
              disabled={reversing}
              className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
            >
              {reversing ? 'Reversing…' : 'Reverse entry'}
            </button>
          )}
        <button
          onClick={onClose}
          className="rounded px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100"
        >
          Close
        </button>
      </div>
    </div>
  )
}

function JournalsTab() {
  const { hasPermission } = useAuth()
  const [entries, setEntries] = useState<accountingApi.JournalEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  function load() {
    setLoading(true)
    accountingApi
      .listJournals()
      .then(setEntries)
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // Intentional: load the list once on mount, the same pattern
    // PurchasingPage's initial fetch uses.
    // oxlint-disable-next-line react/set-state-in-effect
    load()
  }, [])

  return (
    <div>
      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {!loading && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-500">
              <th className="py-2 pr-4">Journal #</th>
              <th className="py-2 pr-4">Date</th>
              <th className="py-2 pr-4">Source</th>
              <th className="py-2 pr-4">Memo</th>
              <th className="py-2 pr-4">Status</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr
                key={entry.id}
                className="cursor-pointer border-b border-gray-100 hover:bg-gray-50"
                onClick={() => setSelectedId(entry.id)}
              >
                <td className="py-2 pr-4 font-mono text-xs text-blue-600">
                  {entry.journal_number}
                </td>
                <td className="py-2 pr-4">{entry.posting_date}</td>
                <td className="py-2 pr-4">
                  {entry.source_type} #{entry.source_id}
                </td>
                <td className="py-2 pr-4">{entry.memo}</td>
                <td className="py-2 pr-4">
                  <StatusBadge entry={entry} />
                </td>
              </tr>
            ))}
            {entries.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-center text-gray-400">
                  No journal entries yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {selectedId !== null && (
        <JournalDetail
          entryId={selectedId}
          onClose={() => setSelectedId(null)}
          onReversed={load}
          canReverse={hasPermission('accounting.reverse')}
        />
      )}
    </div>
  )
}

function TrialBalanceTab() {
  const [report, setReport] = useState<accountingApi.TrialBalance | null>(null)

  useEffect(() => {
    accountingApi.getTrialBalance().then(setReport)
  }, [])

  if (!report) return <p className="mt-4 text-gray-500">Loading…</p>

  return (
    <table className="mt-4 w-full text-left text-sm">
      <thead>
        <tr className="border-b border-gray-200 text-gray-500">
          <th className="py-2 pr-4">Account</th>
          <th className="py-2 pr-4 text-right">Debit</th>
          <th className="py-2 pr-4 text-right">Credit</th>
        </tr>
      </thead>
      <tbody>
        {report.rows.map((row) => (
          <tr key={row.account_code} className="border-b border-gray-100">
            <td className="py-2 pr-4">
              <span className="font-mono text-xs text-gray-500">{row.account_code}</span>{' '}
              {row.account_name}
            </td>
            <td className="py-2 pr-4 text-right">{row.total_debit}</td>
            <td className="py-2 pr-4 text-right">{row.total_credit}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr className="font-semibold">
          <td className="py-2 pr-4">Total</td>
          <td className="py-2 pr-4 text-right">{report.total_debit}</td>
          <td className="py-2 pr-4 text-right">{report.total_credit}</td>
        </tr>
      </tfoot>
    </table>
  )
}

function ProfitAndLossTab() {
  const [pnl, setPnl] = useState<accountingApi.ProfitAndLoss | null>(null)

  useEffect(() => {
    accountingApi.getProfitAndLoss().then(setPnl)
  }, [])

  if (!pnl) return <p className="mt-4 text-gray-500">Loading…</p>

  const row = (label: string, value: string, emphasize = false) => (
    <div
      className={`flex justify-between border-b border-gray-100 py-2 ${emphasize ? 'font-semibold' : ''}`}
    >
      <span>{label}</span>
      <span>{value}</span>
    </div>
  )

  return (
    <div className="mt-4 max-w-md">
      {row('Net sales', pnl.net_sales)}
      {row('Cost of goods sold', `(${pnl.cogs})`)}
      {row('Gross profit', pnl.gross_profit, true)}
      {row('Other income', pnl.other_income)}
      {row('Operating expenses', `(${pnl.operating_expenses})`)}
      {row('Net income', pnl.net_income, true)}
    </div>
  )
}

function ReconciliationTab() {
  const [rows, setRows] = useState<accountingApi.InventoryReconciliationRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    accountingApi
      .getInventoryReconciliation()
      .then((r) => setRows(r.rows))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <p className="mt-4 text-gray-500">Loading…</p>

  return (
    <table className="mt-4 w-full text-left text-sm">
      <thead>
        <tr className="border-b border-gray-200 text-gray-500">
          <th className="py-2 pr-4">Store</th>
          <th className="py-2 pr-4 text-right">GL Inventory balance</th>
          <th className="py-2 pr-4 text-right">Operational valuation</th>
          <th className="py-2 pr-4 text-right">Discrepancy</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.store_id} className="border-b border-gray-100">
            <td className="py-2 pr-4">Store #{row.store_id}</td>
            <td className="py-2 pr-4 text-right">{row.gl_inventory_balance}</td>
            <td className="py-2 pr-4 text-right">{row.operational_valuation}</td>
            <td
              className={`py-2 pr-4 text-right font-medium ${
                Number(row.discrepancy) === 0 ? 'text-green-700' : 'text-red-600'
              }`}
            >
              {row.discrepancy}
            </td>
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={4} className="py-6 text-center text-gray-400">
              No inventory activity yet.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}

export function AccountingPage() {
  const [tab, setTab] = useState<Tab>('journals')

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Accounting</h1>
      <div className="mt-4 flex gap-1 border-b border-gray-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-sm font-medium ${
              tab === t.id
                ? 'border-b-2 border-blue-600 text-blue-600'
                : 'text-gray-500 hover:text-gray-800'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'accounts' && <AccountsTab />}
      {tab === 'journals' && <JournalsTab />}
      {tab === 'trial-balance' && <TrialBalanceTab />}
      {tab === 'profit-loss' && <ProfitAndLossTab />}
      {tab === 'reconciliation' && <ReconciliationTab />}
    </div>
  )
}
