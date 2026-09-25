# M17 Discovery — Financial Controls & Operational Integrity

Read-only discovery, per the explicit M17 instruction: no code was changed
while producing this document. Every claim below is tagged **FACT**
(verified directly in current code/tests), **INFERENCE** (reasoned from
FACTs but not itself directly executed), or **UNKNOWN** (a real ambiguity,
not resolved by inventing a policy).

## 0. Exact current state (recorded before any M17 work)

**FACT** (all independently re-run this session, not assumed from M16's report):

| Item | Value |
|---|---|
| Branch | `claude/grocery-erp-pos-architecture-h8a53g` |
| HEAD | `f762cb1` |
| Alembic head | `4a83c462dbff` (single head) |
| Backend tests | `pytest -q` → **930 passed** |
| Frontend tests | `npx vitest run` → **49 passed** (14 files) |
| Deploy-infra tests | 22/22 (the 5 files CI's `deploy-infra` job runs) → **22 passed** in 88.18s |
| Backend lint/format/type | `ruff check .`, `black --check .`, `mypy app` → all clean |
| Frontend lint/format/type | `npx tsc -b`, `oxlint`, `prettier --check` → all clean |
| Working tree | clean, no untracked files |
| CI (last verified, run 35824015913) | backend/frontend/deploy-infra all `completed`/`success` |

These numbers exactly match M16's final report — nothing has drifted
between M16's close and the start of M17.

## 1. Method

Read, function-by-function (not skimmed), the current implementations of:
`app/modules/ap/service.py` (2359 lines), `app/modules/shifts/service.py`
(585 lines), `app/modules/sales/service.py` (1231 lines, return/void
paths), `app/modules/inventory/service.py` (948 lines, adjustment/count
paths), `app/modules/accounting/service.py` (reversal-journal and
reconciliation functions), `app/modules/accounting/models.py`
(`AUTOMATED_SOURCE_TYPES` and its extensive inline design-rationale
comments), and cross-referenced every finding against the actual test
files that claim to cover it (not just that a test file with a plausible
name exists).

## 2. Gap matrix

### A. Supplier payment correction/reversal — **SOLID, not a gap**

**FACT**: `reverse_supplier_payment` (ap/service.py:1256) is idempotent-by-existence,
store-isolated (`_enforce_store_access`), audit-logged, requires a non-empty
`reason`, and posts a dedicated `SUPPLIER_PAYMENT_REVERSAL` journal entry
via `post_supplier_payment_reversal_journal` — never through the generic
`reverse_journal_entry` (`SUPPLIER_PAYMENT` is in `AUTOMATED_SOURCE_TYPES`).
Genuine multi-connection concurrency proof exists
(`test_ap_reversal.py`'s `test_two_concurrent_reversal_attempts_exactly_one_creates_a_reversal_row`,
real `SessionLocal()` + `threading.Barrier`), backed by the DB's own
`UNIQUE(supplier_payment_id)` constraint on `supplier_payment_reversals`.
14 tests total.

### B. Supplier credit-note correction/reversal — **SOLID, not a gap**

**FACT**: `reverse_supplier_credit_note` (ap/service.py:1764) mirrors A
exactly — same idempotency/isolation/audit/dedicated-journal pattern,
same `SUPPLIER_CREDIT_NOTE_REVERSAL` automated source type, same
`UNIQUE(supplier_credit_note_id)` DB backstop.

### C. Cash-shift variance accounting/reversal behavior — **INTENTIONAL DESIGN, documented, not a gap**

**FACT**: `accounting/models.py` explicitly documents, in an inline
comment attached to `CASH_SHIFT_VARIANCE`'s entry in
`AUTOMATED_SOURCE_TYPES`: *"There is deliberately no
CASH_SHIFT_VARIANCE_REVERSAL counterpart yet — M15 builds no
shift-reopen/correction workflow... so nothing in this milestone would
ever call one."* This is a **deferred feature by explicit prior design
decision**, not an oversight. M16 already re-verified (not merely
assumed) that the generic `reverse_journal_entry` correctly refuses this
source type
(`test_shifts.py::test_cash_shift_variance_cannot_be_reversed_via_generic_journal_reversal`).

**INFERENCE**: if a manager mis-enters a shift's closing counted amount,
the only correction path today is a brand-new stock-count-style workflow
that does not exist. This is a real, user-facing operational gap, but
building a "reopen a closed shift" workflow is exactly the kind of
speculative new functionality M17's own instructions forbid inventing
without a confirmed requirement. **Recommendation: report as a limitation,
do not build it in M17** unless the user confirms it's in scope.

### D. Cash movement accounting and audit linkage — **MOSTLY SOLID, one real gap found**

**FACT**: `record_cash_movement` is idempotent (`client_transaction_id`),
locks the shift row (serializing against a concurrent close — proven by
`test_shifts_concurrency.py::test_cash_movement_racing_shift_close_resolves_deterministically`
and `::test_concurrent_duplicate_cash_movement_requests_create_only_one`),
store-isolated, and audit-logged with `movement_id`/`client_transaction_id`
in the audit payload (M16 fix). No reversal exists for an individual cash
movement — by design, matching the append-only-ledger philosophy already
established for `CASH_SHIFT_VARIANCE`; a correction is a new, offsetting
movement, never a mutation or deletion of the original.

**CONFIRMED DEFECT (small)**: `list_cash_movements(db, shift_id)`
(shifts/service.py:576) takes **no `caller_store_id` parameter at all** —
unlike its sibling `get_shift`/`list_shifts`, which M16 Phase 0 item 5
specifically hardened for direct-service-layer store isolation
("defense-in-depth... so a direct service call can't bypass it either").
The HTTP endpoint (`shifts.py`'s `list_cash_movements`) is safe today —
it calls `service.get_shift(..., caller_store_id=...)` first purely for
its isolation check, per an explicit code comment — but a direct service
call to `list_cash_movements` bypasses isolation entirely, unlike every
other financially-sensitive read/write function audited in this
document. This is the one place M16's own stated defense-in-depth
standard for this exact module was not applied consistently.

### E. Stock-adjustment authorization and store isolation — **SOLID for isolation; no reversal concept exists (deferred, not a defect)**

**FACT**: `create_stock_adjustment` (inventory/service.py:221) takes
`caller_store_id` (M16 fix) and enforces it; has a
`client_transaction_id` idempotency key (pre-M15 hardening); genuine
concurrency-tested
(`test_inventory_idempotency.py::test_concurrent_duplicate_adjustment_requests_create_only_one_adjustment`,
real threads); audit-logged; posts its own `STOCK_ADJUSTMENT` journal
entry (in `AUTOMATED_SOURCE_TYPES`, blocked from generic reversal).

**UNKNOWN / not designed**: there is no first-class "reverse this stock
adjustment" operation — a correction is a brand-new adjustment, not
linked back to the original the way `SupplierPaymentReversal` links to
its `SupplierPayment`. No design document (M8 through M16) ever specifies
this as a requirement. **This is not classified as a confirmed defect**
— nothing in the system's documented design ever promised it, and
inventing one now would be exactly the speculative-functionality M17's
own rules forbid. Reported as a limitation.

### F. Shift authorization and store isolation — **SOLID** (see D for the one exception)

**FACT**: `get_shift`/`list_shifts` (M16-hardened), `open_shift`,
`close_shift`, `record_cash_movement` are all store-isolated at the
service layer, independent of the endpoint. `close_shift`'s
self-vs-override authorization is explicitly re-checked in the service
function itself (not just the endpoint), per its own docstring, matching
the belt-and-suspenders pattern `sales.service._resolve_return_approval`
established first.

### G. Sales/returns → GL consistency — **SOLID, not a gap**

**FACT**: `cash_payment_method_summary` (M11, reports/service.py)
already compares operational payment totals against the GL cash/bank
account balance per payment method. `_return_business_date()` (M16)
closed the one real divergence found (a backdated return landing in a
different reporting period than its GL posting), regression-tested by
`test_reports_sales.py::test_backdated_return_lands_in_return_date_period_matching_gl`.
`void_sale` is idempotent, double-void-protected
(`NOTHING_TO_VOID` conflict error), and store-isolated.

### H. Supplier subledger → GL consistency — **MECHANISM SOLID; one real test-coverage gap found**

**FACT**: `ap_reconciliation`/`purchase_clearing_reconciliation`
(ap/service.py:2283/2321) exist since M6/M7, comparing the GL Accounts
Payable / Purchase Clearing control-account balances against the
subledger. `ap_reconciliation`'s own docstring explicitly states it is
"now net of both payments AND credit notes." `_outstanding_balance`
(the subledger's single definition of "what is still owed") reads
`invoice.grand_total - invoice.amount_paid - invoice.amount_credited` —
both of which `reverse_supplier_payment`/`reverse_supplier_credit_note`
correctly decrement on reversal, per direct reading of those functions.
`ap_reconciliation` is proven to be a real, working check, not decorative
(`test_ap_mutation.py::test_mutation_ap_reconciliation_detects_a_corrupted_invoice_total`
deliberately corrupts data and shows the check catches it).

**CONFIRMED TEST-COVERAGE GAP**: no existing test calls
`ap_reconciliation`/`purchase_clearing_reconciliation` after an M16 AP
reversal to prove the GL and subledger remain reconciled. **INFERENCE**
(not yet proven): reconciliation almost certainly still holds, by
construction, since reversal correctly decrements the same fields
`_outstanding_balance` reads — but this is inference, not verification,
and M17's own instruction explicitly requires supplier-subledger/GL
reconciliation to be **tested**, not merely reasoned about. This is
M17's highest-value, lowest-risk target: a new regression test (and,
only if it fails, a real fix).

### I. Idempotency of all financially material reversal/correction operations — **SOLID**

**FACT**: Every reversal/correction-class operation audited
(`reverse_supplier_payment`, `reverse_supplier_credit_note`,
`void_purchase_invoice`, `void_sale`/`create_sale_return`, `close_shift`,
`record_cash_movement`, `create_stock_adjustment`,
`post_payroll_reversal_journal`/`reverse_payroll_period` from M10) uses
one of exactly two idempotency patterns consistently: idempotent-by-
existence (a reversal row for the source id is a domain fact, created at
most once, backstopped by a DB `UNIQUE` constraint) or a
`client_transaction_id` fast-path-lookup pattern. No exceptions found.

### J. Concurrency behavior of all financially material reversal/correction operations — **SOLID**

**FACT**: Genuine multi-session (`SessionLocal()` + `threading`)
concurrency tests exist for: AP payment reversal, shift open, shift close
(including racing a sale finalization and a cash movement against a
close), cash movement recording, and stock-adjustment creation. None of
the concurrency tests found use the "call the same function twice
sequentially" anti-pattern M17's own instructions warn against — all use
real separate sessions/threads/barriers.

### K. Audit trail completeness — **SOLID**

**FACT**: Every mutating function in `ap/service.py` (7
create/post/void/pay/credit/reverse×2 functions), `shifts/service.py`
(6), `inventory/service.py` (10 call sites), and `sales/service.py` (8
call sites) has at least one `audit_service.log_event` call in its body,
confirmed by direct per-function source inspection (not just a whole-file
grep count).

### L. Cross-store IDOR/service-layer isolation — **SOLID, with the one D-noted exception**

**FACT**: Every mutating financial function across AP, shifts, sales,
and inventory takes `caller_store_id` and calls a per-module
`_enforce_store_access` helper. Single-resource READ functions
(`get_purchase_invoice`, `get_sale`, `get_supplier_payment`, etc.)
consistently do **not** take `caller_store_id` at the service layer —
isolation for these is enforced at the endpoint layer instead (a 404 on
cross-store access, matching the established "information-hiding"
convention already documented in M16). This is a **consistent,
intentional, repo-wide pattern**, not a gap — confirmed by checking it
holds identically across AP, sales, and (mostly) shifts. The one
deviation is `list_cash_movements` (see D), which is inconsistent with
its own module's `get_shift`/`list_shifts` siblings specifically, not
with the repo-wide read-isolation convention in general.

### M. Remaining irreversible financial transaction types — **FULLY DOCUMENTED, not an unmapped gap**

**FACT**: `AUTOMATED_SOURCE_TYPES` (accounting/models.py) and its
extensive inline comments account for every source type in the system:
`SALE`/`SALE_RETURN` (reversed via a new return/void, an operational
counter-action, not a journal reversal), `PURCHASE_RECEIPT`/
`PURCHASE_RETURN` (same pattern), `STOCK_ADJUSTMENT` (no first-class
reversal — see E), `PURCHASE_INVOICE`/`PURCHASE_INVOICE_VOID` (void is
its own dedicated, idempotent function — see G's note on `void_sale`'s
AP cousin), `SUPPLIER_PAYMENT`/`SUPPLIER_CREDIT_NOTE` (dedicated
reversal — see A/B), `INTER_STORE_TRANSFER_SHIP`/`_RECEIVE` (M8:
explicitly no reversal, "would not undo the real inventory movement"),
`PAYROLL_POSTING`/`PAYROLL_REVERSAL` (dedicated reversal, M10),
`CASH_SHIFT_VARIANCE` (see C). Every irreversible type has an explicit,
on-the-record design reason in the code itself — this document did not
have to infer any of them.

## 3. Summary: what M17 should actually do

Discovery does not support a large M17 implementation. The financial
core built across M10–M16 already applies one consistent, well-tested
set of patterns (idempotency, row-locking, store isolation, audit
linkage, reconciliation) almost everywhere. Two concrete items warrant
action; two are confirmed intentional and should be reported as
limitations rather than built:

| # | Finding | Classification | M17 action |
|---|---|---|---|
| 1 | `list_cash_movements` lacks service-layer store isolation (§D) | **Confirmed defect** (small, narrow) | Fix: add `caller_store_id`, mirroring `get_shift`/`list_shifts` exactly |
| 2 | No test proves AP reconciliation holds after a reversal (§H) | **Confirmed test-coverage gap** | Add regression test(s); fix only if the test reveals a real discrepancy |
| 3 | No shift-close correction/reopen workflow (§C) | **Deferred feature by explicit prior design decision** | Report as a limitation; do not build |
| 4 | No stock-adjustment reversal/correction linkage (§E) | **Unknown/undesigned, not a confirmed requirement** | Report as a limitation; do not build |

No other confirmed defects were found in Phase 0 discovery. This is
reported as-is, not inflated to justify a larger milestone than the
evidence supports.
