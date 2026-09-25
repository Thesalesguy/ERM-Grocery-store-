import { useEffect, useState } from 'react'
import * as storeSettingsApi from '../api/storeSettings'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

/**
 * M16: Store settings. `store.settings.read`/`store.settings.write`
 * gate the whole capability server-side (App.tsx wraps this route with
 * RequirePermission for read; the write control below is also hidden
 * without store.settings.write, though the PUT would 403 regardless).
 * Exposes exactly the fields the backend actually supports --
 * attendance_day_boundary_hour and return_approval_threshold_amount --
 * nothing invented (no currency, tax, fiscal, or hardware config exists
 * to expose). See docs/M16_DESIGN.md "Settings screen".
 */

export function SettingsPage() {
  const { user, hasPermission } = useAuth()
  const canWrite = hasPermission('store.settings.write')

  const [storeId, setStoreId] = useState<number | null>(user?.store_id ?? null)
  const [storeIdInput, setStoreIdInput] = useState('')
  const [settings, setSettings] = useState<storeSettingsApi.StoreSettings | null>(null)
  const [attendanceHour, setAttendanceHour] = useState('0')
  const [threshold, setThreshold] = useState('')
  const [clearThreshold, setClearThreshold] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  function load(id: number) {
    setLoading(true)
    setError(null)
    storeSettingsApi
      .getStoreSettings(id)
      .then((s) => {
        setSettings(s)
        setAttendanceHour(String(s.attendance_day_boundary_hour))
        setThreshold(s.return_approval_threshold_amount ?? '')
        setClearThreshold(false)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load settings.'))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (storeId !== null) load(storeId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeId])

  async function handleSave() {
    if (storeId === null) return
    setIsSaving(true)
    setError(null)
    setSavedAt(null)
    try {
      const updated = await storeSettingsApi.updateStoreSettings(storeId, {
        attendance_day_boundary_hour: Number(attendanceHour),
        return_approval_threshold_amount: clearThreshold
          ? undefined
          : threshold.trim() || undefined,
        clear_return_approval_threshold: clearThreshold,
      })
      setSettings(updated)
      setThreshold(updated.return_approval_threshold_amount ?? '')
      setClearThreshold(false)
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save settings.')
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Settings</h1>
      <p className="mt-1 text-sm text-gray-500">Store configuration.</p>

      {user?.store_id === null && (
        <div className="mt-4 flex items-center gap-2">
          <label className="text-xs text-gray-500">
            Store ID
            <input
              type="number"
              value={storeIdInput}
              onChange={(e) => setStoreIdInput(e.target.value)}
              className="ml-2 w-28 rounded border border-gray-300 px-2 py-1 text-sm"
              placeholder="e.g. 1"
            />
          </label>
          <button
            onClick={() => storeIdInput.trim() && setStoreId(Number(storeIdInput))}
            disabled={!storeIdInput.trim()}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Load
          </button>
        </div>
      )}

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && (
        <p role="alert" className="mt-4 text-red-600">
          {error}
        </p>
      )}

      {!loading && settings && (
        <div className="mt-4 max-w-md rounded border border-gray-200 bg-white p-4">
          <div className="text-sm text-gray-500">
            <span className="font-medium text-gray-900">{settings.name}</span>
            {settings.address && <> · {settings.address}</>} · {settings.timezone} ·{' '}
            {settings.is_active ? 'Active' : 'Inactive'}
          </div>

          <label className="mt-4 block text-xs text-gray-500">
            Attendance day boundary hour (0–23)
            <input
              type="number"
              min={0}
              max={23}
              value={attendanceHour}
              onChange={(e) => setAttendanceHour(e.target.value)}
              disabled={!canWrite}
              className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm disabled:bg-gray-100"
            />
          </label>
          <p className="mt-1 text-xs text-gray-400">
            The hour of day at which a new attendance/business day begins, for shifts that cross
            midnight.
          </p>

          <label className="mt-4 block text-xs text-gray-500">
            Return approval threshold amount
            <input
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              disabled={!canWrite || clearThreshold}
              placeholder="No threshold set"
              className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm disabled:bg-gray-100"
            />
          </label>
          <label className="mt-2 flex items-center gap-2 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={clearThreshold}
              onChange={(e) => setClearThreshold(e.target.checked)}
              disabled={!canWrite}
            />
            Clear threshold (returns above any amount require approval)
          </label>

          {canWrite && (
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="mt-4 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isSaving ? 'Saving…' : 'Save'}
            </button>
          )}
          {savedAt !== null && <span className="ml-3 text-xs text-green-700">Saved.</span>}
        </div>
      )}
    </div>
  )
}
