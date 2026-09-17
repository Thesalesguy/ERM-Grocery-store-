import { apiFetch, setAccessToken } from './client'

export interface AccessTokenResponse {
  access_token: string
  token_type: string
  expires_in_seconds: number
}

export interface CurrentUser {
  id: number
  username: string
  full_name: string
  store_id: number | null
  permissions: string[]
}

export function login(username: string, password: string): Promise<AccessTokenResponse> {
  return apiFetch<AccessTokenResponse>('/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export function fetchCurrentUser(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>('/api/v1/auth/me')
}

let refreshPromise: Promise<AccessTokenResponse> | null = null

export function refresh(): Promise<AccessTokenResponse> {
    if (refreshPromise) {
        return refreshPromise
    }

    refreshPromise = (async () => {
        try {
            const response = await apiFetch<AccessTokenResponse>('/api/v1/auth/refresh', { method: 'POST' })

            if (response && response.access_token) {
                setAccessToken(response.access_token)
            }

            return response
        } finally {
            refreshPromise = null
        }
    })()

    return refreshPromise
}

export function logout(): Promise<void> {
  return apiFetch<void>('/api/v1/auth/logout', { method: 'POST' })
}
