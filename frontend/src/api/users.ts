/**
 * M16: Users/RBAC administration client (app/api/v1/endpoints/auth.py,
 * `/users`/`/roles` routes). Every mutation here is gated server-side by
 * `users.manage` and re-validated by the backend's own
 * privilege-escalation and self-action-refusal guards
 * (PRIVILEGE_ESCALATION_DENIED, CANNOT_CHANGE_OWN_ROLE,
 * CANNOT_DEACTIVATE_SELF) — this file never re-implements that
 * authorization, only surfaces the errors the server already returns.
 */
import { apiFetch, buildQuery } from './client'

export interface User {
  id: number
  username: string
  email: string
  full_name: string
  store_id: number | null
  is_active: boolean
  role: string | null
}

export interface UserCreateInput {
  username: string
  email: string
  password: string
  full_name: string
  store_id?: number | null
  role: string
}

export interface UserDeactivateResponse {
  id: number
  username: string
  is_active: boolean
}

export interface Role {
  name: string
  description: string
}

export function listUsers(store_id?: number): Promise<User[]> {
  return apiFetch<User[]>(`/api/v1/auth/users${buildQuery({ store_id })}`)
}

export function createUser(input: UserCreateInput): Promise<User> {
  return apiFetch<User>('/api/v1/auth/users', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function deactivateUser(userId: number): Promise<UserDeactivateResponse> {
  return apiFetch<UserDeactivateResponse>(`/api/v1/auth/users/${userId}/deactivate`, {
    method: 'POST',
  })
}

export function reactivateUser(userId: number): Promise<UserDeactivateResponse> {
  return apiFetch<UserDeactivateResponse>(`/api/v1/auth/users/${userId}/reactivate`, {
    method: 'POST',
  })
}

export function updateUserRole(userId: number, role: string): Promise<User> {
  return apiFetch<User>(`/api/v1/auth/users/${userId}/role`, {
    method: 'PUT',
    body: JSON.stringify({ role }),
  })
}

export function listRoles(): Promise<Role[]> {
  return apiFetch<Role[]>('/api/v1/auth/roles')
}
