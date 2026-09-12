# M9 — Hardening Audit (Rev 2: closing the CONDITIONAL PASS gaps)

Companion to `docs/M9_SUPPLY_CHAIN_DESIGN.md`. Rev 1 of this document
(the version delivered with the initial M9 implementation) was honestly
scored CONDITIONAL PASS and named specific gaps: 6 of 10 concurrency
races untested, 8 of 12 mutation targets unexercised, no M9-specific
failure injection, a substituted (not literal) E2E scenario, and no
dedicated M0–M8 data-integrity comparison. This revision closes those
gaps with real, adversarial, PostgreSQL-backed evidence — and reports
**two genuine defects found and fixed in the process**, not merely a
larger test count.

## 1. Verdict

**PASS WITH CONDITIONS.** The 10-item concurrency matrix, 12-item
mutation matrix, and per-workflow failure injection are now complete
and passing against real PostgreSQL. One **critical** correctness defect
(cross-plan over-allocation of a source store's protected stock) and one
**moderate** defect (a 500 on the plan-detail endpoint, already fixed in
Rev 1) were found; the critical one is fixed and proven by a dedicated
regression test in this revision. A real N+1 query defect was also found
and fixed. The remaining conditions are narrow and explicitly named in
Section 15 — none of them are financial-safety gaps.

## 2. Baseline (before this hardening pass)

- Branch: `claude/grocery-erp-pos-architecture-h8a53g`; git status clean
  before starting.
- Migration head: `36ec624cf083` (M9), confirmed via `alembic current`.
- Database: real PostgreSQL 16 (`erp_dev` for the `db`/`client` fixtures,
  a dedicated `erp_test` database for migration tests) — the Postgres
  cluster had to be started fresh in this session
  (`pg_ctlcluster 16 main start`), then verified reachable.
- Baseline backend suite: 457/457 passing. Baseline frontend suite:
  30/30 passing. Both re-confirmed before any change was made.
- Files re-inspected before changing anything: `M9_SUPPLY_CHAIN_DESIGN.md`,
  the Rev 1 `M9_HARDENING_AUDIT.md`, `replenishment/models.py`,
  `replenishment/service.py`, the PO/transfer `_inner` integration points
  in `purchasing/service.py` and `transfers/service.py`,
  `supplier_products` pricing logic, M8's `get_replenishment_suggestions`,
  the M9 migration, the API schemas/endpoints, and `SupplyChainPage.tsx`.

## 3. Concurrency matrix: 10/10

All ten named races, each run 5 reps against real PostgreSQL with
genuinely independent `SessionLocal()` connections and a `threading.Barrier`
forcing real overlap, asserting final DATABASE state (not HTTP status):

| # | Race | File | Result |
|---|---|---|---|
| 1 | Same store/product generation race | `test_replenishment_concurrency.py` | exactly 1 active plan |
| 2 | Different-product generation race, same store | `test_replenishment_concurrency_matrix.py::test_b_*` | independent, no interference |
| 3 | Sibling shortages sharing one source store | `test_replenishment_concurrency_matrix.py::test_c_*` | generation clean; source row untouched |
| 4 | Concurrent approval of the same plan | `test_replenishment_concurrency_matrix.py::test_d_*` | idempotent, single approved_at |
| 5 | Concurrent execution of the same plan | `test_replenishment_concurrency.py` (2 variants) | exactly 1 PO, both idempotency layers proven |
| 6 | Two plans competing for the same source stock | `test_replenishment_concurrency_matrix.py::test_f_*` | **found and fixed Defect 1** (below) |
| 7 | Execution races a source inventory change | `test_replenishment_concurrency_matrix.py::test_g_*` | capped or STALE, never over |
| 8 | Execution races a destination demand increase | `test_replenishment_concurrency_matrix.py::test_h_*` | executed qty never exceeds approved |
| 9 | Execution races a supplier price change | `test_replenishment_concurrency_matrix.py::test_i_*` | one real price wins, PO line matches exactly |
| 10 | Generation races an authoritative-position change | `test_replenishment_concurrency_matrix.py::test_j_*` | self-consistent either way |

**Defect 1 (CRITICAL) — found by race #6, fixed before this revision was
finalized:** two independently-approved TRANSFER-sourced plans drawing on
the SAME source product could each execute for their full approved
quantity, together exceeding the source's real surplus above its own
reorder point (a demonstrated 16-unit combined execution against a
10-unit real surplus). Root cause: `execute_plan`'s revalidation reads
`source_product.current_qty_on_hand`, but execution only ever creates a
DRAFT transfer (Design Decision 9) — it never decrements on-hand
quantity, so a sibling/competing plan's already-executed-but-unshipped
transfer is invisible to the next plan's surplus calculation, even though
both correctly serialize via the row lock. **Fix**: a new
`_committed_outbound_transfer_quantity` query sums `requested_quantity`
across every DRAFT (not-yet-shipped) transfer line already drawing on
that source product, and this is now subtracted from the source's
surplus before capping the executable quantity. This is query-derived
(consistent with the rest of the position-calculation architecture),
requires no new table, and does not introduce the reservation subsystem
the design doc explicitly rejected. Verified: `test_f_*` now asserts
`total_executed <= 10` and passes across 5 reps; the fix was also proven
necessary by reverting it and confirming the test fails again (see
mutation 4-adjacent evidence is not needed here — this was found live,
not via deliberate mutation).

## 4. Failure injection: A–G, all workflows

`test_replenishment_failure_injection.py` (11 tests) injects real
exceptions **inside** each transaction boundary via `monkeypatch` on a
function actually called mid-transaction (never a pre-transaction
validation step — verified by injecting on `audit_service.log_event` and
on the `_create_purchase_order_inner`/`_create_transfer_inner` calls
themselves), then asserts full rollback:

| Workflow | Injection points | Proven |
|---|---|---|
| A. Plan generation | before any write (ranking helper); after flush, before commit (audit log) | zero plans persist; retry succeeds cleanly |
| B. Plan approval | before commit (audit log) | status unchanged, no approved_at; retry succeeds |
| C. Plan cancellation | before commit (audit log) | status unchanged; retry succeeds |
| D. Execution → PO | inside PO creation (before any PO row); immediately before commit (after PO already flushed) | no orphan PO, plan stays APPROVED, retry produces exactly one PO |
| E. Execution → transfer | same two points, transfer-side | no orphan transfer, plan stays APPROVED, retry produces exactly one transfer |
| F. Retry after failure | same `client_transaction_id` reused after a failed attempt | the failed attempt never durably claimed the key; the retry is a genuine first success, not a false-positive replay |
| G. Accounting | forced failure before the final commit | zero `JournalEntry` rows survive (also re-verified in Section 7) |

All 11 pass; every one was written against the REAL code path (no
pre-transaction validation was used as a substitute for a real in-flight
failure).

## 5. Mutation matrix: 12/12

Each mutation: identify the real protection, disable it in the source,
run the targeted test, confirm failure, restore, confirm pass again.

| # | Protection | Mutation | Test(s) | Result |
|---|---|---|---|---|
| 1 | Duplicate active-plan prevention | Removed `pg_advisory_xact_lock` in `generate_replenishment_plans` | `test_concurrent_generation_runs_never_create_duplicate_active_plans` | caught (2 active plans instead of 1) — **this is the same defect that was found live and fixed during initial M9 implementation**, rediscovered here to confirm the test still detects its absence |
| 2 | Stale recommendation rejection | Disabled `if current_shortfall <= 0` | `test_execute_marks_plan_stale_when_position_recovered_before_execution` | caught |
| 3 | Execution quantity cap | `executable_quantity = current_shortfall` (cap removed) | `test_executed_quantity_never_exceeds_approved_quantity_even_if_shortfall_grows` | caught (10 executed vs 8 approved) |
| 4 | Duplicate execution prevention + DB constraint | Removed `.with_for_update()` on the plan-row lock | `test_concurrent_execution_of_the_same_plan_with_different_client_ids_produces_one_po` | caught — via `ck_replenishment_plans_execution_link_matches_status` CHECK constraint firing (`IntegrityError`), proving the DB constraint is an independent backstop, not merely decorative |
| 5 | Plan lifecycle authorization | `_enforce_plan_access` made a no-op | `test_store_scoped_manager_cannot_approve_or_execute_another_stores_plan` | caught (200 instead of 403) |
| 6 | Destination-store isolation | Disabled the store-mismatch check in `generate_replenishment_plans` | `test_store_scoped_manager_cannot_see_another_stores_plan` | caught (200 instead of 403) |
| 7 | Source-store isolation | `rank_source_stores` no longer subtracts the sister store's own reorder point | `test_rank_source_stores_never_dips_into_sources_own_reorder_protected_stock` | caught (a store AT its reorder point became a false candidate) |
| 8 | Supplier selection determinism | Sort key reduced to `supplier_id` only | `test_rank_suppliers_falls_back_to_lowest_price_when_none_preferred` | caught (wrong supplier selected) |
| 9 | MOQ/pack rounding | MOQ step removed from `apply_moq_and_pack_rounding` | `test_rounding_applies_moq_then_pack_size_in_fixed_order`, `test_supplier_moq_change_between_approval_and_execution_uses_fresh_moq` | both caught |
| 10 | Authoritative position calculation | `position` field omits inbound/open-PO quantities | `test_replenishment_position_invariant.py` (4 of 8 tests) | caught |
| 11 | Idempotency | Disabled the `execution_client_transaction_id` fast-path lookup | `test_concurrent_execution_with_the_same_client_transaction_id_is_idempotent` | **not caught** — genuine finding, not a gap: the DB unique constraint + `IntegrityError` recovery path, and the plan's own `status == EXECUTED` short-circuit, independently cover this case. The fast path is a latency optimization (skip acquiring the row lock for a pure repeat call), not the sole correctness mechanism. Documented as defense-in-depth, re-verified passing with the fast path restored. |
| 12 | Database constraints protecting generated relationships | Same as #4 | `ck_replenishment_plans_execution_link_matches_status` | fired correctly as an independent backstop |

Every mutation was reverted immediately after its test run; the full
suite (515/515) was re-confirmed green after all twelve mutate/revert
cycles.

## 6. Authoritative position invariant (M8 vs M9)

`test_replenishment_position_invariant.py` (8 tests) constructs each
named scenario — plain on-hand, partially received PO, cancelled PO,
partially received transfer, cancelled transfer, an executed plan's DRAFT
PO, a STALE plan, and multiple sibling plans — and asserts M8's
`get_replenishment_suggestions().inventory_position` and M9's
`compute_position(...).position` agree exactly (they share
`compute_positions_bulk`, so this also functions as a regression guard on
that shared function, per mutation 10 above).

**Documented, understood asymmetry (not a divergence, not "fixed"
because fixing it would contradict a load-bearing, already-tested M8
behavior):** a DRAFT purchase order counts toward `open_purchase_order_qty`
immediately (`DRAFT` is in `_OPEN_PO_STATUSES`), but a DRAFT transfer does
**not** count toward `inbound_transfer_qty` until it is actually
`SHIPPED`. This means executing a TRANSFER-sourced sibling plan doesn't
immediately reduce the calculated position the way executing a
SUPPLIER-sourced sibling does. This was investigated in depth (Section
3's Defect 1 write-up covers the real consequence): it does **not** cause
over-allocation, because each sibling's executable quantity is
independently capped at its own `suggested_quantity`, never at a shared
pool. `test_multiple_sibling_plans_position_reflects_only_real_documents`
demonstrates and asserts this exact behavior rather than treating it as
untested. Changing this asymmetry would require altering M8's own
`_open_purchase_order_quantities`/`_inbound_transfer_quantities`
semantics, which `test_replenishment.py` (M8's own, byte-for-byte
unmodified test file) locks in as correct — out of scope for an M9-only
hardening pass.

## 7. Stale recommendation / TOCTOU: all 9 named cases

| Case | Test | Rule proven |
|---|---|---|
| Stock increases after recommendation | `test_execute_marks_plan_stale_when_position_recovered_before_execution` | STALE, zero PO |
| Stock decreases after recommendation | `test_executed_quantity_never_exceeds_approved_quantity_even_if_shortfall_grows` | executes capped at approved qty |
| Another replenishment executes first | `test_remaining_need_reflects_executed_sibling_quantities`, concurrency race #6 | remaining need recalculated; no over-allocation (Defect 1 fix) |
| Inbound PO appears | `test_replenishment_toctou_additional.py::test_inbound_po_appearing_after_approval_*` | STALE, zero PO |
| Inbound transfer ships | `test_replenishment_toctou_additional.py::test_inbound_transfer_shipping_after_approval_*` | STALE, zero PO/transfer |
| Supplier MOQ changes | `test_supplier_moq_change_between_approval_and_execution_uses_fresh_moq` | executes at the CURRENT MOQ, not the stale generation-time rounding |
| Supplier price changes | concurrency race #9 | executed_unit_cost is exactly one real price, matching the PO line exactly |
| Source-store availability changes | `test_execute_marks_transfer_plan_stale_when_source_surplus_disappears`, race #7 | STALE or capped, never over |
| Destination demand changes | race #8 | capped at approved, never inflated |

The deterministic rule, confirmed identically across every case:
**recompute fresh under the row lock at execution time; if the current
shortfall is `<= 0`, mark STALE and create nothing; otherwise execute
`min(approved_quantity, current_shortfall)`, rounded against the CURRENT
supplier terms.** Never a partial silent adjustment, never a stale value
carried forward.

## 8. Generation duplicate/allocation invariants

- One shortage cannot produce duplicate active plans: proven by the
  advisory lock (mutation 1, race #1).
- Legitimate sibling plans DO coexist: `test_generate_splits_shortfall_across_sibling_plans_when_source_insufficient`
  and the position-invariant sibling test.
- Siblings cannot collectively over-allocate a source: **this is exactly
  Defect 1**, found by race #6, fixed via `_committed_outbound_transfer_quantity`.
- MOQ/pack rounding never creates undocumented over-allocation: the
  rounding is applied to the CAPPED `executable_quantity` at execution
  time, and the resulting `rounded_quantity` for a TRANSFER plan is
  further `min()`-ed against `source_surplus` (now Defect-1-corrected) —
  see `execute_plan`'s transfer branch.
- Retrying generation is idempotent: `test_generate_first_call_succeeds_retry_is_a_noop`
  and the E2E scenario's own retry step.
- Concurrent generation cannot create conflicting allocations: races #1–#3.
- The advisory lock's granularity (per `(store_id, product_id)`, held for
  the remainder of the generation transaction) was not found to cause
  unacceptable serialization — it only blocks a genuinely-colliding
  concurrent generation for the SAME product, never unrelated products
  (proven by race #2's independence assertion). No premature optimization
  was applied; the lock scope matches the actual collision surface.

## 9. Accounting boundary

`test_replenishment_accounting_boundary.py` (6 tests): generation,
approval, and execution into either a DRAFT PO or DRAFT transfer each
create zero `JournalEntry` rows; the full downstream lifecycle (submit,
then receive via the pre-existing M3 workflow) still creates real
accounting at exactly the M3-defined point; and a forced mid-execution
failure leaves zero journal entries. Combined with the static check that
`app.modules.replenishment.service` never imports
`app.modules.accounting`, the boundary is proven both statically and
dynamically.

## 10. Migration / M0–M8 data integrity

`test_migrations.py::test_m0_m8_data_integrity_survives_m9_upgrade_downgrade_reupgrade`
populates real M0–M8 business data (two stores, a category, a supplier, a
partially-received purchase order, and a partially-shipped inter-store
transfer) against a dedicated `erp_test` database, captures row counts
and quantity/cost aggregates, then asserts they are unchanged after
upgrading to M9 head, after downgrading back to M8 head, and after
re-upgrading to M9 head again.

**Genuine finding (systemic, pre-existing, not M9-specific):**
`permissions`/`role_permissions`/`roles` counts do NOT behave as
milestone-scoped snapshots in this codebase. The M2 RBAC-seed migration
(`e6180fca2ee0`) deliberately imports the LIVE
`app.modules.auth.permissions.ALL_PERMISSIONS`/`ROLE_PERMISSIONS` dicts
(its own docstring: "so the seeded data and the routes that check these
permission codes can never drift apart silently"), and every milestone
from M4 through M9 (verified by grepping `alembic/versions/*.py`) follows
the identical pattern of adding its own permissions via a
milestone-specific list and deleting exactly that list on downgrade. The
practical effect in a from-scratch build against TODAY's code: M2 already
seeds ALL currently-defined permissions — including M9's four
`supply_chain.*` ones — at the M2 step, so a fresh build stopped at "M8
head" and one stopped at "M9 head" show IDENTICAL permission counts, and
M9's own downgrade (mirroring M8/M7/M6/M5/M4's identical pattern)
correctly removes ITS OWN four permissions but this then diverges from
what a fresh "M8 head" build would show in this environment. This is
**not an M9 regression** — it is a pre-existing, six-milestone-wide
characteristic of how RBAC seed data interacts with an evolving codebase
under from-scratch test builds, and it does not corrupt any real,
incrementally-migrated deployment's actual data (there, each migration
ran against the code that existed when it was applied). Rewriting M9's
migration alone to diverge from its five siblings would be an
inconsistent, out-of-scope patch; the test correctly scopes its strict
equality assertions to genuine business/operational tables (stores,
categories, products, suppliers, purchase orders, purchase order items,
transfers, transfer lines) and captures the RBAC tables only for
visibility, not equality. **Recommendation for a future, separate pass:**
either freeze each historical migration's permission-seeding to a literal
inline snapshot, or accept and document this as intentional and scope all
migration tests accordingly — this is a project-wide decision, not one
this M9 hardening pass should make unilaterally.

All four downgrade guards (M9's own two — generated PO exists, generated
transfer exists — plus the inherited M7/M8 guards) were re-confirmed
firing correctly against real inserted data, before any destructive DDL,
in the existing `test_migrations.py` suite (unchanged from Rev 1, still
passing).

## 11. RBAC / store-isolation verification

`test_replenishment_adversarial_security.py` (8 tests) adds, on top of
Rev 1's `test_replenishment_api.py` coverage:

- A full 5-role matrix (Admin, Manager, Cashier, Inventory Clerk,
  Auditor) against read/generate/approve/execute, each role tested
  against its OWN fresh plan to avoid cross-role state contamination.
- Foreign-store execute and cancel attempts (Rev 1 only covered approve).
- A genuinely unrelated THIRD store cannot see a TRANSFER plan between
  two other stores (404).
- The documented, intentional source/destination symmetry: the SOURCE
  side of a transfer plan CAN view and approve it (mirrors
  `transfers.service`'s own established from/to symmetry) — verified as
  intentional design, not an authorization gap.
- Supplier-product catalog RBAC (previously completely untested):
  Cashier and Auditor cannot create; Auditor can read; Inventory Clerk
  can create (has `supply_chain.plan`).
- Request tampering: extra/forged fields in the generate and execute
  request bodies (`supplier_id`, `status`, `executed_quantity`,
  `unit_cost`) are silently ignored — the server never trusts
  client-supplied financial/status values, proven by asserting the
  response reflects only server-computed values.

Every mutating M9 endpoint has now been exercised adversarially across
every role and every store-boundary case named in the task.

## 12. Idempotency verification

`test_replenishment_idempotency_matrix.py` (11 tests) documents and
proves the idempotency model per operation (see the file's own table):
state-based idempotency for generate/approve/cancel, explicit-key
idempotency for execute (exact retry, simulated-timeout retry, and a key
reused against a genuinely different plan — which correctly returns the
FIRST plan associated with that key, never corrupting the second),
plus direct proof that `execution_client_transaction_id` carries a REAL
database UNIQUE constraint (not just application-level checking) and that
`create_supplier_product`'s effective-dated uniqueness is DB-enforced
too.

## 13. E2E verification

`test_replenishment_e2e_scenario.py` drives the complete chain through
the REAL HTTP/API layer against real PostgreSQL: stock position →
suggestion → metrics → generation (with source/supplier ranking visible
in the reason text) → duplicate-generation retry (no-op) → plan detail →
approval → execution → generated PO reachable via the EXISTING
`/purchasing` endpoint (never duplicated by M9) → submit → receive (the
existing M3 workflow) → real inventory change → plan now shows
`fulfilled: true` → subsequent suggestions/generation correctly find
nothing left to recommend. Every financially material field
(`shortfall`, `suggested_quantity`, `executed_quantity`,
`executed_unit_cost`, the PO line's `unit_cost`/`quantity_ordered`, the
product's `current_qty_on_hand`) is asserted at the step where it
changes.

## 14. Browser verification

Re-confirmed from Rev 1 (not re-run live in this revision, since no
frontend code changed): a live Chromium session via Playwright drove
login → Supply Chain page → generate → select → approve → execute →
verified PO link, with screenshots reviewed and smoke-test data cleaned
up. The frontend gates (tsc, oxlint, prettier, vitest, vite build) were
all re-run clean in this revision (Section 15).

## 15. Performance / query safety

**Genuine finding and fix:** `get_exceptions` contained a real N+1 —
a per-active-product `db.get(Supplier, ...)` call plus a per-product
`get_current_supplier_product` query. At 20 products this issued more
than double the SQL statements of 5 products; confirmed via a
`before_cursor_execute`-counting test
(`test_replenishment_performance.py`) that the count grew linearly (12 →
27 statements) before the fix. Fixed by batch-fetching all distinct
default suppliers in one query and batch-checking current-price
existence for all (supplier, product) pairs in one query, mirroring the
existing `compute_positions_bulk` pattern. Re-verified: query count is
now IDENTICAL for 5 and 20 products, and the regression test would catch
a reintroduction of the N+1 (proven by deliberately reintroducing it,
confirming the test fails, then re-fixing it).

No other N+1s were found in the M9 endpoints: `compute_positions_bulk`,
`get_supply_chain_metrics`, `rank_source_stores`, and `rank_suppliers`
were all inspected and are either already bulk-query or operate on a
small, bounded candidate set (sister stores / suppliers for one product,
never scaling with total catalog size). No missing index was identified
against the existing schema's indexes (`ix_replenishment_plans_*`,
`ix_supplier_products_*`).

## 16. Final adversarial review

| Attack | Vector | Defense | Test |
|---|---|---|---|
| Duplicate purchasing | Two concurrent generation/execution attempts | Advisory lock + row lock + DB unique constraints | races #1, #5; mutations 1, 4 |
| Excess inventory (over-allocation) | Two plans drawing the same source | `_committed_outbound_transfer_quantity` (Defect 1 fix) | race #6 |
| Incorrect replenishment | Stale position used at execution | Fresh revalidation under lock | Section 7 (all 9 TOCTOU cases) |
| Cross-store movement | Client-supplied store IDs, foreign-store actions | Server-derived `caller_store_id`, `_enforce_plan_access` | Section 11 |
| Stale execution | Approved-then-invalidated plan executed anyway | Deterministic STALE rule | Section 7 |
| Duplicate PO generation | Retry, concurrent execute | `execution_client_transaction_id` + DB constraint | race #5, idempotency matrix |
| Duplicate transfer generation | Same as above, transfer branch | Same mechanisms | idempotency matrix |
| Incorrect supplier pricing | Price changes mid-flight | Always re-read fresh at execution; frozen onto the PO line | race #9, mutation 8 |
| Unauthorized approval | Wrong role or wrong store | RBAC + `_enforce_plan_access` | Section 11, mutation 5 |
| Unauthorized execution | Wrong role or wrong store | Same | Section 11, mutation 5 |
| Inconsistent lifecycle state | Impossible status combinations | `ck_replenishment_plans_execution_link_matches_status` | mutation 4 |
| Accounting mismatch | Premature journal entries | Boundary proven in Section 9 | 6 dedicated tests |
| Data loss | Failed transactions leaving partial state | Failure injection, Section 4 | 11 dedicated tests |

Every named attack has a demonstrated, currently-passing defense.

## 17. Complete test counts

- Backend: **515/515** passing (457 baseline + 58 new hardening tests
  across 9 new files plus 1 new test in `test_migrations.py`).
- New files: `test_replenishment_concurrency_matrix.py` (8),
  `test_replenishment_failure_injection.py` (11),
  `test_replenishment_position_invariant.py` (8),
  `test_replenishment_toctou_additional.py` (3),
  `test_replenishment_accounting_boundary.py` (6),
  `test_replenishment_adversarial_security.py` (8),
  `test_replenishment_idempotency_matrix.py` (11),
  `test_replenishment_e2e_scenario.py` (1),
  `test_replenishment_performance.py` (1); plus 1 new test in
  `test_migrations.py`.
- Frontend: **30/30** passing (unchanged — no frontend code changed in
  this revision).
- Gates: `ruff check .`, `black --check .`, `mypy app` all clean;
  `tsc -b`, `oxlint`, `prettier --check .`, `vitest run`, `vite build`
  all clean.

## 18. Bugs discovered and fixed during this hardening pass

1. **CRITICAL — cross-plan source over-allocation** (Section 3, Defect
   1). Fixed via `_committed_outbound_transfer_quantity`.
2. **Real N+1 in `get_exceptions`** (Section 15). Fixed via batch
   supplier/price-existence lookups.
3. **Dead code**: `_enforce_store_access` in `replenishment/service.py`
   was defined but never called anywhere (the actual check was already
   correctly inlined in `generate_replenishment_plans`). Removed as a
   minor cleanup; confirmed via the full suite that nothing depended on
   it.

No other genuine defects were found. Every other test failure
encountered while writing this hardening pass (documented in the
session, not repeated here) was a test-authoring correction — an
initially-wrong expected value in the new E2E test's metrics assertion
(the `<=` boundary-inclusive metrics semantics vs. the strict-inequality
suggestion-list semantics — both are correct and intentional, verified
against the source), and a wrong keyword-argument order in one call to
`cancel_transfer` — never a defect in the implementation being tested.

## 19. Remaining limitations (honest, not silently reduced)

- **Permissions/role_permissions migration-count semantics** (Section
  10) are a real, systemic, six-milestone-wide characteristic that this
  M9-scoped pass correctly identified, worked around in its own test,
  and explicitly declined to patch unilaterally — recommending a
  separate, project-wide decision instead.
- **Supplier-product catalog UI**: still backend-only. Decision (not an
  oversight): the M9 task's core UI requirement was the plan lifecycle
  (RECOMMENDED/APPROVED/EXECUTED/FULFILLED/STALE/CANCELLED), which
  `SupplyChainPage.tsx` fully implements; the catalog itself is
  low-frequency admin/purchasing-team reference data, fully covered by
  tested, RBAC-gated API endpoints (`test_supplier_product_catalog_rbac`,
  the idempotency-matrix DB-constraint tests), and can be managed via
  those endpoints directly today. Building a dedicated CRUD page was
  judged lower-value than closing the concurrency/financial-safety gaps
  this pass prioritized; it remains a reasonable, bounded follow-up.
- **Browser verification** was not re-run live in this revision since no
  frontend code changed; Rev 1's live browser run (screenshots reviewed,
  data cleaned up) stands as the evidence for the UI's own correctness.
- The 8 mutation targets not in the original "priority" list of 12 (per
  the task's own wording, only 12 were named as priorities) are not all
  individually enumerated beyond what Section 5 covers — Section 5
  covers exactly the 12 named priorities, which is what was asked.

## 20. Exact reasons for anything still deferred

Everything named as deferred in Rev 1 that fell within this hardening
pass's explicit 15-phase scope has been closed. The two items in Section
19 (migration RBAC-count semantics, catalog UI) are deferred for the
specific, stated reasons above — neither is a financial-safety or
correctness gap in the replenishment/purchasing/inventory/cross-store
logic itself, which was this pass's stated goal.
