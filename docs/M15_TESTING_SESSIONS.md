# M15 Testing Sessions

## Session A: baseline (before any code changes)

- Backend: `pytest --collect-only -q` → 849 tests collected.
- Frontend: `npx vitest run` → 37/37 passed, 11 test files.
- Migration head: `e0d2359bb08a`.
- `git status`: clean, HEAD `73cc0cf`.

## Session B: pre-M15 hardening (stock adjustment idempotency)

Added `client_transaction_id` to `StockAdjustment` (migration `1a4bae98d246`), threaded through `create_stock_adjustment`/schema/endpoint. New file `tests/test_inventory_idempotency.py` (5 tests): exact-duplicate-returns-same-adjustment, duplicate-key-does-not-double-post-movement-or-journal-entry, omitted-key-preserves-pre-M15-behavior, service-layer direct retry, and a genuine two-thread concurrent-duplicate race against real PostgreSQL. All 5 passed on first correct implementation. Full inventory suite (`test_inventory.py`, `test_inventory_api.py`, `test_stock_counts*.py`, `test_m8_failure_injection.py`) re-run: 43/43 passed. Full backend suite: 854/854 passed (849 baseline + 5 new). ruff/black/mypy clean.

## Session C: data model + migration

`CashierShift`/`CashMovement` models (new `shifts` module), `Sale.shift_id`/`SaleReturn.shift_id`, three new permissions, `Cash Over/Short` GL account, widened `ck_journal_entries_source_type`. Migration `db482a11ee31`.

**Real defect found and fixed**: the first migration draft defined `cashier_shifts.updated_at`/`cash_movements.updated_at` as `nullable=False, server_default=now()`. `TimestampMixin.updated_at` is `Mapped[datetime | None]` with `onupdate=func.now()` only — every other table using this mixin leaves it NULL on INSERT (only `onupdate` sets it). The mismatch produced a live `NotNullViolation` the moment the first service-layer test tried to create a row. Caught immediately by `tests/test_shifts.py`'s first real run (23 of 26 tests failed with the same `IntegrityError`) — not a design-time review catch, a real run-then-fix. Fixed by matching every prior migration's exact `updated_at` column definition (`nullable=True`, no server default); required a clean `alembic downgrade -1` + fix + `upgrade head` cycle since no real data existed yet in the sandbox to block it.

**Second real gap found (RBAC, not a bug)**: the first permission grant draft gave Cashier only `shift.manage`, not `shift.read` — meaning a cashier could open/close their own till but could not view their own shift's cash-movement history or detail (`GET /shifts/{id}/cash-movements` returned 403 `FORBIDDEN: Missing required permission: shift.read`, not the expected data). Caught by `test_cash_movement_paid_in_and_paid_out` and `test_cross_store_cashier_cannot_view_another_stores_shift`, both of which exercise a Cashier reading their own shift's detail. Fixed by granting `shift.read` to Cashier alongside `shift.manage`, mirroring the existing `pos.use`+`sales.read` pairing (documented in `M15_DESIGN.md` §8) — required a second downgrade/upgrade cycle to reseed the corrected role grant.

Migration verified directly against the live schema (`information_schema.columns`, `accounts` row, `permissions` rows) after both fixes — matched the model exactly.

## Session D: functional behavior + financial invariants + security (Session H folded in)

`tests/test_shifts.py`, 27 tests covering: shift open/duplicate-key idempotency/negative-float rejection/one-active-shift-per-cashier, active-shift lookup, cash-sale-attribution (with and without an open shift), cash movements (paid-in/paid-out, rejected on a closed shift, non-positive amount rejected), close (already-closed conflict, idempotent retry), exact/positive/negative variance with GL posting proof, the `$100 sale tendered $150` worked example, non-cash-tender exclusion, cash-refund inclusion, audit-log content, and the full security/adversarial matrix (unauthorized role, cross-cashier close without override, manager override succeeds, cross-store view/close denied at 404/403, cash-movement cross-cashier authorization, and a direct service-layer bypass attempt).

**Real bugs found while writing these** (both fixed, see Session C above): the `updated_at` NOT NULL bug and the missing `shift.read` grant for Cashier. Also fixed two test-authoring bugs of my own: `make_product`'s `current_qty_on_hand` defaults to 0, so several sale-attribution tests needed an explicit stock quantity; and the error-response shape is `{"error": {"code": ..., "message": ...}}` (confirmed by grepping the existing `test_return_void_approval.py`'s own assertions), not a top-level `error_code` key — every assertion in this file uses `.json()["error"]["code"]`.

**Final status**: 27/27 passing.

## Session E: financial invariants (folded into Session D above)

Covered by the same file — see `test_close_shift_exact_reconciliation_zero_variance`, `test_close_shift_positive_variance_overage_posts_gl`, `test_close_shift_negative_variance_shortage_posts_gl`, `test_close_shift_no_gl_entry_on_exact_reconciliation`, `test_expected_cash_handles_change_correctly`, `test_expected_cash_excludes_noncash_tender`, `test_expected_cash_includes_cash_refund_reduction`.

## Session F: concurrency

`tests/test_shifts_concurrency.py`, 6 tests, real PostgreSQL with genuinely independent `SessionLocal()` connections per thread (the `db` fixture's savepoint isolation would make one thread's setup invisible to another's connection — same discipline `test_purchasing_concurrency.py` documents for itself):

1. Two concurrent shift opens for the same cashier, different keys → exactly one succeeds, the other gets `SHIFT_ALREADY_OPEN`.
2. Two concurrent close requests for the same shift, same key (idempotent retry) → both succeed, identical result, one CLOSED row.
3. **Sale finalization racing shift closure** — the highest-stakes scenario (see `M15_DESIGN.md` §6): deterministically forced via a barrier + a controlled lock-hold delay. Proven: the sale that wins the race is always attributed to the shift and always counted in its expected cash (opening 100 + a 20 cash sale that legitimately committed first = 120 expected, matching what was counted).
4. Cash movement racing shift closure — resolves deterministically in either winning order, no lost update.
5. Duplicate cash-movement requests with the same key, genuinely concurrent → exactly one row created.
6. **Two concurrent close requests with DIFFERENT keys** (a genuine double-close attempt, not a retry — e.g. two browser tabs) → exactly one succeeds, the other is rejected `SHIFT_NOT_OPEN`, and at most one `CASH_SHIFT_VARIANCE` journal entry exists afterward. Added during the adversarial self-review (§21 of the task) after noticing the same-key test alone didn't prove this distinct case.

**Final status**: 6/6 passing, all against real Postgres row locks (no mocking).

## Session G: failure injection

`tests/test_shifts_failure_injection.py`, 7 tests, mirroring `test_sale_finalization_failure_injection.py`'s exact monkeypatch-and-roll-back methodology:

1. Failure during shift-open audit logging → no shift row survives.
2. Retry after a failed open, same key → succeeds cleanly.
3. Failure during cash-movement audit logging → no movement row survives, shift stays OPEN.
4. Failure during expected-cash calculation (between acquiring the shift's lock and mutating it) → shift remains fully OPEN, every closing field untouched.
5. **Failure during GL posting** — the highest-stakes boundary, since by this point the shift's closing fields have already been flushed (but not committed) in the same transaction: proven that the shift is never durably CLOSED without its GL entry, and the GL entry is never posted without the shift being durably closed (both roll back together).
6. Retry after a failed close, same key → succeeds cleanly, correct variance, GL entry present.
7. The `SHIFT_CLOSE_INITIATED` audit entry (logged before the mutation) is itself rolled back along with everything else on a downstream failure — it is not independently committed, unlike M14's deliberate commit-on-rejected-approval pattern; this function never calls `db.commit()` anywhere.

**Final status**: 7/7 passing. No partial financial state was ever observed to survive a rollback.

## Session H: security / adversarial

Folded into Session D (`test_shifts.py`) — see the security/adversarial test list there. Additionally covered directly in Session F/G's own scope: a genuinely concurrent bypass attempt (F) and a direct-service-layer bypass attempt (`test_direct_service_call_cannot_bypass_override_check`, Session D) both failed to bypass authorization.

## Session I: mutation testing

Method: identical to every milestone since M9 — apply a targeted change directly to `app/modules/shifts/service.py`, run the specific test(s) expected to catch it, confirm failure, revert via exact string-restore, confirm `grep -c MUTATION` returns 0 and the relevant tests pass again before the next cycle.

**Result: 9/9 named mutations applied live and detected.**

1. Remove `close_shift`'s self-vs-override authorization check → `test_cashier_cannot_close_another_cashiers_shift`, `test_direct_service_call_cannot_bypass_override_check` failed. Detected.
2. Remove `record_cash_movement`'s self-vs-override authorization check → `test_cashier_cannot_record_movement_on_another_cashiers_shift` failed (a test added during this mutation session, closing a real coverage gap the mutation itself exposed). Detected.
3. Remove store-isolation enforcement in `close_shift` → `test_cross_store_manager_cannot_override_close` failed. Detected.
4. Flip the sign on `change_given` in the expected-cash formula (subtract → add) → `test_expected_cash_handles_change_correctly` failed. Detected.
5. Omit `opening_float` from the expected-cash formula → `test_close_shift_exact_reconciliation_zero_variance` failed. Detected.
6. Include non-cash payments in the expected-cash formula (drop the `CASH`-only filter) → `test_expected_cash_excludes_noncash_tender` failed. Detected.
7. Omit `cash_refunded` from the expected-cash formula → `test_expected_cash_includes_cash_refund_reduction` failed. Detected.
8. Permit closing an already-CLOSED shift (bypass the status check in `close_shift`) → `test_close_already_closed_shift_conflict` failed. Detected.
9. Permit a cash movement against a CLOSED shift (bypass the status check in `record_cash_movement`) → `test_cash_movement_rejected_on_closed_shift` failed. Detected.

No mutation was ever left applied between cycles (`grep -c MUTATION app/modules/shifts/service.py` → 0 after the session); full `test_shifts*.py` (44 tests) + ruff/black/mypy re-verified clean immediately after.

**A real gap found by this session itself**: mutation #2 initially had no test to catch it (`record_cash_movement`'s cross-cashier authorization had never been directly exercised). Rather than skip the mutation or weaken its target, `test_cashier_cannot_record_movement_on_another_cashiers_shift` was written first, confirmed to pass against the real (unmutated) code, and only then was the mutation applied and confirmed to fail it — the same discipline this milestone's own instructions require: a mutation-testing session finding a real coverage gap is itself a successful outcome, not something to route around.

## Session J: full regression

Backend: `pytest -q` (full suite, including the new `tests/test_shifts.py`, `test_shifts_concurrency.py`, `test_shifts_failure_injection.py`, `test_inventory_idempotency.py`, and the updated `test_migrations.py`/`test_accounting_api.py`).

**Real regression found and fixed**: `test_accounting_api.py::test_manager_can_read_accounts_and_journals` hardcoded the chart-of-accounts count at 25 (the exact count as of M10). Adding the M15 `Cash Over/Short` account made it 26. Fixed the same way every prior milestone's equivalent count assertion (permission counts, table counts) has been bumped — updated the assertion and its explanatory comment to name the new account, not weakened or removed.

Backend final: **all tests passing** (see exact count in `M15_HARDENING_AUDIT.md` §1). ruff/black/mypy clean.

Frontend: `npx vitest run` — 39/39 passed (37 baseline + 2 new `PosPage.test.tsx` shift-panel tests: opens a till from the empty-till prompt, and closes an active till showing the reported variance). oxlint clean. prettier clean. `tsc -b` clean. `vite build` clean.

**Real bug found and fixed in the frontend**: the shift-fetch `useEffect` originally ran once on mount with an empty dependency array, reading `hasPermission('shift.manage')` before `AuthProvider`'s async silent-refresh + `/auth/me` chain had resolved `user` — so it always evaluated against a not-yet-authenticated, permission-less state and never re-ran once the real user (and their real permissions) became available. This is a genuine bug independent of the tests (a real cashier's shift panel would never have appeared either) — caught by the second shift-panel test failing with the shift banner never rendering at all. Fixed by gating the effect on `user` and keying it in the dependency array, so it correctly re-evaluates once authentication resolves.
