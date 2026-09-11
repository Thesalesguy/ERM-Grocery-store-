import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import * as authApi from '../api/auth'
import { setAccessToken, setUnauthorizedHandler } from '../api/client'

interface AuthState {
  user: authApi.CurrentUser | null
  /** true while the initial silent-refresh-on-load attempt is in flight —
   * render nothing/a spinner during this, not the login page, so a
   * page reload doesn't flash the login screen for an already-signed-in
   * user. */
  isLoading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  hasPermission: (code: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<authApi.CurrentUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const clearSession = useCallback(() => {
    setAccessToken(null)
    setUser(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(clearSession)
    return () => setUnauthorizedHandler(null)
  }, [clearSession])

  useEffect(() => {
    // On mount, try to turn an existing httpOnly refresh cookie into a
    // fresh access token, so a page reload doesn't force a re-login.
    let cancelled = false
    authApi
      .refresh()
      .then(async () => {
        const currentUser = await authApi.fetchCurrentUser()
        if (!cancelled) setUser(currentUser)
      })
      .catch(() => {
        // No valid session — that's fine, the login page will show.
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const tokenResponse = await authApi.login(username, password)
    setAccessToken(tokenResponse.access_token)
    const currentUser = await authApi.fetchCurrentUser()
    setUser(currentUser)
  }, [])

  const logout = useCallback(async () => {
    try {
      await authApi.logout()
    } finally {
      clearSession()
    }
  }, [clearSession])

  const hasPermission = useCallback(
    (code: string) => user?.permissions.includes(code) ?? false,
    [user],
  )

  return (
    <AuthContext.Provider value={{ user, isLoading, login, logout, hasPermission }}>
      {children}
    </AuthContext.Provider>
  )
}

// Intentional: the standard React context pattern pairs a Provider
// component with its own `useX` hook in the same file, since the hook is
// meaningless without the Provider it reads from. This only costs Fast
// Refresh's state-preserving hot reload for this one file, not
// correctness.
// oxlint-disable-next-line react/only-export-components
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
