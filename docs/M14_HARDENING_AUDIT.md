# M14 Hardening Audit — Return/Void Approval-Threshold Enforcement

## 1. Result summary

| Gate | Result |
|---|---|
| Backend tests | 849 passed (823 pre-M14 + 26 new), 0 failed |
| Frontend tests | 37 passed (36 pre-M14 + 1 new), 0 failed |
| Backend ruff | clean |
| Backend black | clean |
| Backend mypy (`app`) | clean, 90 source files |
| Frontend oxlint | clean |
| Frontend prettier | clean |
| Frontend `tsc -b` + `vite build` | clean |
| Migration head | `e0d2359bb08a`, one new migration on top of `17fb9afe8d39` |
| Migration up/down/populated test | fresh-DB upgrade clean; populated-DB (`erp_dev`) downgrade correctly refused; full up/down/up cycle passes via `test_migrations.py` |
| Mutation testing | 7/7 applied mutations detected; 2/9 documented as not meaningfully applicable (§5) |

## 2. Defects found and fixed during this milestone

### 2.1 Migration seed-data race (HIGH — would break every future from-scratch deployment)
`e6180fca2ee0` (M2's seed migration) reads `app.modules.auth.permissions.ALL_PERMISSIONS`/`ROLE_PERMISSIONS` **live** at migration-run time. Adding `SALES_RETURN_APPROVE` to those dicts (required for the runtime permission check) meant M2's seed migration already inserts it on a from-scratch upgrade, before M14's own migration runs — the first draft's bare `INSERT INTO permissions` then hit a `UniqueViolation`. Fixed with `ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code RETURNING id`, the exact pattern every migration since M4 uses for this reason. Caught by `tests/test_migrations.py::test_full_upgrade_downgrade_upgrade_cycle` — a full-suite run would have shipped this undetected if that test hadn't been run as part of the required regression gate.

### 2.2 Audit-log store-scoping gap (MEDIUM — would have silently hidden the new audit trail from its intended audience)
The M14 approval audit events (`SALE_RETURN_APPROVAL_GRANTED`/`_FAILED`) use `entity_type="store"` since there is no per-approval row to point at. `app/modules/audit/service.py`'s store-scoping map had no entry for this entity type, so a store-scoped Manager (the exact role this feature is about) would never see it via the existing `/audit-logs` endpoint — silently excluded as "unknown entity_type," not surfaced as a gap. Fixed by adding `"store"` as a join-resolved case (`select(Store.id).where(Store.id == store_id)` — special-cased because, unlike every other entry in that map, `Store` has no `store_id` foreign-key column pointing at itself). Caught by re-running `test_audit_log_endpoint.py`/`test_audit_hardening.py` as part of the required regression pass, not discovered by a dedicated M14 test — a reminder that "run the existing suite" is itself a hardening step, not a formality.

### 2.3 `store_settings` did not exist (discovery-report discrepancy, not a code defect)
Documented in full in `docs/M14_DESIGN.md` §1. Investigated per the task's explicit instruction rather than trusted; resolved by following the repository's real, established per-store-config precedent (a typed `Store` column) instead.

## 3. Business invariant verification (the task's own quality gates, checked explicitly)

- **Business invariant enforced server-side**: yes — `_resolve_return_approval` runs inside `create_sale_return`, independent of any client input beyond the two new optional fields; the existing endpoint permission gates (`sales.return.write`, `sales.void`) are unmodified.
- **Frontend cannot bypass it**: the frontend only ever learns approval is needed from a real `APPROVAL_REQUIRED` server response (`SalesPage.tsx`'s `needsApproval` state is set exclusively inside the catch block of a failed submit) — it never predicts or computes the threshold client-side.
- **Direct API calls cannot bypass it**: `test_direct_api_call_cannot_bypass_approval`, `test_direct_api_call_with_cashier_as_approver_is_rejected` — both go through the real HTTP endpoint, not the service function directly.
- **Authorization cannot be bypassed**: permission check (mutation #3), self-approval block (mutation #4) — both live-mutation-tested and detected.
- **Store isolation cannot be bypassed**: mutation #5, live-tested and detected.
- **Approval cannot be replayed / cannot authorize a different operation**: structural property of the design (§2 of the design doc), not merely an enforced rule — verified in §5 below.
- **No orphaned approval state**: `test_rejected_approval_leaves_no_sale_return_row`, `test_failed_approval_credentials_leave_no_partial_state` — both assert zero rows/zero inventory/zero `quantity_returned` change after a rejected attempt.
- **No duplicate financial posting introduced**: the approval gate adds a read-only check before the existing, unmodified posting logic; `test_approved_return_produces_identical_gl_and_inventory_effects` proves the GL entry balances and the inventory movement/product quantity match exactly what an unapproved (below-threshold) return would produce.
- **GL remains balanced**: same test — `sum(debit) == sum(credit)` asserted directly against the posted `JournalLine` rows, in addition to the DB-level constraint trigger that already enforces this for every journal entry in the system.
- **Inventory remains reconciled**: same test — `InventoryMovement.quantity_delta` and `Product.current_qty_on_hand` asserted against the exact expected values.
- **Existing idempotency remains intact**: `test_return_idempotency_unaffected_below_threshold` (identical to the pre-existing test's own assertions) and `test_void_idempotency_unaffected_below_threshold` (updated to reflect actual, pre-existing — not M14-changed — void-retry behavior; see `M14_TESTING_SESSIONS.md` Session 1).
- **Existing return/void behavior intact when no approval required**: the entire pre-M14 test suite (`test_sales_returns*.py`, 49 tests) passes unmodified against the refactored code.

## 4. RBAC / role matrix verification

`sales.return.approve` granted to: Admin (via `list(ALL_PERMISSIONS)`), Manager (explicit grant). **Not** granted to: Cashier, Inventory Clerk, Auditor, HR Clerk — confirmed by reading `permissions.py`'s `ROLE_PERMISSIONS` dict directly, and by `test_unauthorized_cashier_cannot_approve`/`test_direct_api_call_with_cashier_as_approver_is_rejected` exercising the Cashier case specifically (the role most likely to be the initiator in a real store, so the most important negative case).

## 5. Mutation testing — full detail

See `docs/M14_TESTING_SESSIONS.md` Session 4 for the complete procedure and per-mutation results. Summary: 7 of 9 named targets applied as live code mutations (each: apply, confirm the relevant test(s) fail, revert via exact string-restore, confirm `grep -c MUTATION` returns 0 and the full regression is clean again before the next cycle) — all 7 detected. 2 of 9 (reuse of an already-used approval; moving the approval check outside the transaction boundary) assessed and not applied as live mutations, each with a documented architectural reason (no detachable approval object exists to reuse in this design; a naive relocation mutation would be masked by the codebase's own standard rollback-on-exception infrastructure rather than by this feature's logic) — the same discipline M13 applied to its two skipped mutations, not a weakened or discarded target.

## 6. Adversarial review (targeted, M14-specific)

1. Can a Cashier bypass approval by simply omitting the approver fields? No — `APPROVAL_REQUIRED`, tested at both service and HTTP layers.
2. Can the initiator approve their own operation by supplying their own credentials? No — `SELF_APPROVAL_NOT_ALLOWED`, strict id equality, mutation-tested.
3. Can the inline approver-credential check be brute-forced without throttling? No — `approval_rate_limiter`, same limit/window as login, keyed by client IP, wired at the endpoint layer before the service call.
4. Can a Manager from a different store approve? No — reuses the file's own established `_enforce_store_access`, mutation-tested.
5. Can a rejected approval leave the sale in a partially-refunded or otherwise inconsistent state? No — the gate runs before any mutation; verified by direct assertion on `quantity_returned`/`current_qty_on_hand` remaining unchanged.
6. Can the amount a manager approves differ from the amount actually posted? No — both come from one function call (`_compute_line_refund`) over the same inputs; structurally, not just by test coverage.
7. Is the new audit trail visible to the store-scoped role most likely to need it (a store Manager reviewing their own store's overrides)? Yes, after fixing 2.2.
8. Does an inactive approver account succeed? No — `verify_user_credentials` checks `user.is_active`, same as `authenticate_user`.
9. Do voids get a free pass around the gate because `sales.void` is already Manager/Admin-only? No — deliberately not exempted; a Manager-initiated void above threshold still needs a *second* Manager or an Admin. Mutation #9 proves the gate isn't accidentally skipped for the void code path.
10. Does a store with the gate never configured (`NULL` threshold) fail open or fail closed? Fails open in the sense of "unchanged from pre-M14 behavior" — deliberate, documented, not silently permissive: no store had any approval control before this milestone, so `NULL` preserving that is not a regression.

## 7. What M14 deliberately did not build (stated up front, per M11–M13's own discipline)

1. No cumulative/structuring detection across multiple separate return transactions against the same sale (§9 of the design doc) — a materially larger feature, not requested.
2. No admin UI or API endpoint to set a store's threshold — same precedent as `attendance_day_boundary_hour` (M10), which also has none; configured via direct DB/script today.
3. No generic approval/workflow engine — a deliberate rejection, not an oversight (design doc §2).
4. No live "move the approval check outside the transaction" mutation (§5) — assessed as not meaningfully constructible without destabilizing the function or being masked by unrelated infrastructure; verified by static reading + orphaned-state tests instead.

## 8. Exact files changed

Backend: `app/modules/auth/models.py` (Store column), `app/modules/sales/models.py` (SaleReturn column), `app/modules/auth/permissions.py` (new permission + Manager grant), `app/modules/auth/service.py` (`verify_user_credentials`), `app/core/rate_limit.py` (`approval_rate_limiter`), `app/modules/sales/service.py` (enforcement logic, refactored `_compute_line_refund`), `app/modules/sales/schemas.py` (request/read fields), `app/api/v1/endpoints/sales.py` (rate-limit wiring, pass-through), `app/modules/audit/service.py` (store-scoping fix), one new Alembic migration (`e0d2359bb08a`), `tests/conftest.py` (register new rate limiter in the autouse clear fixture), `tests/test_migrations.py` (updated hardcoded head revision/permission count), new `tests/test_return_void_approval.py` (26 tests). Frontend: `src/api/sales.ts` (types), `src/pages/SalesPage.tsx` (approval UI), `src/pages/SalesPage.test.tsx` (1 new test). Docs: `docs/M14_DESIGN.md`, `docs/M14_TESTING_SESSIONS.md`, this document.

## 9. Final verdict: PASS WITH CONDITIONS

Conditions (explicit, not defects):
- No cumulative/structuring protection across multiple separate return transactions (§7.1).
- No admin UI/API to configure the per-store threshold; must be set via direct database/script access until a future milestone (§7.2).
- Threshold configuration itself is not migration-seeded with any default value — every store is unconfigured (gate inactive) until explicitly set, consistent with "do not invent a monetary amount without evidence."
