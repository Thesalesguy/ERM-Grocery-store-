import { useEffect, useState } from 'react'
import * as usersApi from '../api/users'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

/**
 * M16: Users/RBAC administration. `users.manage` gates the whole route
 * (see App.tsx). Every mutation here is enforced server-side — this page
 * only hides controls the acting user cannot use (self role-change,
 * self-deactivation) as a UX convenience. It never assumes that hiding a
 * control is what makes an action safe: the backend's own
 * PRIVILEGE_ESCALATION_DENIED / CANNOT_CHANGE_OWN_ROLE /
 * CANNOT_DEACTIVATE_SELF guards are the actual authorization boundary,
 * exercised directly (bypassing this UI) in
 * backend/tests/test_users_admin.py.
 */

const emptyDraft = {
  username: '',
  email: '',
  password: '',
  full_name: '',
  store_id: '',
  role: '',
}

function StatusBadge({ isActive }: { isActive: boolean }) {
  return isActive ? (
    <span className="rounded bg-green-100 px-2 py-0.5 text-xs text-green-800">Active</span>
  ) : (
    <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-700">Deactivated</span>
  )
}

function CreateUserForm({
  roles,
  onCreated,
}: {
  roles: usersApi.Role[]
  onCreated: (user: usersApi.User) => void
}) {
  const [draft, setDraft] = useState(emptyDraft)
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  function update(field: keyof typeof emptyDraft, value: string) {
    setDraft((prev) => ({ ...prev, [field]: value }))
  }

  async function handleSubmit() {
    if (!draft.username.trim() || !draft.email.trim() || !draft.password || !draft.role) return
    setError(null)
    setIsSubmitting(true)
    try {
      const user = await usersApi.createUser({
        username: draft.username.trim(),
        email: draft.email.trim(),
        password: draft.password,
        full_name: draft.full_name.trim() || draft.username.trim(),
        store_id: draft.store_id.trim() ? Number(draft.store_id) : null,
        role: draft.role,
      })
      setDraft(emptyDraft)
      onCreated(user)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create the user.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="mt-4 rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Create user</h2>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <input
          value={draft.username}
          onChange={(e) => update('username', e.target.value)}
          placeholder="Username"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <input
          value={draft.email}
          onChange={(e) => update('email', e.target.value)}
          placeholder="Email"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <input
          value={draft.full_name}
          onChange={(e) => update('full_name', e.target.value)}
          placeholder="Full name"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <input
          type="password"
          value={draft.password}
          onChange={(e) => update('password', e.target.value)}
          placeholder="Temporary password (min 8 chars)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <input
          type="number"
          value={draft.store_id}
          onChange={(e) => update('store_id', e.target.value)}
          placeholder="Store ID (blank = company-wide)"
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        />
        <select
          value={draft.role}
          onChange={(e) => update('role', e.target.value)}
          className="rounded border border-gray-300 px-3 py-2 text-sm"
        >
          <option value="">Select role…</option>
          {roles.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name}
            </option>
          ))}
        </select>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-600">
          {error}
        </p>
      )}
      <button
        onClick={handleSubmit}
        disabled={
          isSubmitting ||
          !draft.username.trim() ||
          !draft.email.trim() ||
          !draft.password ||
          !draft.role
        }
        className="mt-3 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {isSubmitting ? 'Creating…' : 'Create user'}
      </button>
    </div>
  )
}

function UserRow({
  user,
  roles,
  isSelf,
  onChanged,
}: {
  user: usersApi.User
  roles: usersApi.Role[]
  isSelf: boolean
  onChanged: (user: usersApi.User) => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleRoleChange(role: string) {
    if (!role || role === user.role) return
    setError(null)
    setIsSubmitting(true)
    try {
      const updated = await usersApi.updateUserRole(user.id, role)
      onChanged(updated)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to change role.')
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handleToggleActive() {
    setError(null)
    setIsSubmitting(true)
    try {
      const result = user.is_active
        ? await usersApi.deactivateUser(user.id)
        : await usersApi.reactivateUser(user.id)
      onChanged({ ...user, is_active: result.is_active })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to update the account.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <tr className="border-b border-gray-100 align-top">
      <td className="py-2 pr-4">
        {user.username}
        {isSelf && <span className="ml-2 text-xs text-gray-400">(you)</span>}
        <div className="text-xs text-gray-400">{user.email}</div>
      </td>
      <td className="py-2 pr-4">{user.full_name}</td>
      <td className="py-2 pr-4">{user.store_id !== null ? `#${user.store_id}` : 'Company-wide'}</td>
      <td className="py-2 pr-4">
        <select
          value={user.role ?? ''}
          disabled={isSelf || isSubmitting}
          onChange={(e) => handleRoleChange(e.target.value)}
          className="rounded border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100"
        >
          {user.role === null && <option value="">No role</option>}
          {roles.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name}
            </option>
          ))}
        </select>
      </td>
      <td className="py-2 pr-4">
        <StatusBadge isActive={user.is_active} />
      </td>
      <td className="py-2 pr-4">
        <button
          onClick={handleToggleActive}
          disabled={isSelf || isSubmitting}
          className="rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {user.is_active ? 'Deactivate' : 'Reactivate'}
        </button>
        {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
      </td>
    </tr>
  )
}

export function UsersPage() {
  const { user: currentUser } = useAuth()
  const [users, setUsers] = useState<usersApi.User[]>([])
  const [roles, setRoles] = useState<usersApi.Role[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([usersApi.listUsers(), usersApi.listRoles()])
      .then(([userList, roleList]) => {
        setUsers(userList)
        setRoles(roleList)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load users.'))
      .finally(() => setLoading(false))
  }, [])

  function upsertUser(updated: usersApi.User) {
    setUsers((prev) => {
      const exists = prev.some((u) => u.id === updated.id)
      return exists ? prev.map((u) => (u.id === updated.id ? updated : u)) : [...prev, updated]
    })
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-900">Users</h1>
      <p className="mt-1 text-sm text-gray-500">User accounts and role assignment (RBAC).</p>

      {loading && <p className="mt-4 text-gray-500">Loading…</p>}
      {error && <p className="mt-4 text-red-600">{error}</p>}

      {!loading && !error && (
        <>
          <table className="mt-4 w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-gray-500">
                <th className="py-2 pr-4">User</th>
                <th className="py-2 pr-4">Full name</th>
                <th className="py-2 pr-4">Store</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2 pr-4">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <UserRow
                  key={u.id}
                  user={u}
                  roles={roles}
                  isSelf={u.id === currentUser?.id}
                  onChanged={upsertUser}
                />
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-gray-400">
                    No users found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <CreateUserForm roles={roles} onCreated={upsertUser} />
        </>
      )}
    </div>
  )
}
