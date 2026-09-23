# M17 Testing Sessions

Companion to `docs/M17_DISCOVERY.md` and `docs/M17_DESIGN.md`. Sessions
A–J as specified for this milestone. Because Phase 0 discovery found the
financial core already solid across nearly every category (idempotency,
concurrency, store isolation, audit linkage, reconciliation — see
`M17_DISCOVERY.md` §2 for the full evidence), M17's actual implementation
is two narrow items, not a large new subsystem. Several sessions below
are correspondingly short: their real content is "already exhaustively
verified during Phase 0 discovery by reading the actual code and its
existing tests," which is reported honestly rather than padded with
redundant re-testing of unchanged code.

## Session A — Baseline

Recorded twice: once before any M17 code change (Phase 0 testing gate),
once again after (below), per the explicit instruction not to assume
M16's numbers remain valid.

**Before M17 changes:**
- Backend: `pytest -q` → 930 passed.
- Frontend: `npx vitest run` → 49 passed (14 files).
- Deploy-infra: the 5 CI-wired files → 22 passed (88.18s).
- `ruff check .`, `black --check .`, `mypy app` → all clean.
- `npx tsc -b`, `oxlint .`, `prettier --check .` → all clean.
- `alembic heads` → single head, `4a83c462dbff`.
- Working tree: clean.

**After M17 changes** (Session J has the full final numbers): backend
933 passed (930 + 3 new: 1 store-isolation test, 2 reconciliation
tests). No frontend or deploy-infra files were touched by M17, so those
counts are unchanged from the "before" figures above — verified by `git
diff --stat`, not assumed.

## Session B — AP correction

`M16_TESTING_SESSIONS.md` Session C already covers the full required
scenario list (normal reversal, unauthorized, cross-store, duplicate,
concurrent, rollback-on-posting-failure, audit failure, missing actor,
invalid source, already-reversed) for both supplier payments and
credit notes — re-read in full during M17 Phase 0 discovery and found
unchanged and still passing (all 14 `test_ap_reversal.py` tests are in
the 933-passed count above). **New this milestone**: the one scenario
that was not yet covered — GL-vs-subledger reconciliation specifically
after a reversal — is now covered by
`test_ap_reconciliation.py::test_ap_reconciliation_holds_after_supplier_payment_reversal`
and `::test_ap_reconciliation_holds_after_supplier_credit_note_reversal`
(both hand-derive their expected numbers before asserting, matching
this file's established discipline, and both pass — the reconciliation
functions were correct by construction, now proven rather than merely
inferred).

## Session C — Cash integrity

Full till-lifecycle and concurrency coverage already exists and was
re-read in full during discovery: `test_shifts_concurrency.py`'s six
tests cover concurrent shift opens, concurrent duplicate closes,
different-key concurrent closes, a sale finalization racing a shift
close, a cash movement racing a shift close, and duplicate cash-movement
requests under concurrency — all using genuine separate
`SessionLocal()`/`threading` sessions, not sequential calls to the same
function. `test_shifts.py` covers the expected-cash formula (including
the M16-added non-cash-refund case), over/short variance posting, and
the M16 regression proving `CASH_SHIFT_VARIANCE` cannot be reversed via
the generic mechanism. **New this milestone**:
`test_direct_service_call_to_list_cash_movements_enforces_store_isolation`
closes the one gap discovery found (§D) — `list_cash_movements` now
takes `caller_store_id` and enforces it exactly like `get_shift`/
`list_shifts`.

## Session D — Inventory integrity

Re-verified during discovery (§E): `create_stock_adjustment` is
store-isolated, idempotent (`client_transaction_id`), audit-logged, and
genuinely concurrency-tested
(`test_inventory_idempotency.py::test_concurrent_duplicate_adjustment_requests_create_only_one_adjustment`
and `::test_cross_store_direct_service_call_cannot_mutate_another_stores_stock`,
both already in the 933-passed count, both re-run this session with no
changes). No stock-adjustment reversal/correction mechanism exists —
confirmed as a genuine limitation (not a defect: no design document
ever specified one), reported in `M17_HARDENING_AUDIT.md`, not built
speculatively here.

## Session E — Accounting reconciliation

The actual new work of this milestone — see Session B above for the
two new tests. Also re-confirmed during discovery: `inventory_reconciliation`
(accounting/service.py), `payroll_gl_reconciliation` and
`in_transit_reconciliation` (reports/service.py), and
`cash_payment_method_summary` (reports/service.py, the sales/returns↔GL
check) all exist and are exercised by their own pre-existing test
suites, unchanged by M17.

## Session F — Security / IDOR

Every store-scoped mutating function across AP, shifts, sales, and
inventory was checked during discovery for a `caller_store_id`
parameter and a call to its module's `_enforce_store_access` — confirmed
present on all of them (`M17_DISCOVERY.md` §L). The one exception found
(`list_cash_movements`) is fixed and mutation-tested (Session I below).
No new endpoints, roles, or permissions were introduced by M17, so there
is no new privilege-escalation surface to test beyond what M16 already
covers.

## Session G — Failure injection

**INFERENCE, not independently re-run this session**: `reverse_supplier_payment`/
`reverse_supplier_credit_note`'s failure behavior at each of validation
(missing reason → `REVERSAL_REASON_REQUIRED`), authorization
(cross-store → `ForbiddenError` before any write), and posting
(journal imbalance would raise before commit, same as every other
`_post_journal` caller) was verified by direct code reading during
discovery, not by newly injecting each failure this session — because
none of that code changed in M17, and M16's own `test_ap_reversal.py`
already includes a dedicated
`test_failure_during_gl_posting_leaves_invoice_balance_unchanged` test
exercising exactly this path (in the 933-passed count, unchanged, still
passing). No new failure-injection code was written for M17's two
narrow items because neither introduces a new write path: item 1 is a
read-only isolation check, item 2 is test-only.

## Session H — Concurrency

Covered by Sessions B/C/D above: every financially material path this
document identifies as "already solid" has an existing genuine
multi-session concurrency test, re-confirmed present and passing during
discovery and the Session J full run. No new concurrent-mutation code
was introduced by M17 (item 1 is a read; item 2 is tests only), so no
new concurrency test was needed beyond what already exists.

## Session I — Mutation testing

One control was actually changed by M17 (`list_cash_movements`'s new
store-isolation check) — mutated and confirmed caught, live, this
session:

- **Store isolation** (`shifts/service.py::list_cash_movements`):
  replaced the new `if caller_store_id is not None: get_shift(...)`
  guard with `if False: ...`. Result:
  `test_direct_service_call_to_list_cash_movements_enforces_store_isolation`
  failed (no `NotFoundError` raised for the cross-store call). Reverted;
  32/32 `test_shifts.py` tests green again.

**Not re-mutated, per the explicit instruction not to mutate for
ceremony when a control's exact test has already been executed and
recorded**: authorization, idempotency, reversal uniqueness, accounting
sign, cash formula, and the inventory mutation guard were all mutated
and caught during **M16** (`M16_TESTING_SESSIONS.md` Session J:
privilege-escalation guard, store-settings cross-store isolation, AP
reversal idempotency) or during the **M15**/earlier milestones whose
results are preserved in their own hardening-audit documents. M17
changed none of that code, so re-mutating it here would test nothing
new — the results are preserved by reference, not repeated for
appearance's sake.

## Session J — Full regression

No prior test was weakened, deleted, skipped, or rewritten to
accommodate M17. No hardcoded database id was introduced (the new
`_actor_id(db, store)` helper in `test_ap_reconciliation.py` mirrors
`test_ap_reversal.py`'s own established pattern exactly, creating a
real committed user each time).

- Backend: `pytest -q` → **933 passed** (930 M16 baseline + 3 M17: 1
  store-isolation test + 2 reconciliation tests).
- Backend gates: `ruff check .`, `black --check .`, `mypy app` — all
  clean.
- Frontend/deploy-infra: unchanged from Session A's "before" figures
  (49 passed / 22 passed) — no frontend or deploy-infra file was
  touched by M17, confirmed by `git diff --stat`.
- Migration state: single head, still `4a83c462dbff` — M17 introduced
  no schema change, so no new migration exists to test.
