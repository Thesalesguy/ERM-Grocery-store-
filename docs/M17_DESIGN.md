# M17 Design — Financial Controls & Operational Integrity

Companion to `docs/M17_DISCOVERY.md`. Per that document's finding, only
two items are confirmed gaps warranting implementation; two more are
confirmed intentional/undesigned and are documented there as
limitations, not designed here (designing an implementation for
`docs/M17_DISCOVERY.md`'s §C/§E findings would be exactly the
speculative functionality M17's instructions forbid, since neither has
a confirmed requirement behind it).

## Item 1 — `list_cash_movements` service-layer store isolation

- **Business invariant**: a user whose account is scoped to store A must
  never be able to read another store's cash-movement ledger via any
  code path, including a direct service-layer call that bypasses the
  HTTP layer entirely — the same invariant `get_shift`/`list_shifts`
  already enforce in the same module (M16 Phase 0 item 5).
- **Authorized actors**: unchanged — gated by the existing `shift.read`
  permission at the endpoint layer; this change adds a second,
  independent enforcement point, not a new authorization rule.
- **Store-scope rule**: `caller_store_id is None` (an unrestricted
  account) sees any store's movements; otherwise
  `caller_store_id == shift.store_id` is required, or the call fails —
  identical rule and identical failure mode (`NotFoundError`, matching
  the endpoint's existing information-hiding 404 behavior) to
  `get_shift`.
- **Accounting / inventory / cash effect**: none — this is a read path;
  no financial state changes.
- **Audit requirements**: none — read-only, and `list_cash_movements`
  has never been an audited action (audit logging in this system covers
  mutations, not reads, throughout).
- **Idempotency key / uniqueness constraint**: not applicable (read
  path).
- **Concurrency behavior**: not applicable — a plain `SELECT`, no
  locking, no mutation.
- **Transaction boundary / rollback / failure behavior**: unchanged —
  the function already runs inside whatever transaction its caller
  manages; adding a `caller_store_id` check either lets execution
  continue or raises `NotFoundError` before any query, so there is
  nothing new to roll back.
- **API semantics**: the HTTP endpoint's behavior does not change at
  all (it already gets a 404 on cross-store access via its own
  `get_shift` pre-check) — this closes the gap only for a **direct
  service-layer caller**, which is the actual scenario M16/M17's
  "service-layer guard" standard is about.
- **Reporting semantics**: not applicable.
- **Migration requirements**: none — no schema change.
- **Test strategy**: a direct service-layer test (no HTTP client, no
  endpoint-level pre-check) proving a call scoped to store A raises when
  given store B's shift id — mirroring
  `test_shifts.py::test_direct_service_call_to_get_shift_and_list_shifts_enforces_store_isolation`
  exactly, extended to cover `list_cash_movements`.

## Item 2 — AP reconciliation-after-reversal regression coverage

- **Business invariant**: `ap_reconciliation`'s and
  `purchase_clearing_reconciliation`'s own contract — GL Accounts
  Payable / Purchase Clearing control-account balances equal the AP
  subledger total — must continue to hold after a supplier
  payment/credit-note reversal, not just after ordinary invoice/payment
  activity (which is all the existing `test_ap_reconciliation.py`
  covers).
- **Authorized actors / store-scope rule**: unchanged — this is a test
  addition, not a new capability. `ap_reconciliation` already accepts
  `store_id`/`store_ids` scoping, used as-is by the new test.
- **Accounting effect**: none new — `reverse_supplier_payment`/
  `reverse_supplier_credit_note` already exist and their accounting
  effect is fixed (M16); this item proves an existing, documented
  property of an existing function, it does not add one.
- **Inventory / cash effect**: not applicable.
- **Audit requirements**: not applicable (test-only).
- **Idempotency key / uniqueness constraint**: not applicable.
- **Concurrency behavior**: not applicable — the new test runs
  sequentially (post an invoice, pay it, reverse the payment, assert
  reconciliation), which is the correct shape for a reconciliation
  property test; concurrency safety for the reversal itself is already
  separately proven (`M17_DISCOVERY.md` §I/§J).
- **Transaction boundary / rollback / failure behavior**: not
  applicable.
- **API semantics / reporting semantics**: unchanged.
- **Migration requirements**: none.
- **Test strategy**: extend `test_ap_reconciliation.py` (the existing,
  established home for this exact class of test) with a new test that:
  creates a store/supplier/product, receives and posts an invoice,
  records a full payment, calls `ap_reconciliation` and
  `purchase_clearing_reconciliation` to capture the reconciled baseline
  (`discrepancy == 0`), reverses the payment, and re-calls both
  reconciliation functions — asserting `discrepancy == 0` still holds
  and that the subledger total moved back to the pre-payment amount.
  Repeated for a credit-note reversal, since credit notes touch
  `purchase_clearing_reconciliation` differently (Inventory vs Purchase
  Discounts account, per `reverse_supplier_credit_note`'s own
  docstring) than payments do.
- **Contingency**: if this test fails, that is a genuine, newly
  confirmed defect in `reverse_supplier_payment`/
  `reverse_supplier_credit_note` or in `_outstanding_balance` — not
  something to work around by loosening the test's assertion. The fix
  (if needed) would be scoped to whichever function's numbers the
  failure isolates to, following M16's own established reversal-pattern
  conventions (idempotent-by-existence, dedicated journal, no generic
  `reverse_journal_entry` involvement).

## Non-goals (restated from `M17_DISCOVERY.md`)

No shift-close correction/reopen workflow (§C — deferred by M15's own
prior design decision, no confirmed requirement to revisit it now). No
stock-adjustment reversal/correction linkage (§E — never designed,
not a confirmed requirement). No new roles, permissions, migrations, or
API endpoints of any kind — both items above are a narrow service-layer
guard and a regression test respectively, against code that already
exists and already works.
