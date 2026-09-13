import { useEffect, useState } from 'react'
import * as hrApi from '../api/hr'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const STATUS_STYLES: Record<string, string> = {
  ACTIVE: 'bg-green-100 text-green-700',
  ON_LEAVE: 'bg-amber-100 text-amber-700',
  SUSPENDED: 'bg-orange-100 text-orange-700',
  TERMINATED: 'bg-red-100 text-red-700',
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium uppercase ${STATUS_STYLES[status] ?? 'bg-gray-100 text-gray-700'}`}
    >
      {status}
    </span>
  )
}

function HireEmployeeForm({ storeId, onHired }: { storeId: number; onHired: () => void }) {
  const [employeeNumber, setEmployeeNumber] = useState('')
  const [legalName, setLegalName] = useState('')
  const [hireDate, setHireDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await hrApi.hireEmployee({
        employee_number: employeeNumber,
        legal_name: legalName,
        hire_date: hireDate,
        store_id: storeId,
      })
      setEmployeeNumber('')
      setLegalName('')
      setHireDate('')
      onHired()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not hire employee.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-gray-900">Hire employee</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <input
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          placeholder="Employee number"
          value={employeeNumber}
          onChange={(e) => setEmployeeNumber(e.target.value)}
          required
        />
        <input
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          placeholder="Legal name"
          value={legalName}
          onChange={(e) => setLegalName(e.target.value)}
          required
        />
        <input
          type="date"
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={hireDate}
          onChange={(e) => setHireDate(e.target.value)}
          required
        />
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={busy}
        className="mt-3 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {busy ? 'Hiring…' : 'Hire'}
      </button>
    </form>
  )
}

function EmployeeDetailPanel({
  employeeId,
  onChanged,
}: {
  employeeId: number
  onChanged: () => void
}) {
  const { hasPermission } = useAuth()
  const [detail, setDetail] = useState<hrApi.EmployeeDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const canCompensation = hasPermission('hr.compensation.write')
  const canWrite = hasPermission('hr.write')

  async function load() {
    try {
      const data = await hrApi.getEmployeeDetail(employeeId)
      setDetail(data)
    } catch {
      setError('Could not load employee.')
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [employeeId])

  async function handleTerminate() {
    setError(null)
    setBusy(true)
    try {
      await hrApi.changeEmploymentStatus(employeeId, {
        new_status: 'TERMINATED',
        effective_from: new Date().toISOString().slice(0, 10),
        reason: 'Terminated via UI',
      })
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not change status.')
    } finally {
      setBusy(false)
    }
  }

  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (!detail) return <p className="text-sm text-gray-500">Loading…</p>

  return (
    <div className="rounded border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">{detail.employee.legal_name}</h2>
          <p className="mt-1 text-xs text-gray-500">
            #{detail.employee.employee_number} · hired {detail.employee.hire_date}
          </p>
        </div>
        {detail.current_status && <StatusBadge status={detail.current_status.status} />}
      </div>

      {detail.current_assignment && (
        <p className="mt-3 text-sm text-gray-700">
          Store {detail.current_assignment.store_id}
          {detail.current_assignment.department_id != null &&
            ` · Department ${detail.current_assignment.department_id}`}
        </p>
      )}

      {canCompensation && <CompensationPanel employeeId={employeeId} />}

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}

      {canWrite && detail.current_status?.status !== 'TERMINATED' && (
        <button
          disabled={busy}
          onClick={handleTerminate}
          className="mt-4 rounded border border-red-300 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
        >
          Terminate
        </button>
      )}
    </div>
  )
}

function CompensationPanel({ employeeId }: { employeeId: number }) {
  const [rate, setRate] = useState('')
  const [payType, setPayType] = useState('HOURLY')
  const [history, setHistory] = useState<hrApi.CompensationPeriod[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      setHistory(await hrApi.getCompensationHistory(employeeId))
    } catch {
      /* compensation history is supplementary here */
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [employeeId])

  async function handleSetRate(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await hrApi.changeCompensation(employeeId, {
        effective_from: new Date().toISOString().slice(0, 10),
        pay_type: payType,
        rate,
        pay_frequency: 'BIWEEKLY',
      })
      setRate('')
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not set compensation.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-4 border-t border-gray-100 pt-3">
      <h3 className="text-xs font-semibold text-gray-700">Compensation</h3>
      <ul className="mt-1 space-y-0.5 text-xs text-gray-600">
        {history.map((c) => (
          <li key={c.id}>
            {c.pay_type} {c.rate} from {c.effective_from}
            {c.effective_to ? ` to ${c.effective_to}` : ' (current)'}
          </li>
        ))}
        {history.length === 0 && <li className="text-gray-400">No compensation on file.</li>}
      </ul>
      <form onSubmit={handleSetRate} className="mt-2 flex flex-wrap items-center gap-2">
        <select
          className="rounded border border-gray-300 px-2 py-1 text-xs"
          value={payType}
          onChange={(e) => setPayType(e.target.value)}
        >
          <option value="HOURLY">Hourly</option>
          <option value="SALARY">Salary</option>
        </select>
        <input
          className="w-24 rounded border border-gray-300 px-2 py-1 text-xs"
          placeholder="Rate"
          value={rate}
          onChange={(e) => setRate(e.target.value)}
          required
        />
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Set rate
        </button>
      </form>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  )
}

export function HrPage() {
  const { user, hasPermission } = useAuth()
  const storeId = user?.store_id ?? null
  const [employees, setEmployees] = useState<hrApi.Employee[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const canWrite = hasPermission('hr.write')

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setEmployees(await hrApi.listEmployees())
    } catch {
      setError('Could not load employees.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
  }, [])

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">HR / Workforce</h1>

      {canWrite && storeId != null && (
        <div className="mt-4">
          <HireEmployeeForm storeId={storeId} onHired={load} />
        </div>
      )}

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-1">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="py-2 pr-4">Employee</th>
                </tr>
              </thead>
              <tbody>
                {employees.map((emp) => (
                  <tr
                    key={emp.id}
                    onClick={() => setSelectedId(emp.id)}
                    className={`cursor-pointer border-b border-gray-100 hover:bg-gray-50 ${
                      selectedId === emp.id ? 'bg-blue-50' : ''
                    }`}
                  >
                    <td className="py-2 pr-4">
                      {emp.legal_name}{' '}
                      <span className="text-xs text-gray-400">#{emp.employee_number}</span>
                    </td>
                  </tr>
                ))}
                {employees.length === 0 && (
                  <tr>
                    <td className="py-6 text-center text-gray-400">No employees yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="lg:col-span-2">
            {selectedId ? (
              <EmployeeDetailPanel employeeId={selectedId} onChanged={load} />
            ) : (
              <p className="text-sm text-gray-500">Select an employee to view details.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
