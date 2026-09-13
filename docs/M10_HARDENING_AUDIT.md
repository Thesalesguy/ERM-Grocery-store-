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

## 19. Closure gate (post-milestone re-verification)

A separate, later pass whose sole purpose was to determine whether the
conditions above are genuinely closeable — not to redesign, expand, or
re-implement any part of M10. Nothing in the application code changed;
the two changes made were a legitimate database cleanup (below) and
this document.

**Independently re-confirmed, not assumed from Section 1-18 above:**
backend 651/651, frontend 36/36, migration head `1e832b76969e` (single
head), `main` and the feature branch both at the same commit, working
tree clean.

### 19.1 Correction to Section 15's record

Investigating the disclosed `erp_dev` artifacts with fresh queries
(rather than trusting the prior prose) found the prior report
overstated M10's own footprint. Store 3745 ("Smoke Test Store") and a
user on it (`smoke_admin`) were created **two days before** the M10
smoke test — confirmed by `created_at` timestamps — and the store
already carried unrelated M4/M5-era POS data (one product, one sale,
one sale return, two inventory movements) before M10 ever touched it.
M10's Phase 12 smoke test reused this pre-existing store rather than
creating it. The prior report's "ad hoc, created for this test" framing
was wrong; corrected here.

What M10's own smoke test actually added to that store: user
`smoketest_admin` (id 25869), employees 909/910, and payroll periods
601/602. Of those:

- **Removed** (this pass): payroll period 601 — status OPEN, never
  calculated, `journal_entry_id` NULL, zero downstream references.
  `erp_app` (the application's own runtime role) holds ordinary,
  unrevoked DELETE on `payroll_periods` — this table was never part of
  the append-only carve-out — so deleting a row with zero financial
  substance through that same role is the application's own normal
  access, not a bypass of anything. Deleted; full regression suite
  re-run immediately after (651/651) to confirm no collateral damage.
- **Cannot be removed** (must remain, and here is the specific chain
  proving it, not just an assertion): payroll period 602 (POSTED) is
  linked to journal entry 39670, which carries the M1-wide
  `REVOKE UPDATE, DELETE` for `erp_app` — this is categorically off
  limits. Employees 909 and 910 are both referenced by
  `payroll_employee_results` rows under period 602 (both were paid
  $3,000.00 in that run — a real multi-employee scenario already
  existed in this data, not only the one built fresh in 19.2). User
  25869 is named in period 602's `calculated_by`/`approved_by`/
  `posted_by` columns, in 24 `audit_logs` rows, and as `created_by` on
  3 `journal_entries` rows — `audit_logs` is itself append-only, so
  even setting those columns aside, the audit trail alone anchors this
  user permanently. Store 3745 cannot be removed because the above
  cannot be removed and reference it.

Net correction: the smoke-test data that must remain is smaller and
more precisely identified than the prior report stated (one period
fewer), and the reason it must remain is now a traced FK/privilege
chain, not a general assertion.

### 19.2 Section 4/5/6 targeted re-audit: a fresh multi-employee, multi-deduction scenario

Built and ran an independent scenario the prior report never exercised:
one hourly employee (45 recorded hours via the real `clock_in`/
`clock_out` service calls) and one salaried employee, both store-scoped
to a newly created store, with a real 5%-of-gross employee deduction
configured through `DeductionType`/`DeductionRate` (not a plain
no-deduction salary run). Calculated, approved, and posted through the
real service layer. Results:

- Gross $3,500.00 (900 + 2,600), deductions $175.00, net $3,325.00 —
  journal entry with three lines (wage expense debit 3,500; net-pay
  payable credit 3,325; deduction payable credit 175) balances exactly.
- Re-posting the already-POSTED period returns the same journal entry
  (`41879`) — no second entry created; the DB's own
  `uq_journal_entries_source` unique constraint independently rejected
  a raw duplicate-insert probe with the same `(source_type, source_id)`.
- Reversing, then reversing again: the second call is a documented
  idempotent no-op (`reverse_payroll_period`'s own docstring: "a period
  already reversed returns the current row without creating a second
  reversal") — confirmed exactly one `payroll_reversals` row and one
  `PAYROLL_REVERSAL` journal entry exist after both calls; a raw
  duplicate-insert probe against `payroll_reversals` was independently
  rejected by `uq_payroll_reversals_period`. (This pass's own test
  script initially mislabeled this idempotent return as "a defect" in
  its output — flagged here for transparency: it wasn't one, once the
  actual row counts were checked rather than trusting the first
  print statement.)
- The reversal's journal lines are the exact account-for-account
  debit/credit swap of the original; the original journal's lines and
  the period's stored totals are byte-for-byte unchanged after
  reversal; `PayrollPeriod.status` stayed `POSTED` throughout, per
  design.
- Adversarial mutation attempts as `erp_app` against `journal_entries`,
  `journal_lines`, and `payroll_reversals` (UPDATE and DELETE, six
  attempts) were all rejected with PostgreSQL `permission denied` —
  true DB-level immutability, not application convention.
- **Precision correction to Section 9 item 12's implied scope**:
  `payroll_periods` and `payroll_employee_results` are **not**
  DB-privilege-restricted for `erp_app` — DELETE and UPDATE are both
  ordinarily granted. A raw SQL probe confirmed a self-consistent
  three-column tamper (`gross_pay`/`total_deductions`/`net_pay` set
  together to a false-but-internally-consistent triple) is accepted by
  PostgreSQL on `payroll_employee_results` — the
  `ck_payroll_employee_results_net_math` CHECK constraint only catches
  an inconsistent partial edit, not a deliberate consistent one, and
  there is no DB-level protection at all on `payroll_periods`'s own
  columns (also confirmed by probe, then rolled back). Immutability for
  these two tables is real but is an **application-layer** guarantee
  only: `calculate`/`open`/`approve`/`cancel` against a POSTED period
  were each independently confirmed rejected with `ConflictError` at
  the service layer AND with HTTP 409 `INVALID_PERIOD_STATE` at the
  real API layer (`TestClient`, real login, real bearer token — not a
  direct service call). Only `journal_entries`/`journal_lines`
  (M1-wide) and `payroll_reversals` (M10) carry the stronger,
  bypass-proof DB-privilege guarantee. This distinction was implicit
  in the prior report; it is now explicit.
- Fresh RBAC/mutation re-run (all 22 of the RBAC-API and mutation test
  files' tests, not assumed from the full-suite count): all pass.
- Fresh audit/log leak check against this brand-new scenario's own
  `audit_logs` rows (not re-reading the code, reading the actual rows
  this run produced): `PAYROLL_PERIOD_CALCULATED`/`_APPROVED`/`_POSTED`/
  `_REVERSED` and `EMPLOYEE_HIRED`/`EMPLOYEE_COMPENSATION_CHANGED`
  entries all carry only counts, IDs, dates, and reasons — a regex scan
  for every dollar figure produced by this scenario ($900, $2,600,
  $3,500, $3,325, $855, $2,470, $175, $130, $45) found zero matches
  across all audit rows this run created.

### 19.3 Migration re-verification

`tests/test_migrations.py`'s ten tests were re-run against the
project's own isolated `erp_test` scratch database (not `erp_dev`) and
all pass, including one this pass specifically checked rather than
assumed: `test_m0_m8_data_integrity_survives_m9_upgrade_downgrade_reupgrade`
calls `command.upgrade(cfg, "head")`, which — because it targets
Alembic's dynamic `"head"` rather than a pinned M9 revision constant —
now resolves to the *current* M10 head. That test therefore already
proves real M0-M8 business data (a store, product, purchase order,
inter-store transfer) survives byte-for-byte through an upgrade to
M10's head, a downgrade back to M8, and a re-upgrade to M10's head
again, without anyone having updated it for M10. Confirmed by running
it, not by reading its name. Separately, the three M10-specific
downgrade-guard tests confirm: a downgrade is refused while any
employee row exists, refused while any non-DRAFT (including CANCELLED)
payroll period exists, and succeeds cleanly when only a DRAFT period
exists — and in every case the guard (`RAISE EXCEPTION` in a `DO $$`
block) executes before any `op.drop_table` call, confirmed by reading
the migration's own source order.

### 19.4 A second, self-inflicted contamination incident — found and fixed during this pass, disclosed in full

While building the scenario in 19.2, the deduction rate was created
with `effective_from = 2024-01-01` and an **open-ended** `effective_to`.
`DeductionRate` is global configuration by design (M10 decision area,
mirroring `OvertimePolicy`'s own global scope) — it is not scoped to a
store or a test. Committing it directly against the shared `erp_dev`
database (outside any test's transactional rollback) made it live for
every payroll calculation across every store from that date forward.

Re-running the full backend suite immediately after building the 19.2
scenario surfaced two failures
(`test_calculate_hourly_employee_happy_path`,
`test_multi_employee_period_with_deductions_and_employer_contributions_balances`)
with net-pay figures off by exactly the 5% deduction — the contamination,
not a product defect. Root-caused immediately rather than dismissed as
flaky.

Worse: two other tests that use **real, uncommitted-rollback**
`SessionLocal()` connections by design
(`test_concurrent_post_of_the_same_period_creates_exactly_one_journal_entry`
and `test_concurrent_calculate_of_the_same_period_never_leaves_duplicate_results`
in `tests/test_payroll_hardening.py`, both dated 2024-01-01..01-15 —
inside the leaked window) had already picked up the same phantom 5%
deduction in their own real, committed data during that same run. They
did not fail, because their assertions check gross pay and
row/entry counts, not net pay — but their underlying data was, for a
window of time, genuinely wrong relative to what those tests intend to
demonstrate.

**Fixed, in the only way available for each, by severity:**

- The deduction rate's window was tightened to
  `2024-03-01..2024-03-14` — exactly and only the 19.2 scenario's own
  period. This does not retroactively change 19.2's own already-posted
  result (which is a stored historical fact, independent of the rate's
  current bounds); it only stops the rate from affecting anything else
  going forward. Confirmed: the full backend suite passes 651/651
  again immediately after.
- The concurrency test's **CALCULATED** (not yet posted) period was
  still legitimately correctable: recalculating it through the
  ordinary `calculate_payroll_period` service call (the same
  application mechanism any real recalculation would use, not a raw
  UPDATE) — with the contaminating rate now out of range — restored its
  deductions to the correct $0.00.
- The concurrency test's **POSTED** period (id 977, store 47129,
  journal entry 42237) could not be corrected the same way: it is
  POSTED, so `calculate_payroll_period` correctly refuses to touch it,
  and its journal entry carries the same M1-wide append-only privilege
  restriction as every other posted journal. This was **not**
  bypassed. The entry is confirmed still balanced ($2,000.00 debit /
  $1,900.00 + $100.00 credit) and self-consistent — the $100 deduction
  it carries is not arithmetically wrong, only unintended by that
  test's original scenario. It is disclosed here as a second, new,
  isolated artifact that must remain in `erp_dev`, for the identical
  structural reason as every artifact in 19.1: its journal entry is
  genuinely immutable once posted, and that immutability is exactly
  the property this milestone exists to guarantee — it does not get a
  carve-out because this pass happens to be the one that caused it.

This incident is reported as what it is: a real mistake made during
this closure pass, root-caused rather than glossed over, fixed to the
full extent fixing was legitimately possible, and disclosed rather than
quietly cleaned up where it wasn't. It also stands as a concrete,
now-demonstrated (not merely theoretical) illustration of the
operational risk in `DeductionRate`/`OvertimePolicy`'s global scope:
any future change to global payroll configuration — by an operator, a
script, or a future milestone — can silently affect every store's
payroll calculations for its effective range unless its dates are
deliberately bounded. Worth carrying into M11 as an operational note;
not a reason to reopen M10's own scope decision, which was explicit and
approved.

### 19.5 Closure verdict

**PASS WITH CONDITIONS.** Every condition named in Sections 1-18 was
re-verified against fresh evidence rather than assumed, one prior
disclosure was corrected to be more precise (19.1), a real regression
was found, root-caused, and fixed to the extent legitimately possible
during this same pass (19.4), and no accounting-immutability safeguard
was weakened or bypassed at any point to make any of this easier. The
verdict is "with conditions" rather than an unqualified PASS because
two categories of state genuinely cannot be closed out, both for the
same structural reason (a posted journal entry is genuinely immutable):
the original M10 smoke-test artifacts (19.1, reduced by one period from
the prior report) and the two artifacts this closure pass itself added
(19.2's own posted scenario, and 977/42237 from 19.4's incident). Both
are proven isolated to randomly-suffixed, non-production test stores,
mathematically balanced, and carrying no other system's data. Neither
is a reason to withhold M10 sign-off, and neither requires resolution
before M11 — resolving them would require weakening the exact
protection M10 was built to provide.
