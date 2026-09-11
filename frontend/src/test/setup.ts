import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Explicit rather than relying on @testing-library/react's environment-based
// auto-cleanup detection — unmounts each test's render tree so two tests in
// the same file never see each other's DOM.
afterEach(() => {
  cleanup()
})
