# M19 Discovery — Supply Chain & Vendor Operations Completion

Read-only discovery for M19. Six parallel investigations (Explore agents,
isolated worktrees, no code changes) traced supplier → PO → receiving →
invoicing → AP → payment → statement → accounting, and the parallel
purchase → receiving → valuation → returns/credit chain, against the
actual code and actual tests — not assumed from milestone names or from
this task's own framing. **One agent finding was independently verified
and found to be a factual error** (see §7) — corrected below using direct
grep/read evidence, not taken on either source's word alone.

## 0. Baseline (Phase 0, recorded before any discovery work)

- Branch `claude/grocery-erp-pos-architecture-h8a53g`, HEAD `6c04ed4` (M18 final).
- `origin/main` at `052a250`. Working tree clean. Migration head: single, `4a83c462dbff`.
- Backend `pytest -q` → **936 passed**. Frontend `npx vitest run` → **49 passed**.
- Deploy-infra: docker-postgres subset (24 tests) re-run locally → **24 passed**; full 73/73 already confirmed green in CI (run 35840939620).
- Migration tests → **16 passed**. `ruff`/`black`/`mypy` — all clean.
- CI on this exact HEAD: run `35840939620`, `status=completed`, `conclusion=success`, all 4 jobs green.

## 1. Source-of-truth map (Phase 1)

| Business fact | Authoritative source | Evidence |
|---|---|---|
| Supplier master data | `purchasing.Supplier` table (global, not store-scoped) | `purchasing/models.py:36-88`; no `store_id` column, documented M3 decision |
| Purchase order state | `PurchaseOrder.status` (DB CHECK-constrained) | `purchasing/models.py:94-97` |
| Received quantity (running total) | `PurchaseOrderItem.quantity_received` | `purchasing/models.py:153`, incremented by `receive_goods` |
| Physical receipt event | `GoodsReceipt`/`GoodsReceiptItem` | `purchasing/models.py:168-224` |
| Product cost/on-hand qty | `Product.current_cost`/`current_qty_on_hand`, mutated only by `record_movement` when a caller explicitly passes `new_product_cost` (receiving only) | `inventory/service.py:135-188`, confirmed sole caller is `receive_goods` |
| Invoiced quantity (running total) | `PurchaseOrderItem.quantity_invoiced` | validated/incremented in `ap/service.py::post_purchase_invoice` |
| Invoice-to-receipt-lot matching | `PurchaseInvoiceReceiptMatch` (FIFO-sliced) | `ap/models.py:177-219`, `ap/service.py::_fifo_match_slices` |
| AP subledger balance per invoice | `PurchaseInvoice.amount_paid`/`amount_credited`/`grand_total` (`_outstanding_balance = grand_total - amount_paid - amount_credited`) | `ap/service.py:112-116` |
| GL AP control balance | `journal_entries`/`journal_lines` via `trial_balance` | `ap/service.py::ap_reconciliation` compares this against the subledger sum |
| Purchase Price Variance | Posted GL journal lines at posting time (never independently recomputed) | `reports/service.py::purchase_price_variance_report` reads GL directly |
| Purchase spend | `GoodsReceiptItem.quantity_received * unit_cost`, aggregated fresh per report call | `reports/service.py::purchase_spend_by_*`, no separate spend ledger |
| Supplier statement / running balance | `ap/service.py::get_supplier_statement` — reconstructs from invoice/payment/credit-note event streams, proven algebraically to equal `get_supplier_ap_summary().total_owed` with no date filter | `ap/service.py:2059+`, docstring 2066-2074 |

**No case was found where the same financial fact has two independently
authoritative implementations that could drift.** Every report/summary
function traced either reads the subledger/GL directly or composes
another already-authoritative function (`kpi_dashboard` calls
`ap_aging`/`purchase_spend_by_store` rather than recomputing).

## 2. Supplier master data (Phase 2)

**Model**: global (no `store_id`), documented M3 decision matching
`ProductCategory`/`TaxRate` precedent (`purchasing/models.py:36-53`).
Uniqueness on `code` only (partial unique index). `create_supplier`/
`update_supplier`/`set_supplier_active` correctly take no
`caller_store_id` (global data has no store to isolate against).

**Confirmed gaps**:
- `Supplier.default_payment_terms_days` (added M6, `models.py:87`, read
  by `ap/service.py:354`) has **no write path anywhere** — absent from
  `SupplierCreate`/`SupplierUpdate` schemas and from
  `create_supplier`/`update_supplier`'s field lists. Effectively
  unsettable except by direct DB/ORM write.
- No backend test ever calls `POST /suppliers/{id}/activate` or
  `/deactivate`, despite both being wired, permission-checked, audited
  routes.
- No frontend Edit UI for suppliers — `PUT /suppliers/{id}` has zero
  caller in `SuppliersPage.tsx`/`api/purchasing.ts`.
- `SuppliersPage.test.tsx`'s deactivate test only exercises the
  confirm-cancelled path (`window.confirm` mocked to `false`) — the
  actual deactivation call is never tested end-to-end.

## 3. Purchase order lifecycle (Phase 3)

States: `DRAFT → ORDERED → PARTIALLY_RECEIVED/RECEIVED`, plus
`CANCELLED` from any of `{DRAFT, ORDERED, PARTIALLY_RECEIVED}`
(`purchasing/models.py:94-97`, `service.py:49`). No approval workflow —
`submit_purchase_order` is a single-actor DRAFT→ORDERED transition
gated by the same `purchasing.write` permission that creates the PO.
No `PURCHASING_APPROVE` permission constant exists, unlike parallel
M9/M10/M15 workflows that do have one. Well-tested: duplicate
submission rejected, invalid transitions rejected, cancel-after-full-
receipt blocked, store/supplier isolation (3 dedicated tests), over-
receipt allowed-and-flagged (documented M3 policy), genuine multi-
session concurrency for receiving (5 real-thread tests).

**Confirmed gaps**:
- `PurchaseOrderCreate` has **no idempotency key** (`client_transaction_id`)
  — unlike `GoodsReceipt`/`PurchaseReturn`/`PurchaseInvoice`/
  `SupplierPayment`/`SupplierCreditNote`, all five of which have one.
  A retried/duplicated PO-creation request creates two separate DRAFT
  POs (auto-generated purchase numbers can't collide, so nothing
  detects it). No test exercises this.
- Cancel-after-*partial*-receipt is allowed by the status constraint
  but has no test proving its actual behavior (only cancel-after-*full*-
  receipt is tested and correctly blocked).
- No concurrent-submit test / no visible row lock in
  `submit_purchase_order` (unlike `receive_goods`, which explicitly
  locks the PO row).
- No edit-PO-lines endpoint exists at all (a DRAFT's items cannot be
  modified after creation despite one docstring's claim they are
  "freely re-creatable until submit").

## 4. Receiving (Phase 4)

`receive_goods` (`purchasing/service.py:437-648`) is a single DB
transaction: store/PO-status validation → row-locks the PO → per-line
quantity/cost validation → WAC computation → `InventoryMovement` +
product cost/qty update → `GoodsReceiptItem` → PO running-total update
→ status advance → audit log → accounting journal, all sharing one
uncommitted session, one caller-side commit. Idempotency: real DB
UNIQUE `client_transaction_id`, fast-path pre-check plus
`IntegrityError` recovery for genuine races — proven by a real
multi-thread test creating exactly one receipt from a duplicate-key
race. Partial receiving fully supported via a running-total column,
proven under real concurrency (two threads partially receiving the
same line, no lost update). Zero/negative quantity rejected at both DB
CHECK and service level; zero-cost receipt explicitly allowed
(free/promotional stock) and correctly posts no journal entry.

**Confirmed gap**: **no `test_purchasing_failure_injection.py`** — no
test injects a mid-`receive_goods` failure (e.g. forcing the accounting
post or a second line's inventory write to throw) to prove full
rollback, despite the module's own docstring making an explicit BR-3
atomicity claim and every sibling transactional flow (sales, AP, M8
stock counts) having exactly this kind of test. Corroborated
independently by two separate discovery agents.

## 5. WAC / inventory valuation (Phase 8, folded in here for cohesion)

Formula (`inventory/service.py::compute_new_wac`, lines 48-77):
`(existing_qty*existing_wac + received_qty*received_unit_cost) / (existing_qty+received_qty)`,
recomputed fresh each receipt (not incrementally adjusted), `Decimal`-only
throughout (`Numeric(14,6)`/`Numeric(14,3)` columns, `ROUND_HALF_UP` to
a fixed quantum, applied once at the end to bound rounding drift). Zero-
stock edge case falls back to incoming cost rather than dividing by
zero. Matches the documented blueprint formula exactly, proven against
the exact worked example (100@10 + 50@14 → 11.333333) and a real
concurrency test showing order-independent correctness.

**Confirmed architectural rule, verified by reading every call site**:
only `receive_goods` ever passes `new_product_cost` to `record_movement`
— stock adjustments, purchase returns, sales, and sale returns all move
quantity but never recompute WAC. This is a single, consistently
enforced rule, not an inconsistency, and is explicitly documented on
`PurchaseReturn`'s own model docstring.

**Confirmed gap**: no test targets fractional/`is_weighed` quantities or
the 6th-decimal `ROUND_HALF_UP` rounding boundary specifically, though
the schema (`Numeric(14,3)` quantity columns) and formula fully support
it.

## 6. Purchase invoice / receiving reconciliation (Phase 5)

Three-way matching (PO vs. receipt vs. invoice) **does exist**, enforced
at **posting** time, not creation time. `PurchaseInvoiceReceiptMatch`
links invoice lines to specific receipt lots via a FIFO walk
(`_fifo_match_slices`), carrying `matched_quantity`/`matched_unit_cost`/
`variance_amount`. `create_purchase_invoice` does not require a prior
receipt (invoice-before-receipt is allowed at DRAFT stage — "no
accounting effect, freely re-creatable"); `post_purchase_invoice`
checks `quantity_invoiced + requested <= quantity_received` and raises
`OVER_INVOICING` if it would exceed — proven by a test citing the exact
PO=100/Received=80/Invoice=100 example from the M6 design doc. Price is
never a rejection condition — every price is accepted and the variance
is computed per matched slice and posted to a Purchase Price Variance
GL account. Duplicate invoice number is blocked by two independent
mechanisms (idempotency-key dedup + a DB UNIQUE constraint on
`(supplier_id, invoice_number)`). Partial invoicing (invoice less than
received) is supported and tested.

No gaps found beyond the frontend one noted in §11 (void never exposed
in the UI).

## 7. Supplier payments, credit notes, reversals, and statements (Phase 7)

**Correction of a discovery-agent factual error, independently
re-verified**: one investigation reported that `reverse_supplier_payment`/
`reverse_supplier_credit_note` "do not exist anywhere in this codebase."
This is **wrong** — directly confirmed by grep and by reading the code
just now: both functions exist and are fully implemented
(`ap/service.py:1256` `reverse_supplier_payment`, `:1764`
`reverse_supplier_credit_note`), backed by `SupplierPaymentReversal`/
`SupplierCreditNoteReversal` models (`ap/models.py:367,394`) and
dedicated test files (`test_ap_reversal.py`, plus reversal-specific
cases in `test_ap_reconciliation.py`). This functionality was built and
hardened across M16/M17 of this same engagement: idempotent-by-existence
(a `SupplierPaymentReversal` row is a domain fact, never created twice,
backed by a DB UNIQUE constraint), row-locking that mirrors
`record_supplier_payment` itself, `_recompute_invoice_status` reused so
every derived figure (balance, aging, statement) reflects the correction
automatically, and M17 added dedicated post-reversal reconciliation
tests (`test_ap_reconciliation_holds_after_supplier_payment_reversal`,
`::_after_supplier_credit_note_reversal`). **This is solid, existing,
tested work — not a gap, and not M19 scope.** The likely root cause of
the erroneous report: the generic `reverse_journal_entry` function
correctly *refuses* to reverse a `SUPPLIER_PAYMENT`/`SUPPLIER_CREDIT_NOTE`
journal entry directly (since both source types are in
`AUTOMATED_SOURCE_TYPES`) — an agent reading only that refusal, without
also finding the dedicated operational reversal functions, would
incorrectly conclude reversal is unbuilt.

**Supplier statement**: a dedicated, tested `get_supplier_statement`
function and `GET /ap/suppliers/{id}/statement` endpoint already exist
(§1) — this is not merely implicit in `ap_reconciliation`, it is a
first-class function proven algebraically to reconcile with
`get_supplier_ap_summary`, with a regression test for a real bug once
caught (same-day events ordered by `created_at`, not row id, across
three independently-sequenced tables).

**AP test inventory**: 8 files, 3,735 lines, 65 test functions — invoice
lifecycle (18), payments (9), API/RBAC/isolation (15), mutation-testing
(7), reconciliation (3), concurrency (5 real-thread tests), failure-
injection (7), one full e2e scenario. This is the most thoroughly
tested subsystem found in this discovery.

**Confirmed gap**: `void_purchase_invoice` is defined in the frontend
API client (`api/ap.ts:128-131`) but never called from
`AccountsPayablePage.tsx` — voiding an invoice is API-only today, no UI
affordance.

## 8. Purchase returns / supplier credit (Phase 6)

`create_purchase_return` (`purchasing/service.py:686-823`): store
isolation (three redundant layers — caller-store check, PO-store match,
per-product store match post-lock), idempotency (fast-path + DB UNIQUE
`IntegrityError` recovery, tested), decreases inventory via
`InventoryMovement`, posts accounting, writes an audit event. A
`PurchaseReturn` links only to a `PurchaseOrder`, never to a specific
`GoodsReceipt`/lot — the model docstring documents this as a *cost-basis*
limitation (return cost = current WAC, not the original lot cost, "no
per-receipt-lot tracking").

**Confirmed gap, broader than the documented limitation**: the service
layer validates return quantity only against the product's **current
on-hand stock**, never against what was actually **received on that
specific PO** (`PurchaseOrderItem.quantity_received`). A user can
record a return against an unrelated PO for the same store, as long as
current stock covers it — there is no cross-check against
`GoodsReceiptItem` at all. This is a real traceability gap the model
docstring doesn't fully admit to.

**`PurchaseReturn` ↔ `SupplierCreditNote` linkage**: confirmed, by
reading the full import graph, to be an entirely manual two-step
workflow — creating a return never auto-creates a credit note. The DB
enforces *consistency* when both exist (a `GOODS_RETURN`-reason credit
note must reference a `purchase_return_id`, CHECK-constrained), but
nothing forces a credit note to ever be created for a given return — an
uncredited return is a permanently valid state. This is likely
intentional (matches the manual settlement pattern elsewhere in AP) but
was not found explicitly documented anywhere.

**Confirmed gaps** (all corroborated by a second agent independently):
- No cross-store isolation test specific to `create_purchase_return`,
  `record_supplier_payment`, or `create_supplier_credit_note` (unlike
  PO create/read/receive and invoice create/lookup, which each have
  one) — enforcement code exists for all three, but is untested at this
  specific granularity.
- **Zero genuine multi-session concurrency coverage for
  `create_purchase_return`** — no test races two concurrent returns, or
  a return against a concurrent sale/receipt/adjustment, unlike every
  other high-stakes mutation in this domain (receiving has 5 such
  tests, AP has 5).
- No failure-injection test for `create_purchase_return` (same gap
  pattern as receiving, §4).

## 9. Multi-store isolation (Phase 9)

Every mutating, store-scoped function in `purchasing/service.py` and
`ap/service.py` was checked directly: all 10 (PO create/submit/cancel/
receive/return, invoice create/post/void, payment, credit note) take a
`caller_store_id` and call `_enforce_store_access` (duplicated
verbatim, not imported, in both modules — matches the established
sales-module pattern). `Supplier` is correctly unscoped (global data).
**No enforcement gaps found in the code itself** — only test-coverage
gaps for 3 of the 10 functions (§8).

## 10. Idempotency and concurrency (Phase 10)

Per-operation summary (four financially/inventory-material writes):

| Operation | Idempotency column | Idempotency test | Real concurrency test | Failure-injection test |
|---|---|---|---|---|
| Goods receipt (`receive_goods`) | Yes | Yes | **Yes** (duplicate-key race) | **No** |
| Purchase invoice creation | Yes | Yes | **No** (only *posting* is raced, not creation-dedup) | **No** |
| Supplier payment (`record_supplier_payment`) | Yes | Yes | **Yes** | **Yes** |
| Purchase return (`create_purchase_return`) | Yes | Yes | **No** | **No** |

All five write-path entities have the idempotency column at the schema
level — every gap above is a test-coverage gap, not a schema gap.
`test_purchasing_concurrency.py` and `test_ap_concurrency.py` are the
only two files with real `threading.Thread` usage in this domain (10
genuine multi-session tests total); `test_store_isolation.py` (despite
its generic name) covers only products/inventory/sales, none of
purchasing/AP.

## 11. Frontend procurement UI (Phase 11)

Four pages exist: `SuppliersPage.tsx`, `PurchasingPage.tsx`,
`AccountsPayablePage.tsx`, `SupplyChainPage.tsx` (a replenishment/
reordering dashboard, not a generic procurement page). All four have
loading/error/empty states. `PurchasingPage.tsx`'s receive-goods panel
correctly implements the client half of the idempotency pattern
(`crypto.randomUUID()` held in a ref, persisted across a failed
attempt, cleared only after success).

**Confirmed frontend money arithmetic** (per the milestone's explicit
"never do money arithmetic in the frontend" instruction):
- `PurchasingPage.tsx:228-232` — receipt-total estimate, **display-only**,
  not submitted to the API (raw quantity/cost sent instead).
- `AccountsPayablePage.tsx:164` — payment-total sum of allocation
  amounts, **is submitted** to `POST /ap/payments` as the `amount`
  field. The backend independently validates/derives the real total
  from the allocations server-side regardless (confirmed by the AP
  discovery: allocations-must-sum-to-amount is enforced and tested at
  `ap/service.py`), so this is a UX convenience sum of user-entered
  values feeding a field the server re-validates, not a price/cost
  calculation the client is trusted for — lower-risk than genuine
  money-math, but flagged here for completeness per the explicit
  instruction.
- `AccountsPayablePage.tsx:254-261` — variance/matched-cost, **display-
  only**, explicitly commented in the component as non-authoritative
  UI recomputation of server-returned data.

**Confirmed gap**: `SupplyChainPage.tsx` has **zero test coverage under
any filename**, despite having real permission-gated workflow logic
(approve/execute/cancel a replenishment plan, status-dependent button
visibility) — the only one of the four procurement pages with no test
file at all.

## 12. Purchasing/vendor reporting (Phase 12)

All purchasing/AP report functions found in `reports/service.py`
(`purchase_spend_by_{supplier,store,product}`, `po_fulfillment`,
`supplier_delivery_performance`, `purchase_price_variance_report`) and
`ap/service.py` (`ap_aging`) trace to authoritative sources with **no
duplicated calculation found anywhere** — `purchase_price_variance_report`
explicitly reads already-posted GL journal lines rather than
recomputing variance independently; `ap_aging` reads the invoice
subledger's own stored balance fields. Every function is wired to a
real endpoint and surfaced in the UI (`ReportsPage.tsx`'s
`PurchasingTab`, or `AccountsPayablePage.tsx`'s `SupplierTools` for AP
aging/statement) — **no unreachable/dead report code found.**

## 13. Summary — confirmed gaps mapped to M19 phases

| Gap | Severity | M19 phase |
|---|---|---|
| No idempotency key on `PurchaseOrderCreate` | Real, untested duplicate-PO risk | Design + implement (small) |
| `create_purchase_return` validates only against on-hand stock, not against what was actually received on that PO | Real traceability gap | Design + implement |
| No failure-injection test for `receive_goods`, `create_purchase_invoice`, `create_purchase_return` | Test-coverage gap on financially-material paths | Phase 15 |
| No real concurrency test for `create_purchase_return` or purchase-invoice-creation-dedup | Test-coverage gap | Phase 10 |
| No cross-store isolation test for `create_purchase_return`/`record_supplier_payment`/`create_supplier_credit_note` | Test-coverage gap (enforcement code is correct) | Phase 9 |
| `Supplier.default_payment_terms_days` has no write path | Dead field | Small fix |
| No supplier activate/deactivate backend test; no frontend Edit UI | Test/UI gap | Phase 2 / 11 |
| `SupplyChainPage.tsx` has zero test coverage | UI test gap | Phase 11 |
| `void_purchase_invoice` has no frontend affordance | Minor UI gap | Phase 11 (optional) |
| Cancel-after-partial-receipt untested | Test-coverage gap | Phase 3 |
| No WAC test for fractional/weighed quantities | Minor test-coverage gap | Phase 8 |

## 14. Non-goals / confirmed-solid, no M19 work needed

Supplier payment/credit-note reversal (§7 — already built, tested,
hardened across M16/M17). Three-way invoice matching (§6 — already
built, posting-time enforcement, well-tested). Supplier statement (§7 —
already a first-class function). WAC core formula and Decimal
discipline (§5 — correct and matches the documented blueprint). Multi-
store isolation *enforcement code* (§9 — complete; only test coverage
has gaps). Reporting duplication (§12 — none found). No approval
workflow on POs is treated as an intentional M3 scope decision, not a
defect — building one now without a confirmed requirement would be
exactly the speculative functionality this milestone's own instructions
forbid.
