# M15 Hardening Audit — Cashier/Till Shift Session Management

## 1. Result summary

| Gate | Result |
|---|---|
| Backend tests | 894 passed (849 pre-M15 baseline + 5 stock-adjustment-idempotency + 27 shift functional/financial/security + 6 shift concurrency + 7 shift failure-injection), 0 failed |
| Frontend tests | 39 passed (37 pre-M15 + 2 new shift-panel tests), 0 failed |
| Backend ruff | clean |
| Backend black | clean |
| Backend mypy (`app`) | clean, 95 source files |
| Frontend oxlint | clean |
| Frontend prettier | clean |
| Frontend `tsc -b` + `vite build` | clean |
| Migration head | `db482a11ee31`, two new migrations on top of `e0d2359bb08a` (`1a4bae98d246` pre-M15 hardening, `db482a11ee31` M15 itself) |
| Migration up/down/up cycle | fresh-DB upgrade clean; full downgrade-to-base then upgrade-to-head cycle passes via `test_migrations.py`; guard-first downgrade refusal proven for both new migrations |
| Mutation testing | 9/9 applied mutations detected, 0 skipped/inapplicable |

## 2. Defects found and fixed during this milestone

### 2.1 `updated_at` NOT NULL migration bug (HIGH — would have broken every shift-related write in production)
The first migration draft defined `cashier_shifts.updated_at`/`cash_movements.updated_at` as `nullable=False, server_default=now()`. `TimestampMixin.updated_at` (the ORM base every table in this codebase uses) is `Mapped[datetime | None]` with `onupdate=func.now()` only — it is never set on INSERT, only on a later UPDATE. Every single INSERT into either new table would have failed with `NotNullViolation` in any environment. Caught immediately: 23 of 26 tests in the first real run of `tests/test_shifts.py` failed with the identical `IntegrityError`. Fixed by matching every prior TimestampMixin-table migration's exact column definition (`nullable=True`, no server default) — confirmed against `be26de9d9459` (M10) as the reference pattern. Required a clean `alembic downgrade -1` / fix / `upgrade head` cycle; no real data existed yet in the sandbox, so the downgrade was uneventful.

### 2.2 Missing `shift.read` grant for Cashier (MEDIUM — would have silently hidden a cashier's own shift history from themselves)
The first RBAC draft granted Cashier only `shift.manage`. A cashier could open/close their own till and record cash movements, but `GET /shifts/{id}` and `GET /shifts/{id}/cash-movements` — including for their **own** shift — returned 403. Caught by `test_cash_movement_paid_in_and_paid_out` and `test_cross_store_cashier_cannot_view_another_stores_shift` failing with `FORBIDDEN: Missing required permission: shift.read`, not the expected data/404. Fixed by granting `shift.read` to Cashier alongside `shift.manage`, mirroring the existing `pos.use`+`sales.read` pairing already established for every other role in this permission matrix — not a new pattern, a consistency fix. Required a second downgrade/upgrade cycle to reseed the corrected grant.

### 2.3 Sale finalization vs. shift closure race (HIGH — a real, silent till-shortage bug if unfixed)
Not a bug that shipped and was caught later — identified and designed around *before* implementation, during the design-validation pass, then proven correct by dedicated concurrency testing (see `M15_DESIGN.md` §6 and `M15_TESTING_SESSIONS.md` Session F #3). Documented here because it is the single most consequential correctness property in this milestone: without the `FOR UPDATE`-based serialization between `finalize_sale`/`create_sale_return` and `close_shift`, a legitimately-committed cash sale could be silently excluded from its shift's frozen `expected_cash_amount`, permanently understating the till by that sale's amount with no way to detect it after the fact (the shift is closed; nothing re-derives its expected cash later).

### 2.4 Missing test coverage for `record_cash_movement`'s cross-cashier authorization (found by mutation testing)
Mutation #2 (removing the self-vs-override check in `record_cash_movement`) initially had no test to catch it. Rather than skip the mutation, `test_cashier_cannot_record_movement_on_another_cashiers_shift` was written, verified to pass against the real code, and only then was the mutation applied and confirmed to fail it. Documented in full in `M15_TESTING_SESSIONS.md` Session I.

### 2.5 Chart-of-accounts hardcoded count (regression in an unrelated pre-existing test, LOW)
`test_accounting_api.py::test_manager_can_read_accounts_and_journals` hardcoded the seeded account count at 25 (accurate through M14). The new `Cash Over/Short` account (§9 below) made it 26. Fixed the same way every prior milestone's equivalent count assertions have been bumped — updated the number and its explanatory comment, not weakened.

### 2.6 Frontend: shift-fetch effect read a stale pre-authentication permission set (MEDIUM — the shift panel would never have appeared for any real user)
`PosPage`'s shift-fetch `useEffect` originally ran once on mount with `[]` deps, calling `hasPermission('shift.manage')` before `AuthProvider`'s async silent-refresh + `/auth/me` chain had populated `user` — so it always evaluated against a not-yet-authenticated state and never re-ran once real permissions became available. A genuine bug, not a test artifact: it would have hidden the shift panel from every real cashier, not just the mocked test user. Caught by the second `PosPage` shift-panel test (the "closes an active till" case) failing with no shift banner rendered at all. Fixed by keying the effect on `user` and guarding with an early return until it resolves.

## 3. Business invariant verification (this milestone's own quality gates, checked explicitly)

- **Server-side enforcement**: every authorization, store-isolation, idempotency, and financial-computation rule lives in `app/modules/shifts/service.py`, re-checked independently of the HTTP layer (`test_direct_service_call_cannot_bypass_override_check` calls the service function directly, bypassing the endpoint's permission dependency entirely, and is still correctly rejected).
- **Frontend cannot bypass it**: `PosPage`'s shift panel only ever displays server-confirmed state (`GET /shifts/active`) and never computes expected cash or a variance itself — every number shown after a close comes straight from the `close_shift` API response.
- **No orphaned financial state under failure**: proven directly by `tests/test_shifts_failure_injection.py` (7/7) — a failure at any of the tested boundaries (audit logging during open/movement, expected-cash calculation, GL posting during close) leaves nothing durably committed; a CLOSED shift is never observed without its GL entry, and vice versa.
- **No lost update under concurrency**: proven directly by `tests/test_shifts_concurrency.py` (6/6), including the highest-stakes sale-vs-close race (§2.3 above) and a genuine (different-key) double-close attempt.
- **GL remains balanced**: `sum(debit) == sum(credit)` asserted directly against posted `JournalLine` rows in `test_close_shift_positive_variance_overage_posts_gl`/`..._negative_variance_...`, in addition to the DB-level deferred constraint trigger that already enforces this for every journal entry in the system.
- **No duplicate financial posting**: the `uq_journal_entries_source` partial unique index (pre-existing, reused unchanged) backstops `CASH_SHIFT_VARIANCE` exactly as it does every other automated source type; the different-key double-close test additionally confirms at most one variance entry survives a genuine race.
- **Existing sales/returns behavior intact**: the full pre-existing `test_sales*.py`/`test_pos_api.py`/`test_return_void_approval.py` suite (96+ tests) passes unmodified against the shift-integrated `finalize_sale`/`create_sale_return`.

## 4. RBAC / role matrix verification

`shift.manage` granted to: Admin (via `list(ALL_PERMISSIONS)`), Manager (explicit), Cashier (explicit). `shift.read` granted to: Admin, Manager, Cashier, Auditor (all explicit or implicit). `shift.override` granted to: Admin, Manager only — confirmed by reading `permissions.py`'s `ROLE_PERMISSIONS` dict directly, and by `test_cashier_cannot_close_another_cashiers_shift`/`test_cashier_cannot_record_movement_on_another_cashiers_shift` exercising the Cashier-lacks-override case specifically (the most important negative case, since Cashier is the role that operates this feature day to day).

## 5. Mutation testing — full detail

See `docs/M15_TESTING_SESSIONS.md` Session I for the complete procedure and per-mutation results. Summary: **9 of 9 named mutation targets applied as live code mutations** (each: apply, confirm the relevant test(s) fail, revert via exact string-restore, confirm `grep -c MUTATION` returns 0 and the full targeted suite is clean again before the next cycle) — **all 9 detected**, none skipped or assessed-inapplicable. One mutation (removing `record_cash_movement`'s authorization check) exposed a genuine pre-existing test-coverage gap, closed by writing the missing test before applying that mutation — the same discipline M11–M14 applied to their own mutation-testing sessions when a target initially lacked coverage.

## 6. Adversarial self-review (targeted, M15-specific — the 19-question checklist from the task's own §21)

1. Can the same cash be counted twice? No — `expected_cash_amount` is computed exactly once, inside the atomic close transaction, from each attributed `Sale`/`SaleReturn`/`CashMovement` row summed exactly once; a CLOSED shift can never be re-closed (status check, mutation-tested).
2. Can a closed shift receive a sale? No — `lock_active_shift_for_cashier` filters `status = 'OPEN'` under `FOR UPDATE`, re-evaluated against the latest committed row on unblock (READ COMMITTED semantics); proven under real concurrency in `test_sale_finalization_racing_shift_close_is_never_lost`.
3. Can a sale attach to another cashier's shift? No — the lookup is filtered by the exact cashier processing the sale, never a client-supplied shift id.
4. Can a user from another store manipulate a shift? No — `_enforce_store_access` checked in both `record_cash_movement` and `close_shift`, mutation-tested (mutation #3).
5. Can a retry double-post? No — `client_transaction_id`/`close_client_transaction_id` idempotency with `IntegrityError`-recovery, proven under genuine concurrency (Session F #2, #5).
6. Can two concurrent closes both succeed? No — same-key race returns one shared result (Session F #2); different-key race (a genuine double-close) rejects the loser with `SHIFT_NOT_OPEN` (Session F #6, added during this self-review).
7. Can two concurrent opens both succeed? No — partial unique index + `IntegrityError` recovery, proven under real concurrency (Session F #1).
8. Can a failed close leave partial GL state? No — proven by `test_failure_during_gl_posting_leaves_no_closed_shift_without_variance`: a CLOSED status flushed-but-uncommitted is rolled back along with the (never-attempted-to-be-committed) GL entry.
9. Can non-cash tender affect physical cash? No — excluded by the `payment_method == 'CASH'` filter, mutation-tested (mutation #6).
10. Is cash change handled correctly? Yes — proven against the task's own $100/$150/$50 worked example end to end through a real sale + close, mutation-tested (mutation #4).
11. Can a manager override without the intended permission? No — `shift.override` is a distinct grant from `shift.manage`; a Cashier (who has `shift.manage` but not `shift.override`) is explicitly proven unable to close another cashier's shift.
12. Can the cashier bypass the manager requirement through the service layer? No — `test_direct_service_call_cannot_bypass_override_check` calls `close_shift` directly, bypassing the HTTP endpoint's permission dependency entirely, and is still rejected.
13. Is any monetary value represented with floating point? No — every amount is `Decimal`/`Numeric`, confirmed by direct code review of `app/modules/shifts/models.py` and `service.py`.
14. Did we create a second source of truth? No — `expected_cash_amount` is a derived, frozen fact (like `Sale.grand_total`), not an independently-maintained running balance; justified explicitly in `M15_DESIGN.md` §2 against the `PurchaseInvoice.balance_due` counter-example.
15. Can historical financial state be mutated improperly? No — `CashierShift` closing fields are set exactly once (status-guarded); `CashMovement` rows are never edited by any route or service function after creation.
16. Does downgrade destroy live financial data? No — both new migrations guard-first refuse to drop tables/columns if any real shift/variance data exists, mirroring `e0d2359bb08a`'s own discipline exactly.
17. Are audit records sufficient to reconstruct the event? Yes — `SHIFT_OPENED`/`CASH_MOVEMENT_CREATED`/`SHIFT_CLOSE_INITIATED`/`SHIFT_CLOSED`/`*_REJECTED` each carry the relevant actor, amounts, and reason; no credentials are ever logged (this flow has no credential-verification surface to log, unlike M14).
18. Did any frontend behavior accidentally become the security boundary? No — the shift panel is purely display/relay; every enforcement decision is server-side, confirmed by direct code review of `PosPage.tsx`'s `ShiftPanel` component.
19. Did we accidentally introduce a jurisdiction/currency/fiscal assumption? No — no currency column added, no jurisdiction-specific rule anywhere; GL posting reuses existing chart-of-accounts infrastructure only.

## 7. What M15 deliberately did not build

See `docs/M15_DESIGN.md` §11 for the full list and reasoning: a general multi-till model (one shift per cashier is this milestone's bounded scope), mandatory shift enforcement on checkout (opportunistic attribution only, to avoid breaking every pre-existing POS test), GL posting for individual cash movements (no evidenced category to post to — folded into the shift-level variance instead), a currency model (a pre-existing, cross-cutting gap this milestone doesn't attempt to close), and hardware integration.

## 8. Exact files changed

**Pre-M15 hardening**: `app/modules/inventory/models.py` (`StockAdjustment.client_transaction_id`), `app/modules/inventory/service.py` (`create_stock_adjustment` idempotency), `app/modules/inventory/schemas.py`, `app/api/v1/endpoints/inventory.py`, migration `1a4bae98d246`, new `tests/test_inventory_idempotency.py`.

**M15 backend**: new module `app/modules/shifts/` (`__init__.py`, `models.py`, `service.py`, `schemas.py`), `app/api/v1/endpoints/shifts.py` (new), `app/api/v1/api.py` (router registration), `app/modules/sales/models.py` (`Sale.shift_id`, `SaleReturn.shift_id`), `app/modules/sales/service.py` (shift attribution + locking in `finalize_sale`/`create_sale_return`), `app/modules/auth/permissions.py` (three new permissions + role grants), `app/modules/accounting/constants.py` (`ACCOUNT_CASH_OVER_SHORT`), `app/modules/accounting/models.py` (`CASH_SHIFT_VARIANCE` source type), `app/modules/accounting/service.py` (`post_cash_shift_variance_journal`), `app/modules/audit/service.py` (`cashier_shift` store-scoping entry), `app/db/base.py` (model registration), migration `db482a11ee31`, `tests/test_migrations.py` (updated head revision/table count/permission count), new `tests/test_shifts.py` (27 tests), `tests/test_shifts_concurrency.py` (6 tests), `tests/test_shifts_failure_injection.py` (7 tests), `tests/test_accounting_api.py` (updated account count).

**Frontend**: `src/api/shifts.ts` (new), `src/pages/PosPage.tsx` (shift panel integration + the `useEffect` timing fix), `src/pages/PosPage.test.tsx` (2 new tests).

**Docs**: `docs/M15_DESIGN.md`, `docs/M15_TESTING_SESSIONS.md`, this document.

## 9. Final verdict: PASS WITH CONDITIONS

Conditions (explicit, not defects):
- One active shift per cashier is a deliberate, bounded scope decision — no multi-till-per-cashier model exists (§7).
- Shift attribution to a sale/return is opportunistic, never mandatory — a store cannot currently *require* its cashiers to have an open shift before ringing up a sale (§7); this was the deciding scope boundary against breaking pre-existing POS test coverage.
- Cash movements are not yet linked to a specific GL expense/revenue category — their cash effect is folded into the shift-level variance at close time rather than posted individually (§9 of the design doc), pending an evidenced business requirement for what category they belong to.
- No admin UI/API to configure anything beyond the shift lifecycle itself exists yet (matches the precedent every prior milestone's own unconfigured-by-default settings have set, e.g. `attendance_day_boundary_hour`, `return_approval_threshold_amount`).
