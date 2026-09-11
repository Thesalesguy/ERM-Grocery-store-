/**
 * Thin fetch wrapper all API calls go through.
 *
 * Attaches the current access token (see setAccessToken) as a Bearer
 * header, and always sends credentials so the httpOnly refresh-token
 * cookie round-trips on /auth/refresh and /auth/logout. On a 401 from any
 * OTHER endpoint, it makes exactly one attempt to silently refresh the
 * access token and retry the original request — so a merely-expired
 * access token (15 minute lifetime) doesn't interrupt an in-progress POS
 * sale, but a truly invalid session still surfaces as a real 401 for
 * AuthContext to act on (redirect to /login).
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export class ApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/** Builds a `?key=value&...` query string, skipping undefined values.
 * Accepts any params object (interfaces don't have index signatures, so
 * this takes `object` rather than `Record<string, ...>` and casts once
 * internally). */
export function buildQuery(params: object): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}

let accessToken: string | null = null
let onUnauthorized: (() => void) | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

/** Called once (by AuthContext) so the client can react when a refresh
 * attempt fails outright — i.e. the session is truly gone. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

async function refreshAccessToken(): Promise<string | null> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
    if (!response.ok) return null
    const body = (await response.json()) as { access_token: string }
    setAccessToken(body.access_token)
    return body.access_token
  } catch {
    return null
  }
}

async function parseError(response: Response, path: string): Promise<ApiError> {
  let code = 'UNKNOWN_ERROR'
  let message = `Request to ${path} failed with status ${response.status}`
  try {
    const body = await response.json()
    code = body?.error?.code ?? code
    message = body?.error?.message ?? message
  } catch {
    // Response body wasn't JSON (e.g. a proxy error page) — fall back to
    // the generic message above rather than throwing a parse error.
  }
  return new ApiError(response.status, code, message)
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isAuthEndpoint = path.startsWith('/api/v1/auth/')

  const doFetch = (token: string | null) =>
    fetch(`${API_BASE_URL}${path}`, {
      ...init,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    })

  let response = await doFetch(accessToken)

  if (response.status === 401 && !isAuthEndpoint) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      response = await doFetch(refreshed)
    } else {
      setAccessToken(null)
      onUnauthorized?.()
    }
  }

  if (!response.ok) {
    throw await parseError(response, path)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
