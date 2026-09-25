# M21 Testing Sessions — Cross-Store Isolation Remediation

Companion to `docs/M21_DISCOVERY.md` (the discovery-only audit that found
these gaps) and this milestone's implementation. Scope, per the M21
implementation brief: remediate Findings F1, F5, and F6 only. F7 and F8
are explicitly out of scope and remain unresolved (see "F7/F8 status"
below) — nothing in this milestone touches them.

## Baseline (Phase 0)

- Starting HEAD: `093423b` (M21 discovery commit).
- Working tree: clean before any code change.
- Endpoints/services located for each finding:
  - **F1**: `app/api/v1/endpoints/hr.py::get_employment_status_history/
    get_employment_assignment_history/get_compensation_history` →
    `app/modules/hr/service.py::employment_status_history/
    employment_assignment_history/compensation_history`.
  - **F5**: `app/api/v1/endpoints/ap.py::get_supplier_summary/
    get_supplier_transactions/get_supplier_statement` →
    `app/modules/ap/service.py::get_supplier_ap_summary/
    get_supplier_transaction_history/get_supplier_statement`.
  - **F6**: `app/api/v1/endpoints/ap.py::get_matching_status/
    get_matching_status_multi` →
    `app/modules/ap/service.py::get_invoice_matching_status/
    get_invoice_matching_status_multi`.
- Established isolation patterns identified and reused (no new
  authorization model invented):
  - HR: `hr/service.py::_enforce_store_access_via_current_assignment` —
    already existed, already used by `change_employment_status`/
    `reassign_employee`/`change_compensation`/`clock_in`/`clock_out`/
    `correct_attendance`. Resolves the employee's authoritative store via
    `get_current_assignment` (there is no `Employee.store_id` column —
    store membership is effective-dated, so the *current* assignment row
    is the only correct source of truth). F1's three read functions were
    simply never wired to call it.
  - AP direct-by-ID reads: `purchasing.py::_get_po_with_store_check` /
    `ap.py::get_invoice` — 404-not-403 on a cross-store direct-ID read
    (never confirm existence elsewhere). Reused verbatim for F6.
  - AP store-scoped list/aggregate reads: `scoped_store_filter` (already
    used by `list_invoices`/`list_payments`/`list_credit_notes` in the
    same file) — a store-scoped caller's omitted filter silently narrows
    to their own store; an unrestricted caller's omitted filter stays
    unfiltered. Reused for F5's three aggregate functions, since `Supplier`
    itself is legitimately company-wide reference data (a documented M3/M19
    decision) but its invoices/payments/credit-notes are store-scoped
    operational rows everywhere else in the same file.
- Existing tests covering these paths before this milestone: none exercised
  cross-store access for any of the six vulnerable functions (confirmed by
  grep for the function/endpoint names combined with isolation vocabulary
  in `docs/M21_DISCOVERY.md`'s own Finding sections — zero matches). This
  is exactly why the gaps existed undetected through 20 prior milestones.
- Pre-existing test suites run before any change (all green, confirming a
  clean baseline to change against): `test_hr_service.py`,
  `test_hr_attendance.py`, `test_hr_payroll_rbac_api.py`,
  `test_hr_payroll_mutation.py`, `test_ap_invoices.py`, `test_ap_payments.py`,
  `test_ap_reversal.py`, `test_ap_reconciliation.py`,
  `test_ap_e2e_scenario.py`, `test_ap_api.py`, `test_ap_concurrency.py`,
  `test_ap_failure_injection.py`, `test_ap_mutation.py`.

## Implementation summary

- **F1**: added a required `caller_store_id: int | None` keyword parameter
  to all three HR history functions; each now calls the existing
  `_enforce_store_access_via_current_assignment` before querying. The three
  `hr.py` endpoints now pass `caller_store_id=current_user.store_id`. No
  new authorization model, no change to compensation calculation,
  employment-status semantics, or payroll behavior — these are pure
  read-path additions.
- **F6**: added a required `caller_store_id: int | None` keyword parameter
  to both matching-status functions; `get_invoice_matching_status` now
  checks the already-fetched `PurchaseOrder.store_id` against it and raises
  `NotFoundError` (mirroring `get_invoice`/`_get_po_with_store_check`'s
  404-not-403 convention) on mismatch. `get_invoice_matching_status_multi`
  threads the same value into each single-PO call in its loop, so a
  comma-separated list containing even one foreign-store PO id fails the
  whole request (fail closed, not silent partial results).
- **F5**: added an optional `store_id: int | None = None` keyword parameter
  to all three supplier-financial functions, applied identically to every
  underlying query (`PurchaseInvoice`, `PurchaseOrder`, `SupplierPayment`,
  `SupplierCreditNote`, and the two reversal-join queries in
  `get_supplier_statement`). This is a *filter*, not a deny, because
  `Supplier` is legitimately company-wide reference data — a store-scoped
  caller's summary/transactions/statement now reflects only their own
  store's rows for that supplier, while an unrestricted caller still sees
  the true company-wide total. The three `ap.py` endpoints compute
  `scoped_store_filter(current_user, None)` (no `store_id` query parameter
  was added to these endpoints — this silently narrows an already-scoped
  caller exactly the way `list_invoices` already does for its own omitted
  filter) and pass the result through. No accounting/journal logic,
  invoice/payment/credit-note amounts, or matching semantics were touched.

## Phase 3 — Adjacent-bypass sweep

- **HR**: `get_current_assignment`/`get_current_status`/
  `get_current_compensation` (the raw per-dimension lookups) are called
  only from `get_employee_detail` (which already has its own store check)
  and from the six mutation functions (all already store-checked). No
  other endpoint or function exposes `EmploymentStatusPeriod`/
  `EmploymentAssignment`/`CompensationPeriod` rows by employee id. Clean —
  no adjacent bypass found.
- **AP**: grepped every `supplier_id ==`/`.supplier_id ==` query site in
  `ap/service.py`. The three list functions (`list_purchase_invoices`,
  the payment-list function, the credit-note-list function) that also
  filter by `supplier_id` were already correctly and independently
  store-scoped (they take both `store_id` and `supplier_id` as
  simultaneous, ANDed filters, with the API layer always forcing
  `store_id` via `scoped_store_filter` for a store-scoped caller) — not a
  bypass of the F5 fix, a pre-existing correct pattern. `ap_aging` was
  already correctly `store_id`/`store_ids`-scoped (unaffected by this
  milestone). No other function reads `PurchaseInvoice`/`SupplierPayment`/
  `SupplierCreditNote` by `supplier_id` without a store filter. Clean —
  no adjacent bypass found.

## Testing Session A — Positive access

Every remediated endpoint has a same-store (and, separately, unrestricted-
caller) test proving legitimate access is unchanged:
`test_f1_same_store_manager_reads_status_history`,
`test_f1_same_store_manager_reads_assignment_history`,
`test_f1_same_store_manager_reads_compensation_history`,
`test_f1_unrestricted_caller_reads_history_for_any_store`,
`test_f5_same_store_manager_sees_own_store_supplier_summary`,
`test_f5_unrestricted_caller_sees_company_wide_total`,
`test_f6_same_store_manager_reads_own_stores_matching_status`. All
assert real content (status transitions, a specific compensation rate, a
specific dollar total, a specific received quantity), not just a 200
status code.

## Testing Session B — Cross-store denial

Every remediated endpoint has an explicit-attempt cross-store test,
including the exact reproduction scenario named in each Finding:
`test_f1_store_scoped_manager_cannot_read_another_stores_status_history`,
`..._assignment_history`, and
`test_f1_store_scoped_manager_cannot_read_another_stores_compensation_history`
(the highest-severity case — asserts the response body never contains the
leaked rate figure, not just the status code);
`test_f5_store_scoped_manager_summary_excludes_another_stores_invoices`,
`..._transactions_excludes_another_store`,
`..._statement_excludes_another_store` (F5 is a filter, so these assert
the *content* excludes the foreign store's data, not a denial — the
correct proof for a filter-style fix, per the F5 remediation shape);
`test_f6_store_scoped_manager_cannot_read_another_stores_matching_status`
and `test_f6_store_scoped_manager_cannot_smuggle_another_stores_po_into_multi`
(the multi-PO variant — proves the whole request fails, not a silent
partial drop, when the id list mixes stores). All are real HTTP requests
via `TestClient`, not direct service calls.

## Testing Session C — Service-layer bypass (mandatory)

Each finding has a direct-service-call test proving the guard exists below
the HTTP layer: `test_f1_service_layer_rejects_cross_store_status_history_without_http`,
`..._assignment_history_without_http`,
`..._compensation_history_without_http` (all assert `ForbiddenError` is
raised by calling `hr_service.*` directly, no `TestClient`/route
involved); `test_f5_service_layer_summary_scoping_cannot_be_bypassed_without_http`
(calls `ap_service.get_supplier_ap_summary` directly with an explicit
`store_id`, proving the filter is enforced in the function itself);
`test_f6_service_layer_rejects_cross_store_matching_status_without_http`
(asserts `NotFoundError` from a direct `ap_service.get_invoice_matching_status`
call). Complementary same-store/unrestricted service-layer tests
(`test_f1_service_layer_allows_same_store_and_unrestricted`,
`test_f6_service_layer_allows_same_store_and_unrestricted`) prove the
guard doesn't over-block.

## Testing Session D — Authorization matrix

Tested combinations, using only roles/permissions that already exist (no
invented role semantics): Manager same-store (Session A), Manager
cross-store (Session B), unrestricted Admin (`store_id=None`, Session A),
Cashier lacking the read permission entirely
(`test_f1_cashier_lacks_hr_read_permission_entirely`,
`test_f5_cashier_lacks_ap_read_permission`,
`test_f6_cashier_lacks_ap_read_permission`), Auditor read-only access
(`test_f5_auditor_can_read_but_not_write`), and nonexistent-record
behavior for both a store-scoped caller and an unrestricted caller
(`test_f1_nonexistent_employee_store_scoped_caller_gets_403_not_a_data_leak`,
`test_f1_nonexistent_employee_unrestricted_caller_gets_empty_list`,
`test_f6_nonexistent_purchase_order_is_404_for_everyone`). The
store-scoped-caller-plus-nonexistent-employee case documents genuinely
pre-existing behavior (the fail-closed default of
`_enforce_store_access_via_current_assignment` when no current assignment
exists to verify against) rather than new M21 semantics — this is the
same helper already used by `change_employment_status` et al., unchanged
by this milestone.

## Testing Session E — Concurrency / data integrity

**Not applicable, deliberately.** This milestone's fix is a pure read-side
authorization addition: a filter/deny check on existing read queries, with
no new locking, no new mutation path, and no change to any write
transaction. There is no meaningful concurrency concern to test — two
concurrent reads of the same (now-scoped) query cannot race with each
other in any way the fix introduces. Per the implementation brief's own
instruction ("Add concurrency tests only where the implementation
introduces a meaningful concurrency concern... Do not add artificial
concurrency tests merely to increase test counts"), no concurrency test
was added. Data-integrity verification instead took the form of: (a) the
full pre-existing HR/AP/accounting/payroll test suites re-run green after
the change (no accounting row, AP balance, or payroll value was altered —
see Sessions G/H below), and (b) a direct diff review confirming the six
changed functions perform no writes.

## Testing Session F — Mutation testing (mandatory)

Methodology identical to every prior milestone's mutation-testing
discipline (M19/M20): each target broken live via `Edit` against the real
source file, confirmed to fail the *specific* test(s) for the *stated*
reason, reverted via `cp` from a pre-mutation backup, and the revert
diff-verified byte-identical before moving to the next target. All 6
required mutations from the implementation brief were performed and every
one was caught; no test required strengthening (unlike M19/M20's own
mutation passes, which did surface genuine test weaknesses in their
respective domains — none were found here).

| # | Target | Mutation | Test(s) that caught it | Result |
|---|---|---|---|---|
| 1 | Remove the HR store-scope check | `hr/service.py::compensation_history` — deleted the `_enforce_store_access_via_current_assignment(...)` call | `test_f1_store_scoped_manager_cannot_read_another_stores_compensation_history`, `test_f1_service_layer_rejects_cross_store_compensation_history_without_http` | Caught — both failed with "DID NOT RAISE"/200-instead-of-403 |
| 2 | Remove the AP supplier store-scope check | `ap/service.py::get_supplier_ap_summary` — deleted the `if store_id is not None: invoice_query = ...` filter application | `test_f5_store_scoped_manager_summary_excludes_another_stores_invoices`, `test_f5_service_layer_summary_scoping_cannot_be_bypassed_without_http` | Caught — both stores' totals leaked ($600 instead of $100) |
| 3 | Remove the AP invoice-matching store-scope check | `ap/service.py::get_invoice_matching_status` — deleted the `if caller_store_id is not None and purchase_order.store_id != caller_store_id: raise NotFoundError(...)` block | `test_f6_store_scoped_manager_cannot_read_another_stores_matching_status`, `test_f6_service_layer_rejects_cross_store_matching_status_without_http`, `test_f6_store_scoped_manager_cannot_smuggle_another_stores_po_into_multi` | Caught — all 3 failed |
| 4 | Remove the API-layer authorization pass-through | `hr.py::get_compensation_history` — hardcoded `caller_store_id=None` instead of `current_user.store_id` | `test_f1_store_scoped_manager_cannot_read_another_stores_compensation_history` | Caught — 200 instead of 403, proving the route's correct threading of the real caller store is itself load-bearing, not just the service-layer guard's existence |
| 5 | Alter the store comparison so another store can pass | `ap/service.py::get_invoice_matching_status` — flipped `!=` to `==` | `test_f6_same_store_manager_reads_own_stores_matching_status` (now fails — same-store blocked), `test_f6_store_scoped_manager_cannot_read_another_stores_matching_status` (now fails — cross-store allowed), `test_f6_service_layer_rejects_cross_store_matching_status_without_http`, `test_f6_service_layer_allows_same_store_and_unrestricted` | Caught — 4 of 7 F6 tests failed, including both directions of the flipped logic |
| 6 | Remove the relevant query predicate | `ap/service.py::get_supplier_transaction_history` — deleted only the `invoice_query.where(PurchaseInvoice.store_id == store_id)` line (payment/credit-note predicates left intact, to isolate this specific predicate as the target) | `test_f5_store_scoped_manager_transactions_excludes_another_store` | Caught — the foreign store's invoice transaction leaked into the list |

No mutation was judged inapplicable; all 6 required targets from the
brief map directly onto real code in this milestone's diff.

## Testing Session G — Financial regression

`get_supplier_statement`'s own docstring claims `closing_balance` exactly
equals `get_supplier_ap_summary(...).total_owed` when unfiltered — this
invariant already existed and was already tested pre-M21. Because F5's
fix applies a *store filter* to both functions, this milestone adds
`test_f5_statement_closing_balance_matches_scoped_summary_total_owed`,
proving the identity now also holds when `store_id` is explicitly set (not
just when both are unfiltered) — i.e., the fix did not silently break an
existing, tested financial invariant by scoping the two functions
independently or inconsistently. Beyond this dedicated test: the full
pre-existing `test_ap_*.py` suites (8 files, invoices/payments/reversal/
reconciliation/e2e/api/concurrency/failure-injection/mutation) were
re-run in full after the F5/F6 changes and are green — no supplier
balance, AP aging figure, invoice amount, payment record, credit-note
behavior, or accounting journal entry changed. `get_supplier_ap_summary`/
`get_supplier_transaction_history`/`get_supplier_statement`'s existing
call sites (which never pass `store_id`) are unaffected, since the new
parameter defaults to `None` (unfiltered), matching prior behavior
exactly.

## Testing Session H — HR/payroll regression

The full pre-existing `test_hr_service.py`, `test_hr_attendance.py`,
`test_hr_payroll_rbac_api.py`, and `test_hr_payroll_mutation.py` suites
were re-run in full after the F1 change and are green — no employee
record, compensation value, employment-status transition, employment
assignment, payroll calculation, or payroll posting changed. The five
pre-existing call sites of the three modified HR functions
(`test_hr_service.py`) were updated to pass `caller_store_id=None`
(preserving their prior unfiltered/unrestricted-context behavior exactly,
since none of them were testing isolation) — this is the only change made
to a pre-existing test, and it is a required signature update, not a
weakened assertion.

## Regression and quality gates (Phase 13)

- Targeted HR tests: 54 passed (`test_hr_service.py`, `test_hr_attendance.py`,
  `test_hr_payroll_rbac_api.py`, `test_hr_payroll_mutation.py`).
- Targeted AP tests: 84 passed (8 `test_ap_*.py` files).
- New M21 isolation test file: 30 passed
  (`test_m21_cross_store_isolation.py`).
- Full backend suite: **1,040 passed** (1,010 pre-M21 + 30 new; zero
  regressions, zero skips, zero xfails).
- Frontend suite: 55 passed (15 files) — unaffected, response schemas for
  the six remediated endpoints are unchanged; re-run as a precaution since
  `frontend/src/api/hr.ts`/`ap.ts` reference these endpoint paths.
- Lint (`ruff check app tests`): clean.
- Formatting (`black --check app tests`): clean.
- Type check (`mypy app`): `Success: no issues found in 102 source files`.
- Deploy/infra tests: none reference any of the six remediated endpoints
  (confirmed by grep) — not re-run as part of this milestone's own gate,
  left to the standard CI `deploy-infra`/`deploy-infra-native` jobs.

## Phase 14 — Adversarial self-audit

Answered directly against the final diff, before commit:

1. Can a Store A user still retrieve Store B employee status history by
   changing an id? **No** — proven by
   `test_f1_store_scoped_manager_cannot_read_another_stores_status_history`
   and the service-layer bypass test.
2. Compensation history by id? **No** — proven, including a
   response-body check that the leaked rate figure never appears.
3. Store B supplier transactions by id? **No** — the itemized transaction
   list is filtered; proven with a specific transaction id assertion.
4. Store B supplier statements? **No** — `closing_balance` reflects only
   the caller's store; proven numerically.
5. Store B AP invoice-matching information? **No**, including via the
   multi-PO comma-separated list smuggling attempt.
6. Can any HTTP endpoint bypass the service-layer guard? **No** — the
   route layer's only role is threading `current_user.store_id`/
   `scoped_store_filter(...)` through; the actual enforcement lives in the
   service functions (proven by mutation 4, which mutated the route layer
   alone and was still caught by an HTTP-level test observing the wrong
   status code).
7. Can a direct service invocation bypass the intended store boundary?
   **No** — proven by the three Session C tests, which call the service
   functions with no `TestClient`/HTTP involved at all.
8. Is there another sibling endpoint exposing the same sensitive data
   without the guard? **No**, per the Phase 3 adjacent-bypass sweep above.
9. Did any accounting value change? **No** — full AP/accounting suites
   green, no journal/posting code touched.
10. Did any HR/payroll value change? **No** — full HR/payroll suites
    green, no calculation/posting code touched.
11. Did any migration become necessary? **No** — see Phase 12 below.
12. Did any existing legitimate cross-store administrative workflow
    accidentally break? **No** — every unrestricted-caller
    (`current_user.store_id is None`) test passes unchanged, proving
    Admin/cross-store-authorized access is fully preserved.

## Phase 12 — Migration safety

**No migration was introduced, and none was necessary.** This milestone
adds only application-layer authorization logic (new function parameters
and query predicates against existing, already-store-scoped columns —
`PurchaseInvoice.store_id`, `PurchaseOrder.store_id`, `SupplierPayment.store_id`,
`SupplierCreditNote.store_id` all already existed). No schema change, no
new column, no new table. `alembic heads` remains a single line
(`9c4c5a209aa9`), unchanged from M20.

## Limitations and intentionally unresolved items

- **F7 status: UNRESOLVED, unchanged.** Per the implementation brief,
  `transfers/service.py::create_transfer`'s one-side-authorization model
  (a caller belonging to either the source or destination store may draft
  a transfer naming the other) was neither confirmed as a defect nor
  fixed. The existing repository evidence (`replenishment/service.py`'s
  `_enforce_plan_access` explicitly documents mirroring this exact
  symmetry, in a module written after `transfers` and reviewed during
  M9's own hardening pass) shows the behavior is *consistently applied*
  across two independent modules, but does not establish which of the two
  plausible interpretations (an intentional "pull request" style ask, or
  a gap both modules independently share) is correct. Per the brief's
  explicit instruction ("do not change it unless the repository's
  existing authorization contract provides sufficient evidence for the
  correct behavior"), this remains an open business-policy question for
  the system owner, not resolved by this milestone.
- **F8 status: OUT OF SCOPE, unchanged.** The Reports frontend
  store-selector gap is explicitly excluded from M21 by the implementation
  brief. Nothing in this milestone touches `ReportsPage.tsx` or any report
  endpoint.
- No new report/UI work, no accounting-period changes, no logistics
  changes, no vendor-management UX changes were made — all correctly
  deferred to the milestone roadmap in `docs/M21_DISCOVERY.md` Section 16,
  none of which is this milestone's scope.
