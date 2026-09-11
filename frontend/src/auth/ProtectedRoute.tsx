import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

/** Redirects to /login when there's no signed-in user. Renders nothing
 * while the initial silent-refresh check is still in flight, to avoid a
 * flash of the login page on reload for an already-signed-in user. */
export function ProtectedRoute() {
  const { user, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) return null
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  return <Outlet />
}

/** Hides a route (and, by using it around a NavLink, a nav entry) behind
 * a specific permission — used for pages a Cashier shouldn't reach even
 * though they're authenticated (e.g. Products, Inventory). */
export function RequirePermission({
  permission,
  children,
}: {
  permission: string
  children: React.ReactNode
}) {
  const { hasPermission } = useAuth()
  if (!hasPermission(permission)) {
    return (
      <div className="max-w-md">
        <h1 className="text-xl font-semibold text-gray-900">Access denied</h1>
        <p className="mt-2 text-gray-600">
          Your account doesn't have permission to view this page.
        </p>
      </div>
    )
  }
  return <>{children}</>
}
