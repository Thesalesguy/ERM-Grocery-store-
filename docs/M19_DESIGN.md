# M19 Design — Supply Chain & Vendor Operations Completion

Companion to `docs/M19_DISCOVERY.md`. Per this milestone's explicit
instruction ("do not add speculative enterprise procurement features...
implement only what the existing repository, mission, and verified
discovery support"), this document covers exactly three confirmed code-
level defects — not new features — plus the test-coverage additions
discovery identified. Everything else discovery found is either already
solid (no work needed) or a documented, intentional design decision
(not revisited without evidence).

## 1. Purchase order creation has no idempotency protection

**Problem** (`M19_DISCOVERY.md` §3): every other mutating write in this
domain — `GoodsReceipt`, `PurchaseReturn`, `PurchaseInvoice`,
`SupplierPayment`, `SupplierCreditNote` — has a `client_transaction_id`
unique column and an idempotent-by-existence + `IntegrityError`-recovery
pattern. `PurchaseOrder` does not. A retried/duplicated
`POST /purchase-orders` (double-click, network retry, client timeout-
then-retry) silently creates two separate DRAFT purchase orders with
different auto-generated purchase numbers — nothing detects or prevents
it.

**Design**: add `client_transaction_id: str, unique, NOT NULL` to
`PurchaseOrder`, exactly mirroring `GoodsReceipt`'s own column and
docstring. `PurchaseOrderCreate` gains a required
`client_transaction_id: str` field. `create_purchase_order`/
`_create_purchase_order_inner` gains the same two-part idempotency
check already proven in `receive_goods`/`create_purchase_return`: a
fast-path lookup by the key returns the existing PO unchanged if found;
an `IntegrityError` on insert (a genuine concurrent duplicate that lost
the fast-path race) is caught and the winning row is returned instead
of propagating the error.

**Migration**: existing `purchase_orders` rows have no
`client_transaction_id`. Follows the exact safe pattern this codebase
already established for adding a NOT NULL column to a populated table
(`c82162efb3af`/`35d411b947ec`, M18 discovery's own confirmed evidence):
add nullable → backfill existing rows with a synthetic, guaranteed-
unique value (`'legacy-' || id::text`, cannot collide with any future
client-generated UUID) → set NOT NULL. Downgrade drops the column
(no data-loss guard needed — the column carries no independent business
meaning to preserve, unlike a financial ledger column).

**Regression tests**: sequential duplicate-request test (mirrors
`test_purchasing_idempotency.py`'s existing pattern for receipts/
returns) and a real multi-thread concurrent-duplicate test (mirrors
`test_purchasing_concurrency.py`'s `test_f_concurrent_duplicate_receipt_requests_create_only_one_receipt`).

**Frontend regression found during final CI verification (not caught by
the original test pass)**: making `client_transaction_id` a required
backend field broke the ONE real caller that matters — the actual
browser app. `frontend/src/api/purchasing.ts`'s `PurchaseOrderCreateInput`
type never declared the field, and `PurchasingPage.tsx`'s
`NewPurchaseOrderForm` never sent one; every real "New purchase order"
submission through the UI would have received a 422 from the backend.
This was invisible to the existing frontend test suite because
`PurchasingPage.test.tsx` had no test at all for the create-PO form (only
list/detail/submit/receive were covered) — the gap wasn't a wrong
assertion, it was a missing one. Found and fixed while exhaustively
searching every purchase-order creation call site in the repository
(not just backend/deploy Python files) per the final-validation pass;
fixed with the exact same `idempotencyKeyRef` pattern this file's own
`ReceiveGoodsForm` already established (`useRef<string | null>(null)`,
generate on first attempt, reuse across a retry of the same attempt,
reset only after success). A new regression test
(`PurchasingPage.test.tsx`'s "creates a new purchase order via the form
with a client_transaction_id") asserts the actual outgoing request body
carries a non-empty `client_transaction_id`, not just that the mocked
call resolves.

## 2. Purchase return validates only against on-hand stock, not against what was actually received on that PO

**Problem** (`M19_DISCOVERY.md` §8): `create_purchase_return` checks
`quantity <= product.current_qty_on_hand` but never checks the
requested return quantity against `PurchaseOrderItem.quantity_received`
for the specific PO being returned against. A user can record a return
against any PO for the same store/product as long as current stock
happens to cover it — including a PO that never actually received that
product, or one that received less than is being "returned" against it.

**Design**: after loading the `PurchaseOrder` and before locking
product rows, compute, per distinct product in the request:
`received = Σ PurchaseOrderItem.quantity_received` for that product on
that PO, and `already_returned = Σ PurchaseReturnItem.quantity` across
every prior `PurchaseReturn` against that same PO for that product.
Reject with a new `ConflictError(error_code="RETURN_EXCEEDS_RECEIVED_QUANTITY")`
if `already_returned + requested > received`. The `PurchaseOrderItem`
rows involved are locked `FOR UPDATE` in the same query (sorted by
`product_id`, matching the existing ascending-lock-order discipline used
for `Product` rows in this same function) — this closes both the
traceability gap and, as a side effect, the concurrency gap discovery
flagged (two concurrent returns against the same PO/product now
serialize on the same row lock `receive_goods` already uses for the
`PurchaseOrder`-item relationship).

**Non-goal**: this does not add per-receipt-lot tracking (the
already-documented, accepted cost-basis limitation on `PurchaseReturn`
stays exactly as-is — a return still costs at current WAC, not the
original lot's cost). The fix is scoped to quantity traceability against
the PO as a whole, matching the granularity the schema already
operates at (`PurchaseReturnItem` has no `goods_receipt_item_id`), not
inventing a new lot-tracking concept.

**Regression tests**: return exceeding received-on-this-PO quantity is
rejected even when on-hand stock would otherwise allow it; return of
exactly the received quantity succeeds; two sequential partial returns
summing to more than received is rejected on the second one; a genuine
multi-thread concurrency test proving two concurrent returns against
the same PO/product can't jointly exceed the received quantity.

## 3. `Supplier.default_payment_terms_days` has no write path

**Problem** (`M19_DISCOVERY.md` §2): the column exists (added M6) and
is read by `ap/service.py:354` (`term_days = supplier.default_payment_terms_days or 0`),
but is absent from `SupplierCreate`/`SupplierUpdate` and from
`create_supplier`/`update_supplier`'s field lists — it can never be set
through the API, only by writing to the DB/ORM directly (as tests
already do via a factory override).

**Design**: add `default_payment_terms_days: int | None = None` to both
`SupplierCreate` and `SupplierUpdate`, with a `ge=0` validator (a
negative payment-terms period is meaningless), and thread it through
`create_supplier`/`update_supplier`'s field assignment — no other
service-layer logic changes, since the field is already correctly
consumed downstream. No migration needed (column already exists).

**Regression tests**: creating a supplier with `default_payment_terms_days`
set persists and round-trips through `SupplierRead`; updating it changes
the value read by AP's due-date calculation; a negative value is
rejected.

## 4. Test-coverage additions (no code change — existing behavior is correct, only unproven)

Per discovery's confirmed gaps, the following tests are added without
any corresponding service-layer change, because the underlying code was
independently verified correct by this milestone's discovery — these
close a proof gap, not a behavior gap:

- **Failure injection** for `receive_goods`, `create_purchase_invoice`,
  `create_purchase_return` (Phase 15) — mirrors the existing pattern in
  `test_ap_failure_injection.py`/`test_sale_finalization_failure_injection.py`:
  monkeypatch a downstream step (accounting post, audit log) to raise,
  assert zero rows survive across receipt/invoice/return + inventory
  movement + PO running-total + accounting.
- **Real concurrency test** for purchase-invoice-creation dedup (not
  just posting) — two threads calling `create_purchase_invoice` with
  the same `client_transaction_id`.
- **Cross-store isolation tests** for `create_purchase_return`,
  `record_supplier_payment`, `create_supplier_credit_note` — the
  enforcement code is already correct (verified by discovery); these
  tests prove it, matching the existing dedicated-isolation-test pattern
  already used for PO create/read/receive and invoice create/lookup.
- **Supplier activate/deactivate** backend test (never exercised).
- **WAC fractional-quantity test** (e.g. receiving 2.375 kg of a
  weighed product across two receipts at different costs) — proves the
  existing `compute_new_wac` formula (already verified correct by
  discovery) holds at fractional precision, not just whole units.
- **`SupplyChainPage.tsx`** frontend test file (currently has zero
  coverage under any name) — covers the replenishment-plan
  approve/execute/cancel workflow and its permission-gated button
  visibility, mirroring the existing test structure of the other three
  procurement pages.
- **Cancel-after-partial-receipt** test for `cancel_purchase_order` —
  proves the already-correct status-constraint behavior (PARTIALLY_RECEIVED
  is in `_CANCELLABLE_PO_STATUSES`) and documents what happens to
  already-received inventory when a partially-received PO is cancelled
  (it is retained — cancellation stops future receiving, it does not
  reverse what already physically arrived, matching this codebase's
  append-only-ledger philosophy applied elsewhere).

## 5. `create_purchase_invoice`'s IntegrityError recovery checked the wrong thing first (found during §4's concurrency test, not pre-scoped)

**Problem**: while writing §4's "real concurrency test for
purchase-invoice-creation dedup," the test failed deterministically (not
flaky) — two threads submitting a create-invoice request with the SAME
`client_transaction_id` (and, as a real retry naturally would, the same
`invoice_number` too) did not both resolve to the same invoice. The
loser reliably received `DUPLICATE_SUPPLIER_INVOICE_NUMBER` instead of
transparently getting back the winner's invoice. Root cause: the
`IntegrityError` recovery block in `create_purchase_invoice` branched on
*which* of the two UNIQUE constraints Postgres happened to report
(`uq_purchase_invoices_supplier_invoice_number` vs.
`client_transaction_id`) before ever checking whether a matching
`client_transaction_id` row actually existed — and under an identical-
values race, the invoice-number constraint fires deterministically
first. Every other module's identical recovery block (`receive_goods`,
`create_purchase_return`, `create_purchase_order`) checks for the
idempotent match FIRST, unconditionally; this was the one exception.

**Design**: reorder the recovery block to check for a `client_transaction_id`
match first, regardless of which constraint fired; only raise
`DUPLICATE_SUPPLIER_INVOICE_NUMBER` if no such match exists. This is a
narrow, mechanical reordering of an existing exception handler — not a
redesign of invoice-creation logic, and it changes behavior only in the
race case the new test exercises (a genuine duplicate-number rejection
between two DIFFERENT requests is unaffected, still correctly rejected).

**Regression tests**:
`test_ap_concurrency.py::test_f_two_concurrent_invoice_creations_with_same_idempotency_key_create_exactly_one`
(the real two-thread race that found this) and
`tests/test_ap_mutation.py::test_mutation_removing_invoice_idempotency_fast_path_still_dedupes_via_integrityerror_recovery`
(rewritten — it previously asserted the buggy behavior, i.e. that
disabling the fast-path lookup alone broke retry transparency; it now
asserts the fixed, correct behavior: the IntegrityError-recovery path is
a real second line of defense, so retry transparency survives even with
the fast path disabled).

## Non-goals (confirmed by discovery, not revisited)

Supplier payment/credit-note reversal — already fully built, tested,
and hardened across M16/M17; discovery's own initial read of one
investigation incorrectly claimed this was missing, independently
verified and corrected (`M19_DISCOVERY.md` §7). No PO approval
workflow — an intentional M3 scope decision; inventing one now without
a confirmed business requirement would be exactly the speculative
functionality this milestone's instructions forbid. No automatic
`PurchaseReturn` → `SupplierCreditNote` linkage — the manual two-step
pattern matches how settlement works elsewhere in AP and was not found
documented as a defect anywhere; not changed. No per-receipt-lot cost
tracking for returns — accepted limitation, unchanged (see §2's
non-goal). No frontend void-invoice button — a minor, optional UI gap;
deferred rather than built speculatively, since `void_purchase_invoice`
already has real, tested backend behavior and an unused API client
method is a low-severity gap, not a functional defect.
