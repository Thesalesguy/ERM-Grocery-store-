# M16 Testing Sessions

Companion to `docs/M16_DESIGN.md`. Sessions A–K as specified for this
milestone. Every session below was actually run against this repository
during M16 development — nothing here is a plan for testing that was
never executed.

## Session A — Baseline

Backend suite before any M16 code: 894 tests passing (M15 baseline,
independently re-confirmed at the start of this milestone before Phase 0
work began). Frontend suite before any M16 code: 39 tests passing
(pre-existing pages only; `ReportsPage`/`UsersPage`/`SettingsPage` were
`PlaceholderPage` stubs with no page-specific tests).

## Session B — Hardening tests (Phase 0)

Each Phase 0 item was first re-traced against current code (see
`M16_DESIGN.md` §2's validated-findings table) before any change, per
the explicit instruction not to implement a "confirmed defect" without
re-verifying it. One item (`CASH_SHIFT_VARIANCE` reversal protection)
turned out to already be correctly implemented — a regression test was
added instead of a fix:
`test_shifts.py::test_cash_shift_variance_cannot_be_reversed_via_generic_journal_reversal`.

Focused regression tests added, one per fixed defect, run individually
before the combined suite:

- `test_shifts.py::test_cash_movement_audit_event_identifies_the_exact_movement_row`
- `test_shifts.py::test_direct_service_call_to_get_shift_and_list_shifts_enforces_store_isolation`
- `test_shifts.py::test_expected_cash_still_reflects_original_cash_tender_when_refund_is_noncash`
  (proves the expected-cash investigation's conclusion — see
  `M16_DESIGN.md` §4 — that the discovery audit's proposed fix was
  actually wrong: the existing behavior is correct and this test guards
  it)
- `test_inventory_idempotency.py::test_cross_store_direct_service_call_cannot_mutate_another_stores_stock`
- `test_reports_sales.py::test_backdated_return_lands_in_return_date_period_matching_gl`

All five passed on first run against the fixed code and were re-run
against the pre-fix code (temporarily reverted) to confirm each is a
real regression test, not a vacuous one — see Session J for the same
technique applied more broadly.

## Session C — AP reversal tests

`backend/tests/test_ap_reversal.py`, 14 tests, covering the scenarios
required by the task brief:

1. `test_reverse_payment_creates_reversal_and_restores_invoice_balance`
2. `test_reverse_payment_journal_entry_swaps_debit_and_credit`
3. `test_reverse_credit_note_restores_invoice_balance`
4. `test_reverse_credit_note_journal_entry_swaps_debit_and_credit`
5. `test_duplicate_reversal_is_idempotent` (payment)
6. `test_credit_note_reversal_is_idempotent`
7. `test_reversal_requires_a_reason`
8. `test_reversal_across_stores_is_denied`
9. `test_reversed_payment_appears_in_supplier_statement`
10. `test_reversed_credit_note_appears_in_supplier_statement`
11. `test_reversal_requires_ap_reverse_permission`
12. `test_reversal_of_nonexistent_payment_returns_404`
13. Genuine multi-connection concurrency test (two real `SessionLocal()`
    sessions + `threading.Barrier`, not the `db` fixture's savepoint
    isolation): concurrent reversal attempts on the same payment
    serialize correctly and only one reversal row is ever created.
14. Migration/constraint-level test confirming the DB's own
    `UNIQUE(supplier_payment_id)` /
    `UNIQUE(supplier_credit_note_id)` constraints are the race-safety
    backstop behind the idempotent-by-existence application check.

## Session D — Reporting correctness

Extended rather than duplicated the existing M11 reporting test fixture
dataset (`test_reports_sales.py`). The one new fixture-driven test
(`test_backdated_return_lands_in_return_date_period_matching_gl`)
specifically constructs a return whose physical `return_date` falls in a
different calendar month than its `created_at` timestamp, then asserts
both `reports_service._return_business_date()`-derived reports AND the
GL journal entry for that return land in the *same* reporting period —
proving the fix actually closes the operational-vs-GL divergence
described in `M16_DESIGN.md` §5, not merely that the new column exists.

## Session E — Reports UI

`frontend/src/pages/ReportsPage.test.tsx`, 3 tests:

1. Renders the operational Sales summary and the GL-derived P&L summary
   from two independently-supplied API fixtures with two different
   `gross_profit` values (385.00 vs 365.00), and asserts both are shown
   with distinct labels ("Gross profit (operational)" vs "Gross profit
   (GL-derived)") and that the operational figure is not left on screen
   once the Financial tab is showing the GL figure — the concrete check
   against "never present two independently calculated gross_profit
   values as though they are necessarily identical."
2. A user with only `reports.read` sees exactly the Sales and Dashboard
   tabs; Financial/Inventory/Purchasing/Payroll (each gated by a
   different permission) are absent.
3. A report API error (`403 STORE_ACCESS_DENIED`) is surfaced as visible
   text, not swallowed.

Manual browser smoke test: not performed this session (no running
frontend dev server was exercised against a live backend for Reports
specifically) — covered instead by the full `npm run build` production
build succeeding and the mocked-fetch component tests above, consistent
with this repository's session-log note elsewhere that UI verification
without a live browser session is stated as such rather than claimed as
full manual QA.

## Session F — Users/RBAC UI

`frontend/src/pages/UsersPage.test.tsx`, 4 tests:

1. Lists users; the acting user's own row has its role `<select>` and
   Deactivate button disabled (self-action UX guard), and is labeled
   "(you)".
2. Creates a user through the real request shape (`username`, `email`,
   `password`, `full_name`, `store_id`, `role`) and shows it in the list
   on success.
3. **The most important test in this session**: attempts a role change
   to `Admin` for another user and asserts the UI surfaces the backend's
   `PRIVILEGE_ESCALATION_DENIED` refusal text verbatim — proving the
   frontend does not silently retry, mask, or "correct" a denied
   privilege escalation, and that the actual authorization boundary is
   the backend response, not a client-side role list. The backend side
   of this same boundary is exercised directly, bypassing any UI, by
   `backend/tests/test_users_admin.py::test_service_layer_direct_call_cannot_assign_a_role_broader_than_the_actor_holds`
   — manipulating the request cannot grant privileges the actor does not
   hold, at the layer where it would actually matter if this UI (or any
   UI) had a bug.
4. Deactivates then reactivates a user, verifying the row's status badge
   and button label flip each direction.

## Session G — Settings UI

`frontend/src/pages/SettingsPage.test.tsx`, 3 tests:

1. A store-scoped Manager (`store.settings.write`) loads their own store
   automatically (no store-id prompt), edits
   `attendance_day_boundary_hour`, saves, and the PUT body is asserted
   to contain exactly that field.
2. An Auditor (`store.settings.read` only) sees the same data but the
   Save button is entirely absent and the input is disabled — the
   read/write permission split is enforced in the UI as well as the
   backend (which independently returns 403 on PUT, per
   `test_store_settings.py::test_cashier_cannot_write_settings_but_auditor_can_read`).
3. A company-wide user (`store_id: null`) is prompted for a store id
   before any settings load, and loading store 9 shows store 9's data —
   proving the UI never assumes a single implicit store for an
   unrestricted account.

## Session H — Adversarial security review

Conducted as part of Sessions B/C/F rather than as a separate
after-the-fact pass, per the actual attack surface each new capability
introduces:

- **Cross-store data leakage**: AP reversal (`test_reversal_across_stores_is_denied`),
  Users list/create (`test_store_scoped_admin_cannot_list_or_create_users_in_another_store`),
  Settings (`test_cross_store_manager_cannot_view_or_change_another_stores_settings`
  — read returns 404/information-hiding, write returns 403, matching
  this repo's established 404-vs-403 convention).
- **Privilege escalation**: `test_service_layer_direct_call_cannot_assign_a_role_broader_than_the_actor_holds`
  (service layer, bypassing the fact that `users.manage` is Admin-only
  today — defense-in-depth for if that ever changes) and the frontend
  test in Session F above.
- **Self-action lockout prevention**: `test_role_update_refuses_self_change`
  (`CANNOT_CHANGE_OWN_ROLE`), mirroring the pre-existing
  `CANNOT_DEACTIVATE_SELF` guard.
- **Unauthenticated/under-permissioned reversal attempts**:
  `test_reversal_requires_ap_reverse_permission`.
- **Tampered settings payload**: `test_is_active_is_not_editable_via_settings`
  — a client that includes `is_active` in the PUT body has it silently
  ignored (the schema has no such field at all, not merely an unused
  one), and `test_negative_threshold_rejected` /
  `test_out_of_range_attendance_hour_rejected` for out-of-domain values.
- **Audit trail integrity**: `test_audit_log_records_settings_change`
  confirms the audit log captures the actual new value, not a
  before-the-fact intent.

## Session I — Failure injection

- Duplicate-reversal-under-concurrency (Session C, item 13): two
  simultaneous reversal attempts on the same payment via genuine
  separate DB connections — exactly one reversal is created, the other
  observes it via the idempotent-by-existence check under row-level
  locking, never a race that creates two.
- Constraint-level backstop (Session C, item 14): even if the
  application-level idempotency check were ever bypassed, the DB's own
  `UNIQUE` constraint refuses a second reversal row outright — proven,
  not asserted, by attempting to insert a duplicate directly.
- CHECK-constraint failure injection (Session J, mutation test 3 below):
  a broken idempotency guard was shown to fail loudly with a real
  PostgreSQL `CheckViolation` on `purchase_invoices.amount_paid`, not a
  silently wrong balance — the database itself is a second line of
  defense, not merely inert storage.

## Session J — Mutation testing

Continuing this project's established manual/live methodology (break an
invariant in the running code, confirm the relevant test suite goes red
for the *right* reason, then revert and confirm green again) rather than
an automated mutation-testing tool. Three mutations run this session,
directly against the highest-risk new guards:

1. **Privilege-escalation guard** (`auth/service.py::_validate_role_within_actor_permissions`):
   replaced the `issubset` check with `if False:` (never denies).
   Result: `test_service_layer_direct_call_cannot_assign_a_role_broader_than_the_actor_holds`
   failed with a foreign-key violation instead of the expected
   `ForbiddenError` — confirming the test actually exercises the guard,
   not just the error-handling path. Reverted; 8/8
   `test_users_admin.py` tests green again.
2. **Store-settings cross-store isolation** (`auth/service.py::update_store_settings`):
   removed the `caller_store_id != store_id` check entirely. Result:
   `test_cross_store_manager_cannot_view_or_change_another_stores_settings`
   failed (write returned 200 instead of 403). Reverted; 8/8
   `test_store_settings.py` tests green again.
3. **AP reversal idempotency** (`ap/service.py::reverse_supplier_payment`):
   replaced `if existing_reversal is not None: return payment` with
   `if False: return payment` (always re-applies the reversal). Result:
   `test_duplicate_reversal_is_idempotent` failed with a real PostgreSQL
   `CheckViolation` on `ck_purchase_invoices_amount_paid_non_negative`
   (the second reversal attempt double-decremented `amount_paid` into
   negative territory) — the test caught it, and so did the database's
   own constraint. Reverted; 14/14 `test_ap_reversal.py` tests green
   again.

All three mutations were caught by the test suite and none required a
new test to be written to catch them — evidence the tests written in
Sessions B/C/F are not merely exercising the happy path.

A fourth exercise, not a mutation but a real tooling gap found the same
way: `npx tsc --noEmit` (used throughout early frontend development this
session) silently checks **zero files**, because the frontend's root
`tsconfig.json` is a project-references-only file with `"files": []` —
the actual type errors only surface under `npx tsc -b` (the command
`ci.yml`'s frontend job actually runs). Re-running `npx tsc -b` after
all M16 frontend pages were written caught one real error (a `.filter()`
chained directly onto an explicitly-typed array literal loses the
literal-type narrowing TypeScript would otherwise apply — see
`M16_HARDENING_AUDIT.md`), which was fixed and re-verified with the
correct command. Every frontend typecheck claim from this point in the
milestone onward used `npx tsc -b`, not `--noEmit`.

## Session K — Full regression

No prior test was weakened, deleted, skipped, or rewritten to
accommodate M16.

- Backend: `pytest -q` → **930 passed** (894 M15 baseline + 36 M16
  additions: 14 AP reversal + 8 Users admin + 8 Store settings + 6
  hardening regression tests, listed in Session B, plus a
  migration-assertion update in `test_migrations.py` that does not add
  a new test function). Re-run a second time after the
  Session J mutation round-trips, to prove the revert left the tree
  byte-identical in behavior: still 930 passed.
- Backend gates: `ruff check .`, `black --check .`, `mypy app` — all
  clean.
- Frontend: `npx vitest run` → **49 passed** (39 M15 baseline + 10 new:
  3 Reports + 4 Users + 3 Settings).
- Frontend gates: `npx tsc -b` (build mode — see Session J), `npm run
  lint` (oxlint), `npm run format:check` (prettier), `npm run build` —
  all clean after the Session J fix.
