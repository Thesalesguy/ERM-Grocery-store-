# M3 — Purchasing, Goods Receiving, WAC & Supplier Workflow: Design & Decisions

This document records the M3 milestone's design decisions and explains the
supplier, purchase order, goods receiving, and purchase return workflows
added to `backend/app/modules/purchasing/*` and
`backend/app/api/v1/endpoints/purchasing.py`. Read alongside
`docs/TECHNICAL_BLUEPRINT.md`, `docs/M1_DATABASE_DESIGN.md`, and
`docs/M2_AUTH_AND_POS.md`, which remain authoritative for anything this
document doesn't override or refine.

M3's objective: let a real store manager order stock from a supplier,
receive it (in full or in parts, exactly or over/under), and have that
receipt update on-hand quantity and weighted-average cost (WAC) correctly
under concurrent, duplicate, and adversarial conditions — the system's
first workflow where a bug creates a real financial loss (wrong stock,
wrong cost, or double-counted stock) rather than a wrong screen.

---

## 1. Pre-implementation architecture review

Before writing any M3 code, the existing M1/M2 purchasing schema
(`purchase_orders`, `purchase_order_items`, `goods_receipts`,
`goods_receipt_items`, `purchase_returns`, `purchase_return_items`,
`suppliers`), the WAC implementation (`inventory/wac.py`,
`inventory_service.compute_new_wac` / `record_movement`), and `sales/
service.py`'s `finalize_sale` (M2's model for a safe, audited,
idempotent, row-locked financial transaction) were read in full. One
architectural gap was found and is reported here per the task's "stop and
report" instruction rather than silently worked around:

- **`receive_goods` had a lost-update race on `PurchaseOrderItem.
  quantity_received`.** The M1 implementation read each item's current
  `quantity_received`, computed a new total in Python, and wrote it back
  without ever taking a lock that would serialize two concurrent receipts
  against the *same purchase order*. Two receivers submitting receipts
  for the same PO at the same time could each read the same starting
  `quantity_received`, and the second write would silently clobber the
  first — an under-counted receipt with no error, no audit trail
  showing anything unusual, and stock that doesn't reconcile against the
  ledger. This is exactly the class of silent financial bug the task
  asked to be found before building on top of it. Fixed (see §5) by
  locking the parent `PurchaseOrder` row `FOR UPDATE` before any read of
  `quantity_received`, and verified as a real, necessary fix by
  temporarily removing the lock and observing the regression test
  (`test_b_two_concurrent_receipts_against_the_same_purchase_order`) fail
  intermittently, then restoring it and re-confirming clean.
- Everything else — the WAC formula in `inventory/wac.py`, `record_
  movement`'s inventory-ledger design, the existing `PurchaseOrder`/
  `GoodsReceipt` status enums, and M2's store-scoping/RBAC/audit
  conventions — was extended, not replaced, per the task's explicit
  instruction not to redesign working architecture.

---

## 2. Supplier model: global, not store-scoped

**Decision: `Supplier` is global reference data, not store-scoped.**

This follows the existing precedent set by `ProductCategory` and
`TaxRate` (also global) rather than `Product`/`Sale`/`PurchaseOrder`
(store-scoped). Rationale:

- A supplier is a business relationship ("Acme Distributors"), not a
  location-specific fact. A multi-store operator buying from the same
  distributor across stores should see and reuse one supplier record,
  not re-enter it per store.
- Matches the existing shape of `Supplier.default_supplier_id` on
  `Product`, which already assumed a global supplier ID space.
- `Supplier` has no `created_by`/`updated_by` columns, again matching
  `ProductCategory`'s precedent — the audit log (`SUPPLIER_CREATED`/
  `SUPPLIER_UPDATED`/`SUPPLIER_ACTIVATED`/`SUPPLIER_DEACTIVATED` events)
  is the source of truth for who changed what, not a denormalized column
  on the row itself.
- Added `Supplier.code` (optional, unique when present via a partial
  unique index `uq_suppliers_code ... WHERE code IS NOT NULL`, so
  multiple suppliers can still omit a code) as a human-friendly external
  reference, mirroring `Product.sku`'s optional-uniqueness pattern.

Suppliers are readable/writable by any authenticated user with
`purchasing.read`/`purchasing.write` — there is no store filter on
supplier endpoints, by design.

---

## 3. Purchase order lifecycle

Reused the **existing** DB status enum rather than the task's own
illustrative names, per "use existing naming conventions if equivalent
states already exist":

```
DRAFT → ORDERED → PARTIALLY_RECEIVED → RECEIVED
              \                            ↑
               → CANCELLED  (from DRAFT, ORDERED, or PARTIALLY_RECEIVED)
```

- **DRAFT**: created via `POST /purchasing/purchase-orders`. Freely
  editable in principle (M3 does not add a PO-edit endpoint — only
  create/submit/cancel — since the task scoped M3 to receiving as the
  core transactional boundary, not full PO editing).
  Line items are captured at creation (`quantity_ordered`, `unit_cost`)
  and **never change after receiving begins** — see §7 (cost snapshots).
- **ORDERED**: reached via `POST .../submit`. Only legal from `DRAFT`.
  This is the state goods receiving becomes possible against
  (`_ACTIVE_PO_STATUSES_FOR_RECEIVING = ("ORDERED", "PARTIALLY_RECEIVED")`).
- **PARTIALLY_RECEIVED**: set automatically by `_advance_purchase_order_
  status` the moment any line has `quantity_received > 0` but at least
  one line has `quantity_received < quantity_ordered`.
- **RECEIVED**: set automatically once every line's `quantity_received
  >= quantity_ordered`. A `RECEIVED` PO can no longer be received
  against (over-receiving happens *within* the last receipt, not by
  reopening a completed PO — see §8).
- **CANCELLED**: reached via `POST .../cancel`, legal from `DRAFT`,
  `ORDERED`, or `PARTIALLY_RECEIVED` (not from `RECEIVED` — nothing to
  cancel once fully received; not from `CANCELLED` — no double-cancel).
  A cancelled PO can never be received against again.

Every transition is audited (`PURCHASE_ORDER_CREATED`/`_SUBMITTED`/
`_CANCELLED`) with before/after status in the audit event.

---

## 4. Goods receiving: the core transactional boundary

`purchasing.service.receive_goods()` is the single entry point for
turning a purchase order line into stock. Sequence, matching the task's
required step ordering and `finalize_sale`'s established pattern:

1. **Store-scope check** against the PO's store, via a lightweight
   scalar query *before* loading the full PO (fail fast, no wasted work
   or lock contention for a request that's going to be rejected anyway).
2. **Idempotency fast path**: look up `client_transaction_id` first: if
   a receipt with this key already exists, return it unchanged — no new
   stock movement, no new audit event, no re-validation. This must
   happen before any locking so a duplicate request never contends for
   locks it doesn't need.
3. **Input validation**: lines non-empty, quantities `> 0`, costs
   `>= 0`, bounded list length (max 500, same bound as PO creation).
4. **Lock the `PurchaseOrder` row `FOR UPDATE`** — the fix from §1. This
   is what serializes two concurrent receipts against the same PO. It
   works because *every* code path that mutates `PurchaseOrderItem.
   quantity_received` goes through this function and acquires this lock
   first — Postgres does not cascade a parent lock to children rows;
   the safety comes from every writer being disciplined to lock the
   parent first, the same principle already used for `Product.
   current_qty_on_hand` via `lock_product_for_update`.
5. **Status check**: PO must be `ORDERED` or `PARTIALLY_RECEIVED`, else
   `ConflictError` (`PURCHASE_ORDER_NOT_RECEIVABLE`) — covers DRAFT
   (never submitted), CANCELLED, and already-RECEIVED.
6. **Resolve PO items**, summing quantity/cost across any duplicate
   lines referencing the same item in one request (a client sending two
   lines for the same item in one call is treated as one combined line,
   not two separate stock movements).
7. **Lock all distinct product rows, sorted by ascending product ID** —
   deterministic lock ordering across the whole codebase (same rule as
   `finalize_sale`) is what makes concurrency scenario E (multi-item
   receipts submitted with reversed line order) deadlock-free: two
   transactions that both need locks on products {5, 9} always request
   them in the order 5, then 9, regardless of what order the client
   listed them in the request body.
8. **Create the `GoodsReceipt` row**, flush, and catch `IntegrityError`
   on the `client_transaction_id` unique constraint — if a concurrent
   duplicate request beat this one to the insert, roll back and re-query
   for the winner's row instead of raising (see §6).
9. **Per line**: compute the new WAC via `inventory_service.
   compute_new_wac()` (the one existing implementation, reused, never
   duplicated), call `inventory_service.record_movement(movement_type=
   "PURCHASE_RECEIPT", reference_type="purchase_order", reference_id=
   purchase_order.id, new_product_cost=new_wac)` (which both posts the
   ledger row and updates `Product.current_qty_on_hand`/`current_cost`
   atomically), and create the `GoodsReceiptItem` with its own immutable
   `unit_cost` snapshot.
10. **Apply the summed `quantity_received` once per PO item** (not once
    per raw request line), tracking any item whose new total exceeds
    `quantity_ordered` in an `over_receipt_lines` list — flagged, not
    blocked (see §8).
11. **Advance PO status** via `_advance_purchase_order_status`.
12. **Audit** `GOODS_RECEIPT_COMPLETED`, including `over_receipt_line_
    item_ids` in the after-state so an over-receipt is discoverable from
    the audit trail even though it wasn't rejected.
13. `db.flush()` — **the function never commits.** The caller (the API
    route) commits once, after `receive_goods` returns, so the entire
    sequence above is one atomic database transaction: any failure
    anywhere in steps 1–12 rolls back everything, including stock and
    WAC changes already applied in-transaction.

`create_purchase_return` follows the same shape (idempotency fast path →
validate → lock distinct products ascending → create with duplicate-key
recovery → post `PURCHASE_RETURN` movements → audit → flush, no commit).

---

## 5. Locking strategy

| Resource | Lock | Why |
|---|---|---|
| `PurchaseOrder` row | `SELECT ... FOR UPDATE` | Serializes all writers of that PO's items' `quantity_received`, and of the PO's own status field. |
| `Product` rows | `SELECT ... FOR UPDATE`, sorted by ascending product ID | Serializes stock/WAC writers for that product; ascending order across the whole codebase prevents deadlocks between transactions that need overlapping sets of product locks in different request-order. |

No other locks are taken. Suppliers are not locked (no financial state on
the row that needs serializing under concurrent writes — activation
races are last-write-wins, which is acceptable for reference data).

---

## 6. Idempotency strategy

Every financial/inventory-mutating M3 endpoint requires a client-supplied
`client_transaction_id` (`goods_receipts.client_transaction_id`,
`purchase_returns.client_transaction_id`, both `String(100)`, DB-enforced
`UNIQUE`):

- **Sequential retry** (client resends the same request after a timeout,
  having never seen the first response): the early idempotency lookup in
  step 2 finds the existing row and returns it — no new receipt, no
  second stock movement.
- **True concurrent duplicate** (two requests with the same key racing
  each other): both pass the early lookup (neither has committed yet),
  both attempt the insert, the database's unique constraint lets exactly
  one succeed; the loser catches `IntegrityError`, rolls back its
  partial work, and re-queries to return the winner's row rather than
  erroring out to the client.
- **Retry after a business failure** (e.g., first attempt failed
  validation and never inserted a row): the key was never persisted, so
  a retry with the same key runs the full flow fresh — this is correct,
  since nothing happened the first time.

The frontend generates this key once per receiving/return attempt via
`crypto.randomUUID()` stored in a `useRef` (not React state, so retries
triggered by a re-render reuse the same key rather than minting a new
one) — the same pattern established for the POS sale-finalization flow
in M2.

---

## 7. WAC (Weighted Average Cost)

Formula (unchanged from the existing `inventory_service.compute_new_wac`,
reused rather than reimplemented):

```
new_wac = (existing_qty * existing_wac + received_qty * received_cost)
          / (existing_qty + received_qty)
```

Example from the live smoke test (§11): starting stock 0 @ cost 0;
receive 6 units @ 1.500000 → WAC becomes 1.500000; receive a further 4
units @ 3.000000 → `new_wac = (6*1.5 + 4*3.0) / 10 = (9 + 12) / 10 =
2.100000`. Verified live against the running API, matching the manual
calculation exactly.

- **WAC only changes on receipt** (an addition to stock). Sales,
  adjustments, and purchase returns never call `compute_new_wac` — they
  pass no `new_product_cost` to `record_movement`, so `Product.
  current_cost` is untouched. This was already true of `record_movement`
  before M3; M3 preserves it rather than introducing a second WAC path
  for returns.
- **Zero-quantity/zero-cost edge cases**: a receipt line with `quantity
  <= 0` is rejected at validation (step 3) before it can reach the WAC
  calculation — WAC is never computed against a zero or negative
  divisor. A `unit_cost` of `0.000000` is allowed (a supplier giving
  free stock is a real scenario) and correctly pulls the WAC down.
- **Precision**: all cost columns are `Numeric(14,6)` (matching M1's
  existing cost-precision convention), all arithmetic is `Decimal`-only
  throughout `purchasing/service.py` — no floats anywhere in the money
  or quantity path.
- **Concurrent receipts against the same product** (scenario A) are
  serialized by the product row lock (§5), so WAC recomputation is never
  based on a stale `existing_qty`/`existing_wac` pair.

---

## 8. Partial and over-receiving

- **Partial receiving** is the normal case: a receipt need not cover a
  PO item's full `quantity_ordered`. The PO status machine (§3) already
  models this (`PARTIALLY_RECEIVED`).
- **Over-receiving is allowed, not blocked, and flagged.** A receipt
  that pushes an item's cumulative `quantity_received` above `quantity_
  ordered` succeeds (this matches real warehouse behavior — a supplier
  physically ships more than ordered) but the affected item's
  `is_over_received` becomes `true` in every API read of that PO/item,
  and the receipt's audit event lists the affected line-item IDs under
  `over_receipt_line_item_ids`. This makes over-receiving discoverable
  (for a manager reviewing receipts) without making it a hard error that
  would force an operator to reject real, physically-received stock.
- **Under-receiving** requires no special handling: the PO simply stays
  `PARTIALLY_RECEIVED` (or the manager cancels the remainder via `POST
  .../cancel`, which is legal from `PARTIALLY_RECEIVED`).

---

## 9. Cost snapshots (immutability)

`PurchaseOrderItem.unit_cost` and `GoodsReceiptItem.unit_cost` are
captured at creation/receipt time and **never derived from or overwritten
by current product-master data.** Verified by dedicated tests
(`test_purchasing_audit_and_snapshots.py`):

- Changing a product's price/cost after a PO is created does not change
  that PO item's `unit_cost`.
- A later receipt that changes `Product.current_cost` (via WAC) does not
  retroactively change an earlier `GoodsReceiptItem.unit_cost` — each
  receipt line records the cost *it* was received at, permanently.

This is what makes the accounting outputs in §12 trustworthy: a
historical PO/receipt always reflects what was actually agreed/paid, not
whatever the product happens to cost today.

---

## 10. Purchase returns: scope and limitation

Purchase returns were assessed as **lower risk than initially expected**
and implemented for real (not deferred), because the existing `record_
movement` design already never lets a *removal* (sale, adjustment,
return) alter WAC — so a return cannot corrupt the cost basis of
remaining stock no matter how its own cost is computed.

**Known limitation, documented deliberately rather than silently
skipped:** the schema has no per-lot/per-receipt cost tracking (nothing
ties a `PurchaseReturnItem` back to a specific `GoodsReceiptItem`).
`create_purchase_return` therefore values a return at the product's
**current WAC at return time**, not the original receipt cost of the
specific units being returned. For a product whose WAC hasn't changed
since the relevant receipt this is exact; for a product that's had
multiple receipts at different costs since, the returned units' original
cost cannot be distinguished from other units in the same aggregate
stock pool — this is a bounded, documented limitation of the current
schema, not a bug, and matches how `current_qty_on_hand` itself is
already a single pooled quantity with no lot identity. A true lot-costed
return would require a lot-tracking data model, which is out of M3's
scope per the task's explicit "do not build beyond what M3 needs"
instruction.

Returns validate `quantity <= Product.current_qty_on_hand` (raising
`INSUFFICIENT_STOCK` otherwise) under the same product row lock used for
receiving, so a return cannot drive stock negative.

---

## 11. Live runtime smoke test

Run against a live `uvicorn` instance (not just the test suite) on
2026-09-11, exercising the full HTTP flow end-to-end with two real store
scopes:

1. Created store, admin user, supplier, and product via the running API.
2. Created a DRAFT PO for 10 units @ 1.500000, submitted it → `ORDERED`.
3. Received 6 units @ 1.500000 → stock `6.000`, cost `1.500000`.
4. **Retried the identical request (same `client_transaction_id`)** →
   returned the *same* receipt ID, stock/cost unchanged — idempotency
   confirmed live, not just in tests.
5. Received the remaining 4 units @ 3.000000 → stock `10.000`, cost
   `2.100000`, matching the hand-computed WAC exactly; PO advanced to
   `RECEIVED`.
6. Created a purchase return of 2 units → stock `8.000`, cost unchanged
   at `2.100000` — confirming §10's "returns never touch WAC" live.
7. Cross-store isolation: a second store's admin got `404` reading the
   first store's PO and product (no existence leak), `403 STORE_ACCESS_
   DENIED` attempting to receive against the first store's PO, and the
   first store's stock was confirmed unchanged after the blocked
   attempt. The second store's admin *could* see the first store's
   supplier in a list call, confirming §2's "suppliers are global"
   decision is actually in effect, not just documented.

---

## 12. Accounting foundation

M3 does not build an accounting subsystem — per the task's explicit
scope limit — but ensures the transactional records it creates are
sufficient to *derive* the accounting outputs the task named, without
any additional persisted state:

- **Purchase value** = `Σ GoodsReceiptItem.quantity_received × Goods
  ReceiptItem.unit_cost` per receipt/PO/supplier/date range — all from
  immutable snapshot columns (§9).
- **Inventory value** = `Σ Product.current_qty_on_hand × Product.
  current_cost` (current_cost being the running WAC) — already exposed
  via the existing products endpoint.
- **COGS** for a sale = the `unit_cost_at_movement` already recorded by
  `record_movement` for each `SALE` movement (M2 behavior, unchanged).
- **Supplier totals** = aggregate `GoodsReceiptItem` value grouped by
  the receipt's PO's `supplier_id`.
- **Margin inputs** = sale revenue (existing `SaleItem` data, M2) minus
  COGS (above) — no new column or table needed.

No purchase-side tax field was added (see §13) and no dedicated
reporting endpoints were built in M3; the above are derivable from
existing tables today, and a reporting/accounting milestone can query
them directly rather than M3 pre-aggregating results it doesn't yet have
a consumer for.

---

## 13. Tax handling — deliberately out of scope for purchases

`purchase_order_items`/`goods_receipt_items` carry no tax field, and M3
does not add one. This is a **deliberate scope decision**, not an
oversight: the existing tax architecture (`TaxRate`, effective-dated,
applied in `sales/service.py`) models tax charged *to a customer on a
sale*; the blueprint's own cost/margin formulas have no purchase-side tax
term, and the task's instruction to "use the existing effective-dated
tax-rate architecture" describes reusing it *if and when* purchase tax is
needed, not mandating it be added now. Introducing a purchase-tax model
would be new accounting-subsystem surface the task explicitly said not to
build ahead of an actual requirement. Any timestamp that *is* recorded in
M3 (e.g., `received_date`, audit `created_at`) continues to use UTC, per
M2 hardening's established decision, since no argument for a different
rule arose in M3.

---

## 14. Multi-store isolation

Every store-scoped M3 entity (`PurchaseOrder`, `GoodsReceipt`,
`PurchaseReturn`) extends M2 hardening's CRITICAL-1 fix pattern
end-to-end:

- Route layer: `enforce_store_access`/`scoped_store_filter` from `auth/
  service.py` gate list/detail/write endpoints.
- Service layer: a local `_enforce_store_access` helper (mirroring the
  route-layer one, for direct service-function testability without
  spinning up the HTTP layer — the same duplication pattern already used
  by `finalize_sale`) re-checks store scope inside `receive_goods` and
  `create_purchase_return`, so a service-layer caller can't bypass the
  check by skipping the route.
- Cross-store reads return `404` (existence is not leaked), cross-store
  writes return `403 STORE_ACCESS_DENIED`. Both verified live (§11) and
  by automated tests covering create/read/receive/list, plus a positive
  control confirming a legitimately cross-store-permissioned manager
  role still works where intended.
- `Supplier` is the one deliberate exception (§2) — global by design.

---

## 15. RBAC

Three new/reused permissions gate purchasing endpoints:
`purchasing.read`, `purchasing.write`, `purchasing.receive` (goods
receiving and purchase returns require `purchasing.receive` specifically,
distinct from general `purchasing.write`, since receiving is the
stock/money-moving action). Every endpoint has automated tests covering
authorized, unauthorized (wrong/missing permission), wrong-store, and
unauthenticated cases.

---

## 16. Auditing

Every inventory- or PO-state-affecting action is audited with enough
context to reconstruct who/what/where:
`SUPPLIER_CREATED`/`_UPDATED`/`_ACTIVATED`/`_DEACTIVATED`,
`PURCHASE_ORDER_CREATED`/`_SUBMITTED`/`_CANCELLED`,
`GOODS_RECEIPT_COMPLETED` (including flagged over-receipt line-item IDs),
`PURCHASE_RETURN_COMPLETED`. Audit rows are written inside the same
transaction as the mutation they describe (so an audit event can never
exist for a mutation that didn't actually commit, or vice versa), and the
existing DB-level audit-immutability guarantees from M2 hardening are
unchanged by M3 — no new code path writes to or deletes from the audit
table outside the existing audit-service insert path.

---

## 17. API design

- No internal ORM models are returned directly — every response goes
  through a Pydantic read schema (`schemas.py`).
- Errors use the existing envelope `{"error": {"code", "message"}}` with
  deterministic codes: `PURCHASE_ORDER_NOT_RECEIVABLE`, `STORE_ACCESS_
  DENIED`, `INSUFFICIENT_STOCK`, `VALIDATION_ERROR`, `NOT_FOUND`, etc.
- Cross-module reads (a PO's product name/SKU, a PO's supplier name) are
  resolved via explicit batch `select()` queries in `_to_po_read`, not
  ORM relationship traversal — matching `sales.py`'s established `_to_
  sale_read` pattern, since no `PurchaseOrderItem.product` or
  `PurchaseOrder.supplier` relationship exists on the models (this was a
  bug caught and fixed during implementation, not a pre-existing
  relationship that was reused).

---

## 18. Frontend

`SuppliersPage.tsx` and `PurchasingPage.tsx` (both previously
placeholders; routing/nav/permission-gating already existed from M0 and
needed no changes) implement the minimum usable workflow: supplier
management → PO creation (SKU-based product lookup) → submission →
list/detail → receive goods (partial supported) → confirmation → updated
stock/WAC visible immediately via a detail refetch.

The receiving panel shows, per line: ordered, already-received,
remaining, receiving-now (input), unit cost, and a live-computed
resulting total — making an accidental mis-receive visible before
submission, per the task's requirement. The idempotency key is generated
once per receipt attempt via `crypto.randomUUID()` in a `useRef` (not
state), so a retry after a network timeout reuses the same key rather
than creating a duplicate-looking request; a duplicate button-press while
a request is in flight is prevented by disabling the submit control for
the duration of the request.

---

## 19. Database & migrations

Migration `c82162efb3af` adds `suppliers.code` (partial unique index),
`goods_receipts.store_id` + `client_transaction_id`, and `purchase_
returns.client_transaction_id`, all following the safe nullable →
backfill → `NOT NULL` pattern for already-populated tables (`store_id`
backfilled from the parent PO's store; `client_transaction_id` backfilled
with a synthetic `'legacy-' || id` value for any pre-existing rows so the
`NOT NULL` + `UNIQUE` constraints can be added without breaking existing
data). All constraints are named. Verified with a full `downgrade base` →
`upgrade head` cycle against a real Postgres database containing
committed data, not just an empty test schema.

---

## 20. Testing strategy

169 backend tests pass (from 126 pre-M3), covering: migration up/down/up,
supplier/PO domain and API behavior (~31 tests in `test_purchasing_api.
py`), goods-receiving transaction correctness, WAC math, six lettered
concurrency scenarios (A–F, real Postgres row-locking via `threading.
Barrier` and independent DB connections, repeated 5x clean —
`test_purchasing_concurrency.py`), idempotency (sequential retry, retry-
after-failure, true-concurrent-duplicate — `test_purchasing_idempotency.
py`), RBAC and multi-store isolation (positive and negative cases for
every endpoint), and audit/immutability (`test_purchasing_audit_and_
snapshots.py`). 19 frontend tests pass (13 pre-M3 + 6 new), covering
supplier list/create/deactivate-confirm and PO list/submit/receive
flows with stateful mocks that simulate status changing after a POST.

---

## 21. Known limitations and deferred decisions

- **Purchase returns use current WAC, not original per-lot receipt
  cost** — documented in §10 as a bounded schema limitation, not a bug.
- **No purchase-side tax modeling** — deliberate scope decision, §13.
- **No PO-edit endpoint** — a DRAFT PO can be created/submitted/
  cancelled but not line-edited after creation; re-creating a fresh DRAFT
  is the current workaround. Deferred as out of M3's stated scope
  (receiving is the core boundary, not full PO lifecycle editing).
- **No dedicated reporting/accounting endpoints** — §12's outputs are
  derivable from existing tables today; a future milestone can add
  aggregation endpoints without any M3 schema change.
- **No paid/external services were introduced in M3** — purely a
  backend/frontend feature build against the existing local Postgres
  instance; the Tool/Service Policy accordingly had no applicable
  decisions to record for this milestone.

---

## 22. Cross-references updated

- `README.md`: module/milestone status line updated to include M3.
- `docs/M2_AUTH_AND_POS.md` / `docs/M2_HARDENING_AUDIT.md`: no content
  changes were required — M3 extends rather than alters any M2-documented
  behavior (auth, RBAC primitives, store-scoping helpers, and audit
  mechanics are reused as-is).
