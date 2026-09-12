# M8 — Advanced Inventory, Stock Counts, Inter-Store Transfers & Replenishment

## Phase 0 — What M8 must preserve (traced from M0–M7)

**Inventory lifecycle today:** `PurchaseOrder` (no inventory effect) →
`receive_goods` (locks the PO row, then every distinct `Product` row
touched, ascending id — the deadlock-safe pattern reused everywhere below)
→ one `InventoryMovement` (`PURCHASE_RECEIPT`) + WAC recompute per line →
`finalize_sale` (`SALE`, WAC frozen as COGS, never recomputes WAC) →
`create_sale_return`/`create_purchase_return` (`SALE_RETURN`/
`PURCHASE_RETURN`) → `create_stock_adjustment` (`STOCK_ADJUSTMENT_IN`/
`_OUT`, reason codes `DAMAGE`/`THEFT`/`EXPIRY`/`STOCKTAKE_CORRECTION`/
`OTHER` — **`STOCKTAKE_CORRECTION` already exists and is currently
unused by any code path**) → each of these calls
`accounting_service.post_*_journal` inline, in the same uncommitted
transaction, before the caller's single `db.commit()`.

**Source of truth:** `inventory_movements` is the append-only ledger
(UPDATE/DELETE revoked from `erp_app` at the DB level — M1 privilege
migration). `products.current_qty_on_hand`/`current_cost` are a
maintained *cache*, updated only inside `record_movement` under a
`SELECT ... FOR UPDATE` lock on the product row (`lock_product_for_update`),
never written directly anywhere else. `record_movement` also enforces
`allow_negative_stock` and appends exactly one ledger row per call.

**The load-bearing architectural fact M8 must design around:**
`Product.store_id` is NOT NULL with `UniqueConstraint("store_id", "sku")`
— a product row belongs to exactly one store. The `InventoryMovement`
model's own docstring anticipated this: *"Multi-store transfers
(TRANSFER_IN/TRANSFER_OUT) are NOT included in the movement_type set...
Revisit when multi-store is actually implemented."* Decoupling `Product`
from `store_id` to support a single cross-store "logical product" would
touch `Sale`, `SaleItem`, `PurchaseOrder`, `PurchaseOrderItem`,
`GoodsReceiptItem`, `InventoryMovement`, `StockAdjustment`, and the entire
AP module's store-scoping checks — a rewrite of working M0–M7 code with no
concrete defect motivating it. **Decision: `Product` stays store-scoped.**
An inter-store transfer moves value between two *distinct* `Product` rows
— the source store's row for a SKU and the destination store's *own*,
pre-existing row for the same SKU — never a single cross-store product
identity. See Section 3 for the full consequence of this decision.

**Accounting integration:** `_post_journal` resolves account codes from
`accounting/constants.py`, builds every line from values computed once and
reused for both sides of a pair (never independently derived), and is
called inline inside the same transaction as the operational change —
never as a separate step that could commit out of sync. `AUTOMATED_SOURCE_TYPES`
blocks `reverse_journal_entry`'s generic reversal for every
automatically-posted source type, forcing any correction through the real
operational undo instead.

**Store isolation:** every service function takes `caller_store_id` and
calls a locally-duplicated `_enforce_store_access` (not imported from
`auth`, so the module stays testable standalone — the established M2
pattern, repeated in every module since).

**Locking strategy:** lock the "parent" row first (PO, invoice, stock
count — see below), then every distinct affected `Product` row, always in
ascending `product_id` order, regardless of input order — the single
deadlock-avoidance convention used identically in `receive_goods`,
`finalize_sale`, `create_purchase_return`, and M7's multi-PO invoice
posting.

**Idempotency:** a `client_transaction_id` fast-path lookup before any
mutation, backstopped by a DB unique constraint and an `IntegrityError`
recovery block that returns the winner of a genuinely concurrent race —
identical shape in every write path since M2.

**Audit:** `audit_service.log_event` inside the same transaction, `before`/
`after` dicts, never a separate commit.

**Migration downgrade protection:** every milestone since M6 guards a
downgrade with a `DO $$ ... RAISE EXCEPTION ... $$` check for real
posted-data that the downgraded schema cannot represent, placed *before*
any destructive DDL — proven against a real populated database each time
(M6 and M7 hardening audits both found and fixed a real crash/gap here).

**Current gaps M8 exists to close:** no stock-count/physical-inventory
concept at all; no multi-store movement types; no transfer entity; no
formal replenishment/reorder workflow beyond a single `reorder_point`
column and a `low_stock_only` filter on `list_stock_levels`.

## No external services required

M8 introduces no barcode scanning service, mapping/logistics API,
notification provider, or any other external dependency. Everything is
internal business logic against the existing PostgreSQL database.

## Design Decision 1 — Stock count relationship to existing tables

A `StockCount` is scoped to exactly one store (`store_id`), with an
optional `category_id` filter and/or an explicit product list at creation
time (`StockCountLine` rows are the actual scope — "category scope" is
just a convenience that expands into concrete lines at DRAFT time, never
re-evaluated later, so a product added to the category after the count
started is never silently swept in).

A posted stock count's variance is applied via the **existing**
`StockAdjustment`/`InventoryMovement`/`post_stock_adjustment_journal`
machinery, extended with one new nullable FK
(`stock_adjustments.stock_count_id`) rather than a parallel accounting
path. This is a direct application of "trace M4 first, extend the CoA
only where justified" — `ACCOUNT_INVENTORY_ADJUSTMENT_GAIN` (4900) and
`ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (5900) already exist, already have
exactly the right semantics ("stock found in excess/missing relative to
the recorded on-hand quantity"), and `reason_code = 'STOCKTAKE_CORRECTION'`
already exists in `stock_adjustments`'s own CHECK constraint, unused by
any code path until now. **No new GL accounts are introduced for stock
counts.**

## Design Decision 2 — Stock count lifecycle

```
DRAFT → OPEN → COUNTED → REVIEWED → POSTED
  ↓       ↓        ↓          ↓
              CANCELLED
```

- **DRAFT**: header + scope only. Lines are freely added/removed. No
  expected quantity captured yet — the scope isn't final until OPEN, so
  snapshotting now would be meaningless.
- **DRAFT → OPEN**: **this is the exact moment `expected_quantity` is
  captured**, one line at a time, under the same per-product
  `SELECT ... FOR UPDATE` + ascending-id-order lock `receive_goods` uses.
  This is a deliberate, load-bearing design choice: expected quantity is
  "book quantity at the instant counting begins," not at count creation
  and not at posting. From this point on, normal store operations
  (sales, receipts, adjustments, returns) continue completely unimpeded —
  **no global lock, no freeze of the product catalog** — inventory is not
  a resource a stock count is allowed to hold hostage.
- **OPEN → COUNTED**: counters submit `counted_quantity` per line
  (`record_count_entry`, one line at a time, row-locked). A line may be
  re-submitted while the count is OPEN or COUNTED — this is a *recount*
  (Design Decision 3). The transition to COUNTED itself just marks
  "counting is believed complete"; it does not require every line to have
  a non-null counted quantity (a line legitimately never counted — see
  Design Decision 4 — must still be visible to the reviewer, not silently
  dropped).
- **COUNTED → REVIEWED**: a checkpoint requiring a *different* permission
  tier (`inventory.count.review`) than counting itself
  (`inventory.count.write`) — mirrors the AP module's matching-before-
  posting checkpoint. No inventory or accounting effect yet.
- **REVIEWED → POSTED**: the one financially-atomic transition. See
  Design Decision 5 for exactly what makes this safe under concurrency.
- **Cancellation**: allowed from DRAFT/OPEN/COUNTED/REVIEWED (never from
  POSTED — the whole point of posting is to be the immutable point of no
  return, the same rule every other financial document in this codebase
  follows). Cancelling an OPEN-or-later count has no inventory/accounting
  effect to undo, since nothing was ever posted — it is a pure status
  flip plus audit entry, like voiding a DRAFT purchase invoice.

## Design Decision 3 — Recounts

A `StockCountLine` is unique per `(stock_count_id, product_id)` — the DB
enforces "one line per product per count" directly; there is no separate
"recount" row type. A recount is simply calling the count-entry endpoint
again against an existing line while the count is still OPEN or COUNTED.
It supersedes the previous count value by **overwriting** `counted_quantity`/
`counted_by`/`counted_at` on that same row, under a row lock — the prior
value is never silently lost, because every count-entry call is audited
(`audit_service.log_event` with `before`/`after` on `counted_quantity`).
**Recounts are also the mandatory resolution path for drift** (Design
Decision 5): when posting detects that a product's book quantity moved
since `expected_quantity` was snapshotted, the fix is to reopen the count
(REVIEWED → COUNTED) and recount just the affected line — which
re-snapshots `expected_quantity` to the *current* book quantity at that
moment and requires a fresh `counted_quantity`, exactly modeling "the
world changed, so the comparison basis must move forward too, never be
silently kept stale."

## Design Decision 4 — Product coverage edge cases

- **Missing (never located)**: counter submits `counted_quantity = 0`
  explicitly — a real, meaningful physical fact, not the same as "not yet
  counted" (`counted_quantity IS NULL`). Posting treats a `NULL` counted
  line as **not eligible for posting** (skipped, flagged in the response),
  never silently treated as zero.
- **Over-counted**: no ceiling — `counted_quantity` can exceed
  `expected_quantity` freely (a positive variance, Inventory Adjustment
  Gain).
- **Zero counted**: see "missing" above — an explicit, valid count.
- **Inactive product**: may still be included if it was in scope when the
  count opened (an inactive product can still hold physical stock that
  needs reconciling) — `is_active` is never a gate on stock-count lines.
- **Newly introduced product** (created after the count opened): never
  swept into an already-OPEN count's scope (scope is frozen at OPEN,
  Design Decision 1) — it simply isn't a line; a future count will cover
  it.
- **Not included in the original count**: has no line at all and is
  entirely unaffected by this count, posted or not — no implicit
  "everything else is assumed correct" inference is ever made.

## Design Decision 5 — Stock count concurrency (the mandatory analysis)

**The central risk:** a stock count's `expected_quantity` is a point-in-
time snapshot (captured at OPEN). Real operations (sales, receipts,
adjustments, returns, even a *different* stock count's posting) continue
against the same products for the entire OPEN→POSTED window, which can
span hours or days. If posting blindly computed
`variance = counted_quantity − expected_quantity` and applied that delta
to whatever `current_qty_on_hand` happens to be *at posting time*, every
legitimate movement that occurred during the count window would be
silently double-counted or erased. This is the exact failure mode the
task requires be designed against, not merely tested for.

**Decision: recompute-at-post with mandatory drift detection.**
`post_stock_count` locks the `StockCount` header row first (serializing
two concurrent posts of the *same* count — the second sees POSTED and
returns idempotently, mirroring `post_purchase_invoice`'s precedent),
then locks every distinct `Product` referenced by its lines, ascending
`product_id` order (deadlock-safe against every other locker in this
codebase). For each line, **under that lock**, it compares the product's
*current* `current_qty_on_hand` against the line's stored
`expected_quantity`:

- **No drift** (current == expected — nothing touched this product since
  OPEN): `variance = counted_quantity − expected_quantity` is safe to
  apply as-is (it equals `counted_quantity − current_qty_on_hand`, the
  two are interchangeable exactly because nothing moved). Post one
  `StockAdjustment` (reason `STOCKTAKE_CORRECTION`, `stock_count_id` set)
  per nonzero-variance line, reusing `create_stock_adjustment`'s own
  movement+journal+audit internals inline in the same transaction (one
  commit for the whole count, not one per line).
- **Drift detected** (current != expected — something posted against this
  product during the count window): **the entire count's posting is
  refused** (`ConflictError`, `STOCK_COUNT_DRIFT_DETECTED`, naming every
  drifted line) — never partially posted, and never silently posted
  against the wrong basis. The count is unlocked (transaction rolled
  back) and the caller must reopen it (REVIEWED → COUNTED) and recount
  the named lines (which re-snapshots `expected_quantity` to the new
  current value, Design Decision 3) before posting can be retried. This
  is the explicit, deterministic policy the task requires in place of a
  global lock — every non-count operation on inventory remains completely
  unblocked for the entire count lifecycle; only the *posting instant*
  ever takes a product lock, and only to detect (never to prevent) a
  conflict.

**Race-by-race consequence of this design:**

| Race | Outcome |
|---|---|
| Sale/receipt/adjustment/return while count is OPEN | Proceeds normally, no blocking. Detected as drift at posting time for that product only; forces a targeted recount, not a whole-count failure beyond that. |
| Two users posting the same count | Header row lock — one posts, the other gets the already-POSTED result back idempotently. |
| Two users reviewing the same count | Header row lock on the COUNTED→REVIEWED transition — same idempotent-second-caller shape. |
| Two users counting the same product (same line) | Row lock on `StockCountLine`; both submissions are audited, the later one (by lock acquisition order) wins as the line's authoritative value — no lost update, and nothing is silently discarded from the audit trail. |
| Stock count posting concurrent with another inventory mutation | Whichever transaction acquires the product's row lock first proceeds; the other blocks until it commits, then reads the now-current value — this is precisely what turns a genuine race into a detected, reported drift rather than a corrupted variance. |

## Design Decision 6 — Inter-store transfers: relationship model

Given Product's store-scoping (Phase 0), a transfer line references
**two distinct product rows**: `source_product_id` (must belong to
`from_store_id`) and `destination_product_id` (must belong to
`to_store_id`, **must already exist** — the system never auto-creates a
destination catalog entry merely because a transfer references it, the
same "never create inventory merely because a document exists" principle
extended to the catalog). At line-creation time the destination product is
resolved by matching SKU within the destination store by default (the
common case — same product, same SKU, different store), but the API
accepts an explicit `destination_product_id` for the rare case of a
deliberate SKU remap. If no matching destination product exists and none
is given explicitly, line creation is rejected
(`DESTINATION_PRODUCT_NOT_FOUND`) rather than silently skipped or
auto-created.

## Design Decision 7 — Transfer lifecycle

```
DRAFT → SHIPPED → RECEIVED
  ↓
CANCELLED
```

**REQUESTED and APPROVED are deliberately NOT separate states.** The task
explicitly asks to justify each state by operational value. This codebase
has no cross-store approval workflow precedent to extend (a `PurchaseOrder`
goes DRAFT→ORDERED with `purchasing.write`, no separate approval gate), and
inventing one here would be new, un-asked-for process weight with no
concrete requirement behind it (mirrors the M7 decision not to build a
speculative approval-queue entity for invoice exceptions). Instead:
creating a transfer already requires `inventory.transfer.write` at *both*
the source and destination store's operators' discretion (a transfer is
visible to and cancellable by either store before shipment), and shipping
is itself the commitment point — exactly like a `PurchaseOrder`'s
DRAFT→ORDERED being submission, not a separate approval step. If a real
future need for cross-store spend approval emerges, add it then, against
a real requirement, the same way M6 deferred debit notes.

`IN_TRANSIT` is not a fourth *status* value — it is a **derived fact**:
"shipped but not yet fully received" is exactly `status == 'SHIPPED'` with
`received_quantity < shipped_quantity` on at least one line. Adding a
redundant status column that must always agree with that same derived
fact is exactly the kind of second-source-of-truth this codebase avoids
(`balance_due` on invoices is deliberately never stored, for the identical
reason).

- **DRAFT**: header + lines, no inventory effect. Freely editable.
- **DRAFT → SHIPPED**: locks the source `PurchaseOrder`-equivalent (the
  `InterStoreTransfer` row itself) then every distinct **source** product,
  ascending id. For each line, records the shipped quantity (which may be
  a **partial** shipment of the requested quantity — see Design Decision
  8), captures `unit_cost_at_shipment = source_product.current_cost`
  **at this exact moment**, and posts `TRANSFER_OUT` against the source
  product (an M8-new `InventoryMovement.movement_type`, `reference_type =
  'inter_store_transfer'`) plus the in-transit journal (Design Decision
  9). Inventory leaves the source store here, not at DRAFT creation.
- **SHIPPED → RECEIVED** (possibly partial, possibly multiple receipt
  events — see Design Decision 8): locks every distinct **destination**
  product, ascending id. For each line, applies the **frozen**
  `unit_cost_at_shipment` (never the destination's own current WAC) into
  the destination product's WAC via the *same* `compute_new_wac` formula
  a supplier receipt uses, posts `TRANSFER_IN`, and posts the matching
  in-transit-clearing journal leg. Once every line's received quantity
  reaches its shipped quantity, the transfer's derived state becomes fully
  RECEIVED.
- **Cancellation**: allowed only while DRAFT (nothing has moved yet). Once
  SHIPPED, inventory has already left the source store — cancelling would
  require a real reversal workflow (ship back), which is explicitly
  **deferred** (see "Known limitations"), the same way M6 deferred voiding
  a paid invoice rather than building an unsafe partial-reversal path.

## Design Decision 8 — Partial shipments and receipts

**Shipping is a single event per transfer**; **receiving may happen in
multiple events** (the common real case — a truck arrives and receiving
staff process it over several scans, or part of a shipment is rejected on
arrival and received separately/later). This asymmetry is deliberate, not
an oversight: `InterStoreTransferLine` carries `requested_quantity`,
`shipped_quantity` (0 ≤ shipped ≤ requested, set once at the one DRAFT→
SHIPPED transition — a line may legitimately ship *less* than requested,
which is what "partial shipment" means here), and `received_quantity` (a
running total, 0 ≤ received ≤ shipped, updated by possibly several
`InterStoreTransferReceipt` events — the exact same header+line-items
shape as `GoodsReceipt`/`GoodsReceiptItem`, reused deliberately rather than
inventing a parallel concept). Supporting multiple *shipment* events per
transfer would require an equivalent `InterStoreTransferShipment` child
entity for no concretely-stated requirement — deferred; a transfer that
needs to send more later is a new transfer, the same way a business
issues a new PO rather than reopening a submitted one.

## Design Decision 9 — Transfer accounting

**No P&L impact, no revenue, no expense.** A transfer is a pure balance-
sheet reclassification of the *same* asset (Inventory) — value never
leaves the business, so nothing is earned or spent. A new asset account is
required (nothing existing has this semantic): **`Inventory In Transit`
(1520)**, added to the Chart of Accounts by this milestone's migration —
justified because M4's existing accounts have no way to represent "goods
this business owns but which are not sitting in any one store's
countable stock" without it; collapsing this into plain `Inventory` would
make the per-store operational stock level (which excludes in-transit
goods, correctly — you cannot sell what hasn't arrived) permanently
diverge from the GL Inventory balance (which must include it, since the
business still owns the value) with no way to reconcile the difference.

- **At ship**: `Dr Inventory In Transit / Cr Inventory` — for the shipped
  quantity × `unit_cost_at_shipment`, computed once and reused for both
  lines (never independently derived, per the M4 module docstring's rule).
  Posted against the **source** store (`JournalEntry.store_id`).
- **At receive**: `Dr Inventory / Cr Inventory In Transit` — for the
  received quantity × the same frozen `unit_cost_at_shipment` (never the
  destination's post-receipt WAC, which is itself derived *from* this
  value — using it on both sides of its own derivation would be circular).
  Posted against the **destination** store.
- Both legs together, across the whole transfer, always net to zero real
  economic effect: total `Inventory In Transit` debited at ship equals
  total credited at receive once every unit arrives; a partially-received
  transfer leaves a genuine, correctly non-zero `Inventory In Transit`
  balance for the quantity still in transit — reconciled by a new
  `inventory_in_transit_reconciliation` report (GL balance vs. Σ
  `(shipped_quantity − received_quantity) × unit_cost_at_shipment` across
  all not-fully-received transfer lines), the same shape as every other
  M4/M6/M7 reconciliation report.

## Design Decision 10 — Transfer concurrency

Same deadlock-safe, lock-then-detect discipline as goods receiving and
stock counts:

- **Two users ship the same transfer**: the transfer header row is locked
  FOR UPDATE first (mirrors `receive_goods` locking the PO first); the
  loser sees the now-updated `shipped_quantity` after acquiring the lock
  and its own shipment request is validated against *that*, not a stale
  read — a request that would over-ship is rejected
  (`OVER_SHIPMENT`), never silently capped.
- **Two users receive the same transfer / overlapping quantities**:
  identical header-lock-first pattern bounds `received_quantity ≤
  shipped_quantity` per line under the lock; a request that would
  over-receive is rejected (`OVER_RECEIPT`).
- **Shipment concurrent with a sale, or with a stock adjustment, at the
  source store**: both lock the same `Product` row via
  `lock_product_for_update` — whichever acquires it first proceeds, the
  other sees the post-commit quantity; `record_movement`'s existing
  `INSUFFICIENT_STOCK` guard (respecting `allow_negative_stock`) applies
  identically to a `TRANSFER_OUT` as to a `SALE`.
- **Receipt concurrent with a sale at the destination store**: same
  product-row-lock serialization; a sale cannot oversell stock that
  hasn't been received yet, because the `TRANSFER_IN` movement (and the
  qty increment it carries) only exists once the receiving transaction
  has committed.
- **Transfer cancellation concurrent with shipment**: the header row lock
  makes these mutually exclusive — cancellation is only legal from DRAFT
  (Design Decision 7), so a shipment that wins the race moves the status
  to SHIPPED first, and the cancellation attempt (now reading SHIPPED)
  is rejected (`INVALID_TRANSFER_STATE`), never silently allowed to
  "cancel" inventory that has already left the building.
- **Duplicate API request / retry after timeout**: `client_transaction_id`
  idempotency on both the ship and receive operations, identical shape to
  every other mutating endpoint in this codebase (pre-lock fast path +
  DB unique constraint + `IntegrityError` recovery).
- **Source and destination operations concurrently**: these touch
  *different* `Product` rows entirely (two separate rows, Design Decision
  6) and are never both locked by the same operation at the same time —
  shipping only ever locks source products, receiving only ever locks
  destination products — so they cannot deadlock against each other by
  construction, only serialize normally through the transfer header lock
  where they actually share state (the running shipped/received totals).

## Design Decision 11 — Replenishment

Scoped deliberately narrow, matching the M6/M7 "optimize for correctness,
not feature count" discipline: **a read-only suggested-replenishment
report**, not a new automated ordering engine. For each product with a
non-null `reorder_point`, the report computes:

```
inventory_position = current_qty_on_hand
                    + Σ (shipped − received) on open inbound transfers to this store
                    + Σ (quantity_ordered − quantity_received) on open purchase orders for this store
suggested_quantity = max(0, reorder_point − inventory_position)
```

`inventory_position` (not raw on-hand) is the correct basis — counting
only physical on-hand would suggest re-ordering stock that is already
inbound, producing duplicate purchase orders. The report distinguishes,
per product, how much of the shortfall could be satisfied by an existing
sister-store surplus (`current_qty_on_hand` at another active store
carrying the same SKU, above *that* store's own reorder point) versus how
much genuinely needs a new supplier purchase order — surfaced as two
separate suggested-action figures (suggested transfer quantity vs.
suggested purchase quantity), never a single number that silently prefers
one over the other. The report **never creates a PurchaseOrder or
InterStoreTransfer itself** — a human always initiates the resulting
document through the existing `create_purchase_order`/`create_transfer`
endpoints, using the report's numbers as input. This keeps replenishment
a pure decision-support read path with zero risk of an automated process
placing an order or moving stock nobody asked for.

## Deferred / known limitations (stated up front, not discovered late)

- Cancelling or reversing a SHIPPED (in-transit) transfer — deferred, the
  same way M6 deferred voiding a paid invoice; the safe path (a real
  reverse-transfer) is a future milestone's concern, not an unsafe partial
  fix here.
- Transfer *requests* with a separate approval gate — deferred (Design
  Decision 7); add only against a real, stated requirement.
- Automated purchase-order/transfer creation from the replenishment report
  — deliberately out of scope (Design Decision 11).
- A stock count's expected-quantity drift resolution is recount-only; no
  "override and post anyway" escape hatch is provided, even for an Admin
  — a deliberate, conservative choice given the alternative is silent
  financial corruption.
- Serialized (lot/expiry) inventory tracking remains out of scope, exactly
  as under M0–M7 — a stock count and a transfer both operate at the
  product-quantity level, matching every other inventory operation in this
  codebase.

See `docs/M8_HARDENING_AUDIT.md` for the full test inventory, concurrency
results, mutation-testing results, and final verdict.
