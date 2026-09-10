import { useApiQuery } from '../hooks/useApiQuery'

interface HealthResponse {
  status: string
}

/**
 * Placeholder home page. It doubles as a live demonstration of the API
 * client + loading/error foundation by calling the backend's /health
 * endpoint — real dashboard widgets (sales today, low stock, etc.) arrive
 * with the modules that produce that data (M4/M6).
 */
export function DashboardPage() {
  const { data, loading, error } = useApiQuery<HealthResponse>('/health')

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold text-gray-900">Dashboard</h1>
      <p className="mt-2 text-gray-600">
        Sales, inventory, and P&amp;L summary widgets will appear here in later milestones.
      </p>

      <div className="mt-6 rounded border border-gray-200 bg-white p-4">
        <h2 className="text-sm font-medium text-gray-500">Backend connectivity</h2>
        {loading && <p className="mt-1 text-gray-500">Checking API…</p>}
        {error && <p className="mt-1 text-red-600">Could not reach the API: {error.message}</p>}
        {data && <p className="mt-1 text-green-700">API status: {data.status}</p>}
      </div>
    </div>
  )
}
