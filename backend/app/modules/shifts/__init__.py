"""Cashier/till/shift session management (M15).

See docs/M15_DESIGN.md for the full design. Owns `CashierShift` (a
cashier's bounded cash-handling session at one store, from opening float
to closing physical count) and `CashMovement` (paid-in/paid-out cash
movements recorded during an open shift). `Sale.shift_id`/
`SaleReturn.shift_id` (on the `sales` module's own models, not here —
avoiding a cross-module FK-owns-the-column inversion) attribute cash sales
and cash refunds to the shift that was open when they happened, so a
shift's expected physical cash can be derived from those two modules'
authoritative rows plus this module's own `CashMovement` rows, without
maintaining any separate running balance as a second source of truth.
"""
