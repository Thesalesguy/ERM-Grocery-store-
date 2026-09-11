import { vi } from 'vitest'

export interface MockRoute {
  method?: string
  /** Substring match against the request URL. */
  path: string
  status?: number
  json?: unknown
}

/**
 * Minimal URL/method-pattern fetch mock for component tests. Routes are
 * checked in order; the first match wins. Throws (loudly, in the test
 * console) if a request has no matching route, so an unmocked call fails
 * fast instead of hanging.
 */
export function mockFetchRoutes(routes: MockRoute[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const route = routes.find(
        (r) => url.includes(r.path) && (r.method ?? 'GET').toUpperCase() === method,
      )
      if (!route) {
        throw new Error(`mockFetchRoutes: no route for ${method} ${url}`)
      }
      const status = route.status ?? 200
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => route.json ?? {},
      } as Response
    }),
  )
}
