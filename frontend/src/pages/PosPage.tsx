import { useEffect, useRef, useState } from 'react'

/**
 * POS placeholder. Cart, checkout, and payment are NOT implemented here yet
 * (Milestone M4) — this page exists to establish the scanner-input pattern
 * described in docs/TECHNICAL_BLUEPRINT.md Sections E.1/K.1:
 *
 * USB/Bluetooth barcode scanners behave as a keyboard emitting characters
 * followed by Enter. A hidden, always-focused input captures that stream
 * anywhere on the page, so the cashier never has to click into a field
 * before scanning. The same handler also accepts normal typed input for
 * manual SKU entry, so correctness never depends on telling a scan apart
 * from typing.
 */
export function PosPage() {
  const inputRef = useRef<HTMLInputElement>(null)
  const [lastScan, setLastScan] = useState<string | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'Enter') return
    const code = event.currentTarget.value.trim()
    if (code) {
      // Real barcode lookup (GET /products/barcode/{code}) arrives with
      // the Products module in M1 — for now we just surface what was
      // captured, to prove the input pipeline works end to end.
      setLastScan(code)
    }
    event.currentTarget.value = ''
  }

  return (
    <div className="max-w-2xl" onClick={() => inputRef.current?.focus()} role="presentation">
      <h1 className="text-2xl font-semibold text-gray-900">Point of Sale</h1>
      <p className="mt-2 text-gray-600">
        Cart, checkout, and payment are planned for M4. This screen currently only proves out
        barcode-scanner keyboard-input capture.
      </p>

      <input
        ref={inputRef}
        onKeyDown={handleKeyDown}
        onBlur={() => inputRef.current?.focus()}
        className="sr-only"
        aria-label="Barcode scanner input"
        autoFocus
      />

      <div className="mt-6 rounded border border-gray-200 bg-white p-4">
        <h2 className="text-sm font-medium text-gray-500">Last captured code</h2>
        <p className="mt-1 font-mono text-lg text-gray-900">{lastScan ?? '—'}</p>
        <p className="mt-2 text-xs text-gray-400">
          Scan a barcode or click here and type a code followed by Enter.
        </p>
      </div>
    </div>
  )
}
