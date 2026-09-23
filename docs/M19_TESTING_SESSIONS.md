# M19 Testing Sessions

Companion to `M19_DISCOVERY.md` and `M19_DESIGN.md`. Each session records
the command run, what was expected, what actually happened, the exact
test count, evidence, and any known limitation — the same discipline
every milestone since M10 has followed. Sessions are ordered roughly as
the work was actually done: discovery-era baseline first, then one
session per fix/domain area, then the cross-cutting sessions
(idempotency, concurrency, failure injection, frontend, migrations),
ending with the final full-regression/CI session.

## Session A — Baseline full regression (Phase 0)

**Command**: `python -m pytest tests/ -q` (before any M19 code change)
**Expected**: every M16-M18 test still green; no drift since M18's own
final verification.
**Actual**: all backend tests passed (936 at the time, matching M18's
final report figure). No regressions found.
**Evidence**: full run output, exit code 0.
**Limitation**: none.

## Session B — Purchase order idempotency (fix #1) round-trip

**Command**: `python -m pytest tests/test_purchasing_api.py tests/test_purchasing.py tests/test_purchasing_audit_and_snapshots.py tests/test_purchasing_idempotency.py tests/test_replenishment_failure_injection.py tests/test_replenishment_position_invariant.py tests/test_replenishment_toctou_additional.py -q`
**Expected**: after adding `client_transaction_id` as a required field on
`PurchaseOrderCreate`/`create_purchase_order`, every existing call site
across these files needed a value supplied (a factory default or an
explicit key) or the suite would fail on the new NOT NULL/required-field
constraint.
**Actual**: first run surfaced 68 failures (missing field across ~10
files); each was fixed at its root — `tests/factories.py::make_purchase_order`
gained a default `client_transaction_id`, and every raw HTTP payload /
direct service call that had been missing the field was given one. Final
run: all files green.
**Evidence**: iterative `pytest -k` runs shown in the session transcript;
final combined run `282 passed, 674 deselected`.
**Limitation**: none — this was mechanical test-suite repair, not a
design gap.

## Session C — Purchase return quantity validation (fix #2)

**Command**: `python -m pytest tests/test_purchasing_api.py -q`
**Expected**: a return exceeding what a specific PO actually received is
rejected (`RETURN_EXCEEDS_RECEIVED_QUANTITY`), independent of whether
on-hand stock would otherwise cover it; a return of exactly the received
quantity succeeds; a second partial return that would push the running
total past the received quantity is rejected; `INSUFFICIENT_STOCK` still
fires correctly when the return is within the PO's received quantity but
exceeds current on-hand stock (isolated via a scenario where some of the
received stock left through an unrelated movement).
**Actual**: all four new tests
(`test_purchase_return_cannot_exceed_received_quantity`,
`test_purchase_return_of_exactly_the_received_quantity_succeeds`,
`test_second_partial_return_cannot_push_total_past_received_quantity`,
rewritten `test_purchase_return_cannot_exceed_current_stock`) passed.
**Evidence**: `36 passed` in `test_purchasing_api.py`.
**Limitation**: no per-receipt-lot tracking (accepted, documented
limitation — see `M19_DESIGN.md` §2's non-goal).

## Session D — `default_payment_terms_days` write path (fix #3)

**Command**: `python -m pytest tests/test_purchasing_api.py -q -k terms`
**Expected**: creating/updating a supplier with `default_payment_terms_days`
persists and round-trips through `SupplierRead`; a negative value is
rejected (422); the pre-existing
`test_ap_invoices.py::test_due_date_defaults_from_supplier_payment_terms`
(which already exercised the read side via a factory override) continues
to pass unchanged, proving the write path now feeds the same, already-
correct AP due-date calculation.
**Actual**: 2/2 new tests passed;
`test_due_date_defaults_from_supplier_payment_terms` unaffected.
**Evidence**: `2 passed, 32 deselected`.
**Limitation**: none.

## Session E — Invoice-creation idempotency defect (fix #4, found during this milestone's own concurrency testing)

**Command**: `python -m pytest tests/test_ap_concurrency.py -q -k test_f`
**Expected** (before the fix existed): two threads creating a purchase
invoice with the identical `client_transaction_id` (and, as a real retry
would, the identical `invoice_number`) should both resolve to the same
invoice — mirroring `test_e`'s proof for payments.
**Actual (first run, pre-fix)**: failed deterministically, 3/3 repeats —
the losing thread received `DUPLICATE_SUPPLIER_INVOICE_NUMBER` instead of
the winner's invoice. Root-caused to `create_purchase_invoice`'s
`IntegrityError` recovery branching on which of two UNIQUE constraints
fired before checking for an idempotent match, unlike every other
module's identical recovery block. Fixed per `M19_DESIGN.md` §5 (check
client_transaction_id match first, regardless of which constraint name
came back).
**Actual (post-fix)**: `test_f_two_concurrent_invoice_creations_with_same_idempotency_key_create_exactly_one`
passes, 5/5 repeats. `tests/test_ap_mutation.py`'s pre-existing invoice-
idempotency mutation test was rewritten (it previously asserted the
buggy behavior as correct) to instead prove the fix: retry transparency
now survives even with the fast-path lookup alone disabled, because the
`IntegrityError`-recovery path is a genuine second line of defense.
**Evidence**: `tests/test_ap_concurrency.py` `6 passed`;
`tests/test_ap_mutation.py` `7 passed`; full `ap_`/`purchas`/`supplier`/
`replenishment` sweep `282 passed` after the fix.
**Limitation**: none — this closes a real, reproducible race, not a
cosmetic issue.

## Session F — Multi-store isolation (Phase 9)

**Command**: `python -m pytest tests/test_purchasing.py tests/test_ap_payments.py -q -k "cross_store"`
**Expected**: `create_purchase_return`, `record_supplier_payment`, and
`create_supplier_credit_note` each reject a caller scoped to a different
store than the target store, at the service layer directly (not only
through the HTTP route) — the enforcement code was already correct per
discovery; these tests prove it.
**Actual**: all 3 new tests passed
(`test_cross_store_purchase_return_is_rejected`,
`test_cross_store_supplier_payment_is_rejected`,
`test_cross_store_supplier_credit_note_is_rejected`).
**Evidence**: `2 passed` (test_ap_payments.py -k cross_store) + 1 passed
in test_purchasing.py's full run.
**Limitation**: none.

## Session G — Idempotency (sequential retry) coverage

**Command**: `python -m pytest tests/test_purchasing_idempotency.py -q`
**Expected**: exact-duplicate and retry-after-business-failure semantics
hold for PO creation (new, fix #1), matching the pre-existing coverage
for receipts/returns.
**Actual**: `test_exact_duplicate_po_creation_request_returns_the_same_purchase_order`
and `test_retry_after_po_creation_business_failure_is_a_fresh_attempt`
both passed alongside the 3 pre-existing tests.
**Evidence**: `5 passed`.
**Limitation**: none.

## Session H — Concurrency (real threads, independent DB sessions)

**Command**: `python -m pytest tests/test_purchasing_concurrency.py tests/test_ap_concurrency.py -q`
**Expected**: (1) two threads creating a PO with the same
`client_transaction_id` produce exactly one row (test G, new); (2) two
threads returning against the same PO/product, each individually within
the received quantity but jointly exceeding it, serialize on the
`PurchaseOrderItem` row lock so exactly one succeeds (test H, new); (3)
the pre-existing 6 receiving-concurrency scenarios (A-F) are unaffected;
(4) the new invoice-creation-dedup concurrency test (test F in the AP
file, Session E above) passes.
**Actual**: `test_purchasing_concurrency.py`: `7 passed` (A-F pre-existing
+ new G, then H added afterward makes it 8 — see final count in Session
O). `test_ap_concurrency.py`: `6 passed` (A-E pre-existing + new F).
**Evidence**: full test output; `test_h_concurrent_returns_against_the_same_po_cannot_jointly_over_return`
specifically shows exactly one of two racing 7-unit returns against a
10-unit received PO succeeding, on-hand ending at 3 (10 - 7), not -4 or 10.
**Limitation**: none — genuine independent-session races throughout, no
fake sequential "concurrency" tests.

## Session I — Supplier lifecycle coverage

**Command**: `python -m pytest tests/test_purchasing_api.py -q -k "deactivate or inactive_supplier"`
**Expected**: activate/deactivate round-trips correctly (never exercised
before this milestone); a PO cannot be created against a deactivated
supplier.
**Actual**: both new tests
(`test_deactivate_and_reactivate_supplier`,
`test_purchase_order_cannot_be_created_for_an_inactive_supplier`) passed.
**Evidence**: `2 passed`.
**Limitation**: none.

## Session J — WAC fractional-quantity precision

**Command**: `python -m pytest tests/test_purchasing.py -q -k fractional`
**Expected**: `compute_new_wac`'s formula (already verified correct by
discovery at whole-unit quantities) holds identically at fractional
precision (2.375 kg + 1.625 kg at different costs): `(2.375*10 +
1.625*14) / 4 = 11.625`.
**Actual**: passed, confirming exact-Decimal arithmetic at fractional
scale with no drift.
**Evidence**: `1 passed`.
**Limitation**: none.

## Session K — Cancel-after-partial-receipt

**Command**: `python -m pytest tests/test_purchasing.py -q -k cancelling_a_partially_received`
**Expected**: cancelling a `PARTIALLY_RECEIVED` PO succeeds (it's in
`_CANCELLABLE_PO_STATUSES`) and the inventory already received is
retained — cancellation stops future receiving, it does not reverse a
receipt that already posted (matching the codebase's append-only-ledger
philosophy applied elsewhere).
**Actual**: passed — after cancelling a PO that had received 4 of 10
units, `product.current_qty_on_hand` remained 4.
**Evidence**: `1 passed`.
**Limitation**: none — this is documented, intentional behavior, not a
defect discovery.

## Session L — Failure injection (Phase 15)

**Command**: `python -m pytest tests/test_purchasing_failure_injection.py -q`
**Expected**: a forced failure inside `receive_goods`'s accounting
posting, `create_purchase_invoice`'s audit logging, and
`create_purchase_return`'s accounting posting each roll back completely
— no receipt/invoice/return row, no inventory movement, no PO
running-total change, no journal entry survives.
**Actual**: all 3 new tests passed.
**Evidence**: `3 passed`; each test independently re-queries every
affected table after the forced exception and asserts zero rows /
unchanged cached values.
**Limitation**: none.

## Session M — Frontend (`SupplyChainPage.tsx`, previously zero coverage)

**Command**: `npx vitest run src/pages/SupplyChainPage.test.tsx` then
`npx vitest run` (full suite), `npx tsc -b`, `npm run lint`.
**Expected**: lists plans, shows the empty state, shows a load error,
hides Approve/Execute/Cancel for a user without those permissions, and
approves a recommended plan (state transition via a real button click).
**Actual**: all 5 new tests passed on the first correctness pass after
fixing two route-ordering/text-ambiguity issues (a more specific
`/plans/501` route needed to be listed before the general `/plans`
route in the mock; the post-approve assertion needed to avoid an
ambiguous `getByText('APPROVED')` match once both the list-row badge and
the detail-panel badge said APPROVED). Full frontend suite: `54 passed`
(up from 49 at M18's close). `tsc -b`: clean. `oxlint`: clean.
**Evidence**: full vitest/tsc/lint output.
**Limitation**: none.

## Session N — Migration safety (Phase 14)

**Command**: `python -m pytest tests/test_migrations.py -q`
**Expected**: the new `3a0d50ccc909` migration (PO idempotency key)
upgrades/downgrades/re-upgrades cleanly; `test_full_upgrade_downgrade_upgrade_cycle`'s
table-count and head-revision assertions reflect the new head; all 15
pre-existing migration tests (M2-M18 guards, RBAC seed counts, etc.)
remain green.
**Actual**: first run failed exactly one assertion (`test_full_upgrade_downgrade_upgrade_cycle`
expected head `4a83c462dbff`, actual `3a0d50ccc909` — correct, since a
new migration was added). Fixed by adding `M19_HEAD_REVISION` and
updating the two head-revision assertions. Second run: all 16 tests
passed, including populated-data upgrade→downgrade→re-upgrade cycling
through every milestone's migrations in sequence.
**Evidence**: `16 passed`.
**Limitation**: none.

## Session O — Final full regression, mutation testing, and CI

**Command**: `python -m pytest tests/ -q` (backend), `npx vitest run &&
npx tsc -b && npm run lint` (frontend), plus the 8 required live
mutation tests (see `M19_HARDENING_AUDIT.md` "Mutation testing" for the
full table).
**Expected**: full backend and frontend suites green on the actual final
working tree; every required mutation caught by an existing or
newly-added test; working tree clean of mutation artifacts after each
revert (verified by `diff` against a pre-mutation backup after every
single mutation, not just at the end).
**Actual**: backend `957 passed` (up from the 936 M18 baseline —
net new tests across all sessions above); frontend `54 passed`, `tsc -b` clean,
`oxlint` clean; all 8 mutation targets caught (two — "disable
duplicate-receipt protection" and "bypass idempotency" — required
disabling BOTH the fast-path lookup AND the `IntegrityError`-recovery
layer to actually break, since the fast-path alone is backed by a
second, equally load-bearing layer everywhere in this codebase; "bypass
inventory mutation protection" required a new, dedicated
`test_record_movement_itself_refuses_to_go_negative` test in
`test_inventory.py`, since every existing INSUFFICIENT_STOCK test only
proved a CALLER's own pre-check, never `record_movement`'s own guard in
isolation — see `M19_HARDENING_AUDIT.md` for the full writeup). `git
diff --stat` after all mutations were reverted showed only the intended,
reviewed M19 changes — zero leaked mutation edits.
**Evidence**: full pytest/vitest/tsc/lint output; per-mutation `diff`
confirmations; final CI run (see the M19 completion report for the run
ID and job results).
**Limitation**: none.
