import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)
    try {
      await login(username, password)
      const from = (location.state as { from?: Location })?.from?.pathname ?? '/'
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Incorrect username or password.')
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-xl font-semibold text-gray-900">Grocery ERP</h1>
        <p className="mt-1 text-sm text-gray-500">Sign in to continue</p>

        <label className="mt-6 block text-sm font-medium text-gray-700" htmlFor="username">
          Username
        </label>
        <input
          id="username"
          autoFocus
          className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          disabled={isSubmitting}
        />

        <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={isSubmitting}
        />

        {error && (
          <p role="alert" className="mt-4 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={isSubmitting || !username || !password}
          className="mt-6 w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSubmitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
