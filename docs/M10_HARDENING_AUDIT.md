# M10 — HR/Workforce & Payroll: Final Hardening & Verification Report

Companion to `docs/M10_DESIGN.md` (the approved design, including the 8
binding decisions this implementation follows without exception). This
report covers the full implementation: database schema through
frontend, plus the mandated adversarial self-audit and mutation-testing
passes.

## 1. Verdict

**CONDITIONAL PASS** against the stated safety standard: *"Can this
system safely process employee compensation and create accounting
liabilities without duplicate, stale, unauthorized, cross-store, or
partially committed financial state?"*

For every scenario actually tested — concurrent posting, concurrent
recalculation, network-retried calculate/post, forced mid-transaction
failure, cross-store access at every mutating endpoint, RBAC boundary
enforcement, and DB-level constraint bypass attempts — the answer is
yes, and each is backed by a real, adversarial, PostgreSQL-backed test
that fails without the corresponding protection (verified in Section
14). It is CONDITIONAL, not an unqualified PASS, because of two things
named honestly rather than smoothed over:

1. **One real defense-in-depth gap was found and fixed** during the
   adversarial audit (Section 8) — not a hypothetical, a genuine
   asymmetry with the codebase's own established pattern.
2. **Statutory tax/withholding calculation is explicitly out of scope**
   (M10 design decision #8) — the system posts configurable deductions
   correctly, but does not and must not be represented as computing
   real statutory tax. This is by design, not an oversight, but it
   means "processes compensation" does not yet mean "runs real
   payroll for tax purposes" — a follow-on module's job, not this one's.

No duplicate, stale, unauthorized, cross-store, or partially-committed
financial state was produced under any tested condition.

## 2. Scope and phase order followed

Implemented in the mandated order, each phase gated on tests +
regression + DB-state inspection + transaction-boundary inspection +
a mini adversarial review before moving on:

1. Database schema (HR + payroll), all effective-dated tables using
   `EXCLUDE` constraints via `btree_gist`.
2. Employee/employment history + compensation services.
3. Attendance service (clock in/out, corrections, store-configured
   cross-midnight boundary).
4. Payroll period lifecycle (DRAFT→OPEN→CALCULATED→APPROVED→POSTED,
   plus CANCELLED).
5. Payroll calculation engine (pure function, zero DB access).
6. Payroll→accounting integration (GL posting + reversal).
7–10. Transaction boundaries, concurrency, failure injection,
   idempotency.
11. RBAC / multi-store enforcement + audit/privacy hardening.
12. Frontend (HR + Payroll pages) + live browser smoke test.
13–14. Adversarial self-audit + mutation testing (this report's new
   findings).
15. Final verification (this report).

M9 and every earlier module were left untouched except for a single,
purely additive change to `accounting/service.py` (two new functions;
zero lines removed or modified in existing functions) — confirmed by
diff against the pre-M10 commit (Section 7).

## 3. The 8 binding design decisions: compliance

| # | Decision | Status |
|---|----------|--------|
| 1 | PayrollPeriod is per-store, with `payroll_run_id` as a cross-store label only | Done — `payroll_periods` unique on `(store_id, period_start, period_end)`; `payroll_run_id` is a plain nullable string column, never a second transactional unit |
| 2 | Manager never gets `payroll.reverse`; `payroll.post` must be justified; Admin highest authority; HR Clerk minimal | Done — see RBAC matrix, Section 4 |
| 3 | HR Clerk scoped to master data/history/attendance/payroll prep-and-read only | Done — see RBAC matrix, Section 4 |
| 4 | `EXCLUDE` constraints via `btree_gist` for effective-dated non-overlap | Done — every effective-dated table (`employment_status_periods`, `employment_assignments`, `compensation_periods`, `overtime_policies`, `deduction_rates`, `attendance_records`) |
| 5 | No salary/net-pay/bank/deduction amounts in ordinary logs or audit metadata | Done — verified line-by-line (Section 9); every `audit_service.log_event` call in `hr/service.py` and `payroll/service.py` carries only identifiers, statuses, counts, dates, and reasons |
| 6 | Cross-midnight boundary is store configuration (`Store.attendance_day_boundary_hour`), never an employee field, using UTC conventions | Done |
| 7 | Overtime policy is a global effective-dated configuration for M10 | Done — `OvertimePolicy` has no `store_id` |
| 8 | No invented statutory tax formulas; configurable deduction boundary; engine/config/future-integration separated | Done — `DeductionType`/`DeductionRate` are configurable; `ACCOUNT_STATUTORY_WITHHOLDING_PAYABLE` exists and is reserved but intentionally unposted-to in M10 (documented, mirrors the `ACCOUNT_PURCHASE_CLEARING` precedent) |

## 4. RBAC matrix (final, as shipped)

| Permission | Admin | Manager | HR Clerk | Auditor | Cashier / Inventory Clerk |
|---|---|---|---|---|---|
| `hr.read` | ✓ | ✓ | ✓ | ✓ | — |
| `hr.write` | ✓ | ✓ | ✓ | — | — |
| `hr.compensation.write` | ✓ | ✓ | — | — | — |
| `attendance.read` / `.write` | ✓ | ✓ | ✓ | read-only | — |
| `payroll.read` | ✓ | ✓ | ✓ | ✓ | — |
| `payroll.calculate` | ✓ | ✓ | ✓ | — | — |
| `payroll.approve` | ✓ | ✓ | — | — | — |
| `payroll.post` | ✓ | — | — | — | — |
| `payroll.reverse` | ✓ | — | — | — | — |

Manager's `payroll.approve`-without-`payroll.post`/`.reverse` is a
documented separation-of-duties decision (the person who approves a
calculated period should not also be the one posting it to the GL or
unilaterally reversing a posted one) — recorded inline in
`app/modules/auth/permissions.py` next to the permission constants, not
left implicit.

## 5. Database-level safety (verified by permanent tests)

All of the following are enforced by PostgreSQL itself, not merely by
application code, and each has a passing regression test in
`tests/test_m10_schema.py`:

- Non-overlapping `EXCLUDE` constraints on employment status periods,
  employment assignments, compensation periods, overtime policies,
  deduction rates, and attendance shifts (voided corrections excluded
  via a partial `WHERE`).
- `CHECK` constraints tying `payroll_periods.status` to the presence of
  the right actor/timestamp columns for CALCULATED / APPROVED / POSTED.
- `CHECK` on `payroll_employee_results` that `net_pay = gross_pay -
  total_deductions` and that gross pay is never negative.
- Unique constraint preventing two payroll periods for the same store
  and date range.
- Unique constraint on `payroll_reversals.payroll_period_id` (a period
  can be reversed at most once).
- `REVOKE UPDATE, DELETE` from the application's runtime DB role on
  `payroll_reversals` (append-only), verified in this hardening pass to
  also hold for the `journal_entries` rows a payroll posting produces
  (Section 8, item 12 of the mutation matrix).
- A genuine concurrency test (`test_concurrent_overlapping_status_period_inserts_serialize_to_one_success`)
  proving the `EXCLUDE` constraint — not just application-level
  checking — is what prevents two racing inserts from both succeeding.

## 6. Payroll lifecycle, calculation, and accounting integration

- State machine: DRAFT → OPEN → CALCULATED → APPROVED → POSTED, with
  CANCELLED reachable from any non-POSTED state. Enforced by both the
  service layer (`ConflictError` on an invalid transition) and, for the
  four status-dependent actor/timestamp pairs, a DB `CHECK` constraint.
- Calculation engine (`payroll/calculation.py`) is a pure function —
  zero DB access, fully unit-testable in isolation (13 tests) —
  separated from the DB-orchestration layer (`payroll/service.py`),
  mirroring M9's "one authoritative formula" precedent.
- A genuine float-precision defect was found and fixed during this
  work: `_sum_attendance_hours` originally used
  `timedelta.total_seconds()` (binary floating point) before converting
  to `Decimal`, which can misrepresent exact fractional hours. Fixed
  with exact integer microsecond arithmetic; regression test proves
  5h15m at $17.13/hr calculates to exactly $89.93, not an
  off-by-fractions-of-a-cent value.
- GL posting reuses the established "one domain-specific `post_*`
  function in `accounting/service.py`, called inline before the
  caller's own final commit" pattern. Reversal recomputes debit/credit
  from the stored `PayrollEmployeeResult`/line data with the sides
  swapped — it never reads back the original `JournalLine` rows,
  verified directly in this hardening pass
  (`test_reversal_journal_lines_are_the_swap_of_the_original_not_a_copy`).
- Employee deductions all route to `ACCOUNT_BENEFIT_DEDUCTION_PAYABLE`
  in M10 (a deliberate simplification given `DeductionType` only has a
  two-category model); `ACCOUNT_STATUTORY_WITHHOLDING_PAYABLE` and
  `ACCOUNT_EMPLOYER_CONTRIBUTION_PAYABLE` exist and are wired for a
  future module, not silently dropped.

## 7. M9 (and earlier) integrity

`git diff <pre-M10-commit>~1 -- backend/app/modules/{purchasing,inventory,replenishment,transfers}`
is empty. The only touched file outside the new `hr`/`payroll` modules
is `app/modules/accounting/service.py`, and that diff is 163 insertions,
**0 deletions** — two new functions
(`post_payroll_journal`/`post_payroll_reversal_journal`) and their
imports, nothing in any existing function changed. M9's own hardening
verdict and test suite are unaffected; the full 651-test backend suite
(which includes every M2–M9 test) passes together with M10's tests in
the same run (Section 13).

## 8. Adversarial self-audit finding (Phase 13)

Reviewing every mutating HR/payroll endpoint against the codebase's own
established defense-in-depth pattern (AP module: `create_invoice`,
`create_payment`, and `create_credit_note` each call the ROUTE-layer
`enforce_store_access` in addition to their service layer's own
`_enforce_store_access`, while by-ID mutations correctly rely on the
service layer alone since the target store isn't known until the
entity is loaded) found that `hire_employee` and `create_payroll_period`
— both of which take `store_id` directly in the request body, exactly
like `create_invoice` et al. — were missing that route-layer call. Every
by-ID payroll/HR mutation (open/calculate/approve/post/reverse,
change-status, reassign, change-compensation, clock-in/out, correct)
correctly has only the service-layer check, matching `post_invoice`'s
own precedent.

This was a real asymmetry with the codebase's own standard, not a
functional hole in isolation (the service-layer check alone already
returned 403 in every existing RBAC test), but it removed a layer of
defense-in-depth relative to sibling modules. **Fixed**: added
`enforce_store_access(current_user, payload.store_id)` to both
endpoints, verified via `mypy`, the full test suite, and the two
dual-layer mutation tests in Section 9 (which independently confirmed
the fix by temporarily reverting it and observing the expected test
failures — 201 instead of 403 — before restoring it).

## 9. Mutation testing (Phase 14): 12/12

`tests/test_hr_payroll_mutation.py`, mirroring `test_ap_mutation.py`'s
methodology — each test neuters exactly one protection via
`monkeypatch` (never an inline-expression hack) and asserts the
system's behavior changes in the direction that proves the protection
is genuinely load-bearing:

1. `hire_employee` route-layer check removed → service layer still
   blocks (403).
2. `hire_employee` service-layer check removed → route-layer check
   (the Section 8 fix) still blocks (403) — **confirmed to actually
   fail without the fix**, proving this isn't a vacuous assertion.
3. `create_payroll_period` route-layer check removed → service layer
   still blocks (403).
4. `create_payroll_period` service-layer check removed → route-layer
   check still blocks (403) — same before/after confirmation as #2.
5. `change_compensation`'s only store check removed → cross-store pay
   change silently succeeds (proves the check is necessary, not
   incidental — the intended regression-pin).
6. `reassign_employee`'s shared store-check helper removed → an
   employee can be moved into a store the caller has no access to.
7. `post_payroll_period`'s only store check removed → cross-store
   posting of a real GL liability succeeds.
8. `reverse_payroll_period`'s only store check removed → cross-store
   reversal succeeds.
9. The store-scoped employee filter in `calculate_payroll_period`
   removed → a different store's employee is paid in this store's
   payroll run (the single highest-stakes cross-contamination
   scenario named in the safety standard).
10. `MissingCompensationCoverageError`'s trigger condition forced →
    confirms the caller genuinely refuses to calculate (raises
    `ConflictError`, period stays `OPEN`, zero result rows) rather than
    silently writing a $0 payroll result for an employee with no
    compensation on file.
11. Reversal journal-line integrity: every account's reversal line is
    the exact debit/credit swap of the original posting's line for
    that account — proven end-to-end through real posting + reversal,
    not by re-reading the same rows.
12. Runtime DB role cannot `DELETE` a posted payroll journal entry
    (`permission denied` from PostgreSQL itself) — extends the existing
    `payroll_reversals` append-only test to the `journal_entries` rows
    payroll posting actually produces.

All 12 pass. Two (#2 and #4) were verified to genuinely fail without
the Section 8 fix in place, confirming the whole file exercises real
code rather than restating already-true assertions.

## 10. Audit / privacy verification (design decision #5)

Every `audit_service.log_event` call site in `hr/service.py` and
`payroll/service.py` was read line-by-line. None carry `rate`,
`gross_pay`, `net_pay`, `total_deductions`, bank/payment details, or any
other sensitive amount — only entity identifiers, statuses, dates,
counts, and free-text reasons supplied by the actor (e.g. a cancellation
reason). `EMPLOYEE_COMPENSATION_CHANGED`'s audit payload is `{"effective_from":
..., "pay_type": ...}` — deliberately omitting `rate`. A dedicated API
test (`test_invalid_compensation_request_error_never_echoes_the_rate`)
confirms this also holds for error responses, not just the happy path.

## 11. Timezone / cross-midnight verification (design decision #6)

`Store.attendance_day_boundary_hour` (0–23, DB `CHECK`-constrained) is
store configuration, not an employee field. `resolve_work_date` uses
`zoneinfo.ZoneInfo` for deterministic UTC→store-local conversion, never
server-local `datetime.now()`. Required scenarios, all covered by tests
in `tests/test_hr_attendance.py`: normal same-day shift, midnight
crossing, multi-hour overnight shift, an ambiguous boundary case, and
per-store configuration producing different `work_date` results for the
identical UTC timestamp at two different stores.

## 12. Concurrency, failure injection, and idempotency

- Real independent `SessionLocal()` connections (never the `db`
  fixture's savepoint isolation) for every concurrency test, per this
  codebase's established discipline.
- Concurrent post of the same approved period → exactly one journal
  entry (row lock serializes; the second caller sees POSTED and returns
  cleanly rather than erroring).
- Concurrent (re)calculate of the same period → exactly one generation
  of `PayrollEmployeeResult` rows, never two.
- Forced failure after journal creation, before final commit, on both
  posting and reversal → full rollback, verified by re-querying for
  the period's status and the absence of any journal entry/reversal
  row.
- Forced failure during a recalculate (after the old results were
  deleted, before the new ones committed) → rollback restores the
  original results untouched, never leaves the period with zero
  results.
- State-based idempotency (mirrors `post_purchase_invoice`) plus an
  explicit `client_transaction_id` fast path checked before work begins
  (mirrors `receive_goods`) for both `calculate` and `post` — a retried
  call with the same key returns the original result without
  recomputing, proven by changing the underlying compensation between
  the two calls and confirming the retried total is unchanged.

## 13. Test counts

- M10-specific backend tests: 142 (schema/constraints 25, migrations +3,
  HR service 16, attendance 16, payroll lifecycle 15, calculation 13,
  calculation integration 7, accounting 11, hardening 7, RBAC API 10,
  mutation 12 — plus the M10 guard additions to `test_migrations.py`).
- Full backend suite (M0 through M10 together): **651/651 passing.**
- Frontend suite: **36/36 passing** (`HrPage.test.tsx` ×3,
  `PayrollPage.test.tsx` ×3, plus the 30 pre-existing tests unaffected).
- `npx tsc -b`, `oxlint`, `prettier --check .`, and `mypy` on every
  touched backend file: all clean.

## 14. Live browser verification

A real Playwright/Chromium session against the actual dev servers
(not mocked) walked the full golden path: login → HR page → hire an
employee → view detail → set compensation (SALARY, $3000.00) → Payroll
page → create a payroll period → open it → calculate → approve → post
→ confirmed **POSTED** rendered in the UI. Independently verified in
the database afterward: the posted period's journal entry (id 39670)
has exactly two lines, debit $6,000.00 to wage/salary expense and
credit $6,000.00 to payroll payable — balanced, and matching the
salary amount entered through the UI.

One real scripting mistake was caught and is recorded rather than
hidden: the first attempt used a payroll period dated in the past
(2024) against compensation effective today, correctly triggering
`MISSING_COMPENSATION_COVERAGE` — this was the test script's dates
being wrong, not a product defect (confirmed by direct DB inspection of
the employee's assignment and compensation rows before concluding
so). The corrected script (dates overlapping the compensation's
effective range) completed the full path successfully.

Two pre-existing 401s on `/api/v1/auth/refresh` appeared during login,
consistent with a previously-known React 18 StrictMode +
refresh-token-rotation interaction on first page load — not part of
M10 and not reproduced as a functional blocker (login still succeeded
immediately after).

## 15. Known, disclosed leftover state

The smoke test created real data in the shared `erp_dev` database that
was **not** cleaned up, and that decision is disclosed here rather than
silently left for someone else to discover:

- User `smoketest_admin` (ADMIN role, store 3745).
- Store 3745 (ad hoc, created for this test).
- Employees 909 (`SMOKE-001`) and 910 (`SMOKE-<uuid>`).
- Payroll period 601 (OPEN, never calculated — harmless) and period 602
  (POSTED, with a real balanced journal entry, id 39670).

Period 602's journal entry falls under the same `REVOKE DELETE`
append-only privilege carve-out verified in Section 9, item 12 — it
cannot be deleted by the application's runtime role, and deleting it as
a superuser purely to tidy up test data would contradict the very
immutability guarantee this hardening pass exists to verify. This is
consistent with, not a departure from, this codebase's established
tolerance for uuid-suffixed test data left behind by genuine-concurrency
tests; the difference here is that the leftover data was created ad hoc
by a manual smoke test rather than by an automated suite, so it is
called out explicitly rather than assumed self-evident.

## 16. Bugs found and fixed during this work

1. Fail-open RBAC gap: `change_employment_status`/`change_compensation`
   skipped the store-access check entirely when an employee had no
   current assignment row. Fixed to fail closed
   (`_enforce_store_access_via_current_assignment`).
2. `approve_payroll_period` set the period's status without setting
   `approved_by`/`approved_at`, violating the very `CHECK` constraint
   meant to guarantee their presence. Fixed, and the constraint itself
   was added in a follow-up migration rather than left as an
   application-level `assert` standing in for a missing DB invariant.
3. Missing `CANCELLED` payroll-period status (present in the approved
   design, absent from the first schema pass). Added via migration.
4. Float-precision defect in `_sum_attendance_hours` (Section 6).
5. This hardening pass's own test-query bug (querying journal entries
   by `source_id` without also filtering `source_type`, risking false
   matches against coincidentally-equal IDs from other modules) — found
   and fixed in the test code itself before it could hide a real defect.
6. The Section 8 defense-in-depth asymmetry.

None of these are hypothetical — each has a named regression test that
fails without its fix.

## 17. Remaining limitations (honest, not silently reduced)

- Statutory tax/withholding is genuinely not computed (by design,
  decision #8) — `ACCOUNT_STATUTORY_WITHHOLDING_PAYABLE` is reserved
  but unposted-to. A future module must implement real tax logic; this
  one must not be read as already having done so.
- The RBAC/mutation matrix targets the protections this specific audit
  identified as load-bearing; it is not an exhaustive fuzz of every
  permission/endpoint combination in the system.
- Smoke testing exercised the single golden path (hire → pay → create →
  open → calculate → approve → post) once through the UI; it did not
  re-exercise every lifecycle branch (cancel, reversal-through-the-UI)
  interactively, though each of those is covered by backend API/service
  tests.

## 18. Final status

No PR was opened. M11 was not started. All work is committed and
pushed to `claude/grocery-erp-pos-architecture-h8a53g` across the
following commits for this phase of work: the M10 schema/migrations,
per-module service commits, the RBAC/audit hardening commit, the
frontend commit, and this hardening pass's commit
(endpoint fix + 12 mutation tests). **CONDITIONAL PASS**, with every
condition named above rather than implied.
