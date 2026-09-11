import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import { setAccessToken } from '../api/client'
import { AuthProvider } from '../auth/AuthContext'
import { mockFetchRoutes } from '../test/mockFetch'
import { LoginPage } from './LoginPage'

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('LoginPage', () => {
  afterEach(() => {
    setAccessToken(null)
  })

  it('shows an error on incorrect credentials and keeps the form usable', async () => {
    mockFetchRoutes([
      { method: 'POST', path: '/auth/refresh', status: 401 },
      {
        method: 'POST',
        path: '/auth/login',
        status: 401,
        json: { error: { code: 'INVALID_CREDENTIALS', message: 'bad creds' } },
      },
    ])
    renderLogin()

    await waitFor(() => {
      expect(screen.getByLabelText(/username/i)).toBeInTheDocument()
    })

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'cashier' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'wrong-password' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/incorrect username or password/i)
    })
    // The form is still on screen and usable after a failed attempt.
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument()
  })

  it('disables the submit button until both fields are filled', async () => {
    mockFetchRoutes([{ method: 'POST', path: '/auth/refresh', status: 401 }])
    renderLogin()

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /sign in/i })).toBeDisabled()
    })

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'cashier' } })
    expect(screen.getByRole('button', { name: /sign in/i })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'some-password' } })
    expect(screen.getByRole('button', { name: /sign in/i })).not.toBeDisabled()
  })
})
