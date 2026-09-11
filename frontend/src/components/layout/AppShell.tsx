import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'
import { NAV_ITEMS } from './navigation'

export function AppShell() {
  const { user, hasPermission, logout } = useAuth()
  const visibleItems = NAV_ITEMS.filter(
    (item) => !item.permission || hasPermission(item.permission),
  )

  return (
    <div className="flex min-h-screen bg-gray-50 text-gray-900">
      <aside className="w-56 shrink-0 border-r border-gray-200 bg-white">
        <div className="border-b border-gray-200 px-4 py-4">
          <span className="text-lg font-semibold">Grocery ERP</span>
        </div>
        <nav className="flex flex-col gap-1 p-2">
          {visibleItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === '/'}
              className={({ isActive }) =>
                `rounded px-3 py-2 text-sm font-medium transition-colors ${
                  isActive ? 'bg-blue-600 text-white' : 'text-gray-700 hover:bg-gray-100'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-3">
          <span className="text-sm text-gray-500">Environment: {import.meta.env.MODE}</span>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-gray-700">{user?.full_name}</span>
            <button onClick={() => logout()} className="text-blue-600 hover:underline">
              Sign out
            </button>
          </div>
        </header>

        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
