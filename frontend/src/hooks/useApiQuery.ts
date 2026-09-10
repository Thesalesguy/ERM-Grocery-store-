import { useEffect, useState } from 'react'
import { ApiError, apiFetch } from '../api/client'

export interface ApiQueryState<T> {
  data: T | null
  loading: boolean
  error: ApiError | Error | null
}

/**
 * Minimal loading/error/data foundation for GET requests. Business pages
 * will likely grow more specific data-fetching needs later; this is the
 * shared baseline so pages don't each hand-roll fetch/loading/error state.
 */
export function useApiQuery<T>(path: string): ApiQueryState<T> {
  const [state, setState] = useState<ApiQueryState<T>>({
    data: null,
    loading: true,
    error: null,
  })

  useEffect(() => {
    let cancelled = false
    // Intentional: resets to a loading state when `path` changes and a new
    // fetch starts, mirroring React's own documented data-fetching-effect
    // pattern (this is synchronizing state with the external fetch, not a
    // derivable render-time value).
    // oxlint-disable-next-line react/set-state-in-effect
    setState({ data: null, loading: true, error: null })

    apiFetch<T>(path)
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null })
      })
      .catch((error: ApiError | Error) => {
        if (!cancelled) setState({ data: null, loading: false, error })
      })

    return () => {
      cancelled = true
    }
  }, [path])

  return state
}
