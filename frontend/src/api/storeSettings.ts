/**
 * M16: Store settings client (app/api/v1/endpoints/stores.py). Exposes
 * exactly the two fields that existed only as Store columns with no API
 * surface before this milestone -- attendance_day_boundary_hour (M10)
 * and return_approval_threshold_amount (M14) -- plus basic identity
 * fields. `is_active` is deliberately read-only here (see
 * docs/M16_DESIGN.md "Settings screen"); there is no currency, tax, or
 * fiscal configuration because the backend has none.
 */
import { apiFetch } from './client'

export interface StoreSettings {
  id: number
  name: string
  address: string | null
  timezone: string
  is_active: boolean
  attendance_day_boundary_hour: number
  return_approval_threshold_amount: string | null
}

export interface StoreSettingsUpdateInput {
  name?: string
  address?: string | null
  timezone?: string
  attendance_day_boundary_hour?: number
  return_approval_threshold_amount?: string | null
  clear_return_approval_threshold?: boolean
}

export function getStoreSettings(storeId: number): Promise<StoreSettings> {
  return apiFetch<StoreSettings>(`/api/v1/stores/${storeId}/settings`)
}

export function updateStoreSettings(
  storeId: number,
  input: StoreSettingsUpdateInput,
): Promise<StoreSettings> {
  return apiFetch<StoreSettings>(`/api/v1/stores/${storeId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}
