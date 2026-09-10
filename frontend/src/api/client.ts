/**
 * Thin fetch wrapper all API calls should go through.
 *
 * Business modules will add typed request/response functions on top of this
 * (e.g. `getProductByBarcode`) as those modules are implemented — this file
 * only establishes the shared request/error handling foundation for M0.
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

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init.headers,
    },
  })

  if (!response.ok) {
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
    throw new ApiError(response.status, code, message)
  }

  return (await response.json()) as T
}
