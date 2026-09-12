# M9 — Advanced Supply Chain, Replenishment & Procurement Planning: Design

Written *before* implementation, per the task's Phase 0.5 requirement. Phase 0
findings (verified against the actual repository and a running PostgreSQL
database — M7/M8's own hardening audits were re-checked against code, not
assumed) come first, then the 20 required design answers, then numbered
design decisions.

## Phase 0 — What M9 inherits, verified against code

**The supply chain trace** (Supplier → Product/Supplier relationship →
Reorder Policy → Current Stock → In Transit → Open PO → Received → Invoiced
→ AP → Paid), walked end to end against the actual models/services:

- `Supplier` (`app/modules/purchasing/models.py`) is global reference data,
  not store-scoped — confirmed by its own docstring and lack of `store_id`.
  It has no lead-time or pricing field today.
- **There is no supplier-product catalog or pricing table anywhere in the
  codebase** (grepped `unit_cost`/`price`/`lead_time`/`minimum_order`/
  `pack_size`/`moq` across every module). `PurchaseOrderItem.unit_cost` and
  `AP`'s `PurchaseInvoiceLine.unit_price` are transaction-line snapshots,
  not a catalog. M9 must introduce a real catalog (Section 6 of the task) —
  there is nothing to "extend."
- `Product.reorder_point` and `Product.default_supplier_id` already exist
  (M1/M3). `Product` is **store-scoped** (`UniqueConstraint("store_id",
  "sku")`, re-confirmed from M8's own Phase 0) — a product row already *is*
  a (store, product) pair. There is no `target_stock`/`minimum_stock`/
  `safety_stock` field yet.
- **Current stock** = `Product.current_qty_on_hand` (a maintained cache,
  reconciled against `inventory_movements`, per M1).
- **In transit** = M8's `InterStoreTransferLine.shipped_quantity -
  received_quantity` for transfers whose `InterStoreTransfer.status ==
  'SHIPPED'` and destined for this product (by `destination_product_id`).
- **Open PO** = `PurchaseOrderItem.quantity_ordered - quantity_received`
  for purchase orders whose status is `DRAFT`/`ORDERED`/
  `PARTIALLY_RECEIVED` (M3/M6).
- **Received** → **Invoiced** → **AP** → **Paid** is M6/M7's existing
  three-way-match/settlement chain (`PurchaseOrderItem.quantity_received`
  → `PurchaseInvoice`/`PurchaseInvoiceLine` → `SupplierPayment`/
  `SupplierCreditNote`). M9 does not touch any of this — see Design
  Decision 9.
- M8's `app.modules.replenishment.service.get_replenishment_suggestions`
  is a **read-only** report (verified: `test_never_creates_a_purchase_order_or_transfer_itself`
  calls it twice and asserts zero side effects). It already computes
  `inventory_position = on_hand + inbound_transfer_qty + open_purchase_order_qty`
  and a transfer-vs-purchase split, but picks only the **first** sister
  store with any surplus (not a ranked/exhaustive split across several
  source stores), and never persists anything a user can act on. M9
  builds on this calculation (Design Decision 1) rather than replacing it.

**Two load-bearing facts that directly shape the M9 design, discovered by
reading the actual function bodies (not assumed from the M8 audit doc):**

1. **`create_purchase_order` and `create_transfer` each call `db.commit()`
   internally** (`purchasing/service.py` line ~288, `transfers/service.py`
   line ~196) and are called by their route handlers with no surrounding
   transaction. This means M9 cannot atomically create a PO/transfer *and*
   update a replenishment plan's status/link in one transaction by calling
   these functions as-is — a crash between the PO's own commit and the
   plan's status update would leave exactly the forbidden state the task's
   Section 20 names ("PO exists but plan claims it was never executed").
   **Design Decision 3** below addresses this by refactoring each into a
   non-committing inner function plus a thin, behavior-identical committing
   wrapper — the same pattern M8 already used for
   `create_stock_adjustment` (extended with `stock_count_id`, kept
   callable standalone, wrapped by `post_stock_count` inside one larger
   transaction).
2. `ship_transfer`/`receive_transfer` do **not** commit internally (the
   route commits after calling them) — already the right shape for M9 to
   reuse untouched; M9 never calls these directly (it only ever creates a
   DRAFT transfer — see Design Decision 9).

**RBAC inherited**: `app/modules/auth/permissions.py` currently defines,
relevant to M9: `PURCHASING_WRITE`/`PURCHASING_RECEIVE`, `AP_*`,
`INVENTORY_COUNT_*`, `INVENTORY_TRANSFER_*`. No `supply_chain.*`
permission exists yet.

## The 20 required design answers

**1. What exactly is a replenishment recommendation?**
A `ReplenishmentPlan` row in `RECOMMENDED` status: a single, specific,
already-computed proposal — "destination store X needs N more of product
P, sourced from [this one supplier / this one other store], because the
authoritative position calculation (Section 2) shows a shortfall of S
after accounting for on-hand, inbound transfers, and open POs." It is not
an ephemeral preview — generating recommendations persists rows (unlike
M8's purely computed, throwaway suggestions), because the task requires
recommendations to be reviewed, approved, and later re-validated at
execution, which requires something durable to review/approve/re-validate
*against*.

**2. When can a recommendation become an executable supply action?**
Only after an explicit `APPROVE` transition by a user holding
`supply_chain.approve`, and only while the plan is still `APPROVED` (not
`STALE`/`CANCELLED`/already `EXECUTED`) at the moment `EXECUTE` (requiring
`supply_chain.execute`) is called. Recommendation, approval, and execution
are three distinct actions by design (Design Decision 4) — never
collapsed.

**3. When should inventory be sourced from another store versus a
supplier?**
Deterministically, not ambiguously: a store is a candidate source only if
its *own* surplus (its own `current_qty_on_hand` minus its *own*
`reorder_point`) is positive — i.e. sourcing never eats into another
store's own trigger threshold (task Section 7's explicit requirement).
Candidate source stores are ranked by descending available surplus (ties
broken by ascending `store_id` for determinism) and consumed in that order
until the shortfall is met or sources are exhausted; whatever remains
unmet is sourced from the ranked supplier list (Design Decision 5/6).

**4. How are open purchase orders incorporated?**
Exactly as M8 already does: `Σ(quantity_ordered - quantity_received)`
across that product's `PurchaseOrderItem` rows on POs in
`DRAFT`/`ORDERED`/`PARTIALLY_RECEIVED` status, added to the position.

**5. How is in-transit stock incorporated?**
Exactly as M8 already does: `Σ(shipped_quantity - received_quantity)`
across `InterStoreTransferLine` rows on transfers in `SHIPPED` status,
keyed by `destination_product_id`, added to the position.

**6. How are outstanding transfer quantities incorporated?**
Same mechanism as #5 — "in-transit" and "outstanding transfer quantity"
are the same figure in this system (a transfer's only non-terminal,
inventory-affecting state is `SHIPPED`).

**7. How are supplier lead times incorporated?**
As a ranking input only (Design Decision 6), and as an explainability/
exception field ("expected arrival ≈ today + lead_time_days", surfaced so
a reviewer can judge urgency) — **not** as an input to the quantity
calculation itself. M9 introduces no forecasting; lead time does not
inflate the recommended quantity (that would be a demand-forecasting
policy decision explicitly out of scope — "Do not introduce machine-
learning forecasting in M9" — and lead-time-driven order-quantity
inflation is a forecasting policy, not a deterministic position
calculation).

**8. How are minimum order quantities handled?**
At the supplier-candidate-ranking step and again, authoritatively, at
execution: if the computed purchase quantity for a candidate supplier is
below that supplier's `SupplierProduct.minimum_order_quantity`, the
quantity is rounded **up** to the MOQ (never silently dropped or left
unmet) and this rounding is recorded on the plan's `reason`/audit trail
(Design Decision 7) — over-ordering to satisfy an MOQ is itself an
auditable, explained decision, not a silent side effect.

**9. How are pack sizes handled?**
The same rounding step as MOQ: after applying MOQ, the quantity is rounded
**up** to the next whole multiple of `SupplierProduct.pack_size` (default
1, i.e. no rounding, when unset). Order matters and is fixed:
`max(need, MOQ)` first, then round up to the nearest pack multiple —
documented in code as the single authoritative rounding function
(Design Decision 7) so no two call sites can compute a different final
quantity for the same inputs.

**10. How are supplier prices selected?**
The `SupplierProduct` row for `(supplier, product)` with the greatest
`effective_date <= today` among `is_active = true` rows (Design Decision
2's effective-dating rule) — the same deterministic "latest not-future
effective row wins" rule used once, consistently, everywhere a price is
read (generation-time display, execution-time snapshot).

**11. How are multiple suppliers ranked?**
Deterministic total order (Design Decision 6): (1) `Supplier.is_active`
only — inactive suppliers are never candidates, full stop; (2) is this
`Product.default_supplier_id` (the "preferred" supplier) — preferred
sorts first regardless of price; (3) lowest current unit cost; (4) fewer
MOQ/pack-size rounding units wasted (a proxy for "less over-ordering
required"); (5) shorter `lead_time_days`; (6) lowest `supplier_id` as the
final, fully deterministic tiebreak (never "arbitrary row order" — the
task's explicit warning).

**12. How are stockouts prioritized?**
`Product.current_qty_on_hand <= 0` (or, more precisely, the authoritative
position ≤ 0 — see Design Decision 1) is always `urgency = 'URGENT'`
regardless of the configured `minimum_stock_quantity`; below
`minimum_stock_quantity` (when set) is also `URGENT`; below
`reorder_point` but at/above minimum is `NORMAL`. Urgency is a plain,
explainable classification on every generated plan, not a separate
subsystem.

**13. How are urgent versus normal replenishment differentiated?**
Exactly the classification in #12, stored on `ReplenishmentPlan.urgency`
at generation time (a snapshot, like the quantity) — used for sorting/
filtering in the API and UI, never for a different calculation path.

**14. How does a recommendation become a PO?**
`EXECUTE` on an `APPROVED` plan with `source_type = 'SUPPLIER'` calls the
new non-committing `_create_purchase_order_inner` (Design Decision 3)
inside the same transaction that re-validates the position, re-reads the
current supplier price, sets the plan's `status = 'EXECUTED'` and its
`generated_purchase_order_id` FK, writes the audit entries, and commits
once. The resulting PO is created in `DRAFT`, byte-for-byte what
`create_purchase_order` would have produced for a manually-entered
identical line — submission to `ORDERED` remains a separate, later,
manual action through the *existing* purchasing endpoint (Design
Decision 9).

**15. How does a recommendation become an inter-store transfer?**
The same shape as #14 but for `source_type = 'TRANSFER'`, calling the new
non-committing `_create_transfer_inner`, setting
`generated_transfer_id`. The resulting transfer is created in `DRAFT`;
shipping/receiving remain the existing M8 `ship_transfer`/
`receive_transfer` actions, untouched (Design Decision 9).

**16. How do we prevent duplicate replenishment?**
Three independent layers (Design Decision 4/8): (a) execution locks the
`ReplenishmentPlan` row (`SELECT ... FOR UPDATE`) first and is
idempotent-by-state (an already-`EXECUTED` plan returns its existing
linked document unchanged, exactly like every other status-transition
function in this codebase); (b) execution additionally accepts a
client-supplied `client_transaction_id`, stored uniquely on the plan,
giving the same pre-lock-fast-path / post-lock-re-check / `IntegrityError`
-recovery discipline `ship_transfer` already uses, as defense in depth
against a retried HTTP request racing a *second, independent* execute
call; (c) a DB-level `UNIQUE` constraint on
`generated_purchase_order_id`/`generated_transfer_id` makes "two plans
pointing at the same PO" structurally impossible even if application logic
were ever wrong.

**17. How do we prevent over-ordering?**
The executed quantity is capped at `min(plan.approved_quantity,
current_shortfall-at-execution)` (Design Decision 8) — approval is a
ceiling a human explicitly signed off on; the system may execute for
*less* than approved (if reality improved) but never *more*.

**18. What happens when the underlying inventory changes between
recommendation and execution?**
**Transactional revalidation at execution** — the task's own named safest
default (Design Decision 8). Under the same lock used to prevent
duplicate execution, the authoritative position (Design Decision 1) is
recomputed fresh; if the shortfall has dropped to zero or below, the plan
is marked `STALE` (not silently executed for the old, now-wrong quantity,
and not silently auto-cancelled either — `STALE` is its own visible,
audited terminal-except-for-cancellation state) and execution is refused
with a named error citing both the original and current numbers. If the
shortfall shrank but is still positive, execution proceeds for the
smaller, revalidated quantity (a documented partial-execution case, not
an error).

**19. How are approvals controlled?**
By permission (`supply_chain.approve`), not by a monetary threshold — the
task explicitly says "design them explicitly rather than hardcoding
arbitrary limits," and no requirement or existing precedent in this
codebase (AP, purchasing, transfers) defines a dollar-threshold approval
ladder; introducing one here would be inventing new financial policy
without a specification for it. Every plan, regardless of value, requires
one `APPROVE` transition before it is executable — a single, explicit,
uniform control.

**20. What is the accounting impact, if any, before goods are received?**
**None, by construction, not by a special-cased check.** Recommendation
generation, approval, and execution only ever call
`_create_purchase_order_inner`/`_create_transfer_inner` — the exact same
DRAFT-creating code paths manual PO/transfer creation already uses, and
those paths have never called `accounting_service` (verified by reading
both in full). No new "planning journal" is introduced. The first
accounting effect a supply chain event can ever have remains exactly where
M3/M6/M7 already put it: a goods receipt (inventory + WAC) and a posted
supplier invoice (AP) — see Design Decision 9 and Session N of the test
plan.

## Numbered design decisions

**Design Decision 1 — One authoritative position/shortfall function,
extended from M8, not replaced.**
`app.modules.replenishment.service.compute_position` becomes the single
place `on_hand + inbound_transfer_qty + open_purchase_order_qty` (M8's own
formula, unchanged) is computed; `get_replenishment_suggestions` (M8's
read-only report, kept working unmodified — its own tests must stay
green) and M9's new `generate_replenishment_plans` both call it, so the
two can never silently drift into different formulas. "Committed/
reservations" (the task's optional third input) does not exist anywhere
in this codebase (no sales-reservation concept was ever built) — omitted,
not fabricated.

**Design Decision 2 — `SupplierProduct`: one new table serves as both
catalog and effective-dated price history.**
Rather than a separate "current price" table plus a separate "price
history" table (two representations of the same fact, which the task's
own Section 6 warns against), a single `supplier_products` table holds one
row per `(supplier_id, product_id, effective_date)`. Changing a price
never mutates an existing row — it inserts a new one with a later
`effective_date`. "The current price" is deterministically "the
`is_active` row with the greatest `effective_date` not after today" — one
rule, one function, used everywhere a price is read. A `PurchaseOrderItem`
freezes whatever price was read at execution time (via the *existing*
`unit_cost` column — no new column needed there), so a later price change
provably cannot alter a historical PO (verified by a mutation test).
Fields, each with a demonstrated purpose per the task's own field list:
`supplier_id`, `product_id`, `supplier_sku` (nullable — supplier's own
catalog number, for the generated PO's line reference/explainability),
`pack_size` (`Numeric`, default 1), `unit_cost` (`Numeric(14,6)`, matching
every other cost column in this codebase), `minimum_order_quantity`
(`Numeric`, nullable = no MOQ), `lead_time_days` (`Integer`, nullable —
lives *here*, not on a separate policy table, because lead time is a
property of what a specific supplier can do for a specific product, not
of a destination store; putting it on a per-store policy row would be the
exact "duplicate representation" the task warns against), `effective_date`
(`Date`), `is_active` (`Boolean`). No "purchase_unit" field was added —
`Product.unit_of_measure` already exists and a second, possibly-
conflicting "purchase unit" per supplier is a real-world SKU-remapping
problem this system has no existing machinery for (M8 explicitly punted
the analogous cross-store SKU-remapping question to an "explicit override"
field, never a unit-conversion engine) — deferred (Section 9 "Known
limitations").

**Design Decision 3 — Refactor `create_purchase_order`/`create_transfer`
into a non-committing inner function plus an unchanged-behavior outer
wrapper.**
`create_purchase_order` becomes `_create_purchase_order_inner(...)` (all
existing validation/creation logic, no `commit()`/`refresh()`) plus
`create_purchase_order(...)` = call inner, `commit()`, `refresh()`,
return — byte-identical external behavior, verified by running the full
*existing* `test_purchasing*.py`/`test_ap*.py` suite unchanged after the
refactor. `create_transfer` gets the identical treatment. M9's execution
service calls the `_inner` functions directly, inside its own
lock-revalidate-create-link-audit-commit transaction. This is the same
shape M8 already applied to `create_stock_adjustment` — not a new pattern
invented for M9.

**Design Decision 4 — Recommendation, Approval, Purchase Order, and
Transfer are four distinct concepts, never collapsed.**
`ReplenishmentPlan.status` lifecycle: `RECOMMENDED → APPROVED → EXECUTED`,
with `STALE` reachable from `RECOMMENDED` or `APPROVED` (detected only at
an execution attempt — see Design Decision 8; there is no background job
that proactively marks plans stale) and `CANCELLED` reachable from
`RECOMMENDED`/`APPROVED`/`STALE` (never from `EXECUTED` — mirrors M8's
"never cancel a POSTED stock count" precedent exactly). Five stored
states, not the task's own suggested six (`SUGGESTED → REVIEWED →
APPROVED → EXECUTED → COMPLETED`) or its frontend section's six
(`RECOMMENDED/APPROVED/EXECUTED/FULFILLED/STALE/CANCELLED`) — reconciled
by treating `FULFILLED` as a **derived**, not stored, label: once
`EXECUTED`, the API/UI computes "fulfilled" by checking the linked PO's
receipt completeness (`quantity_received == quantity_ordered` on every
line) or the linked transfer's receipt completeness, the same way M8
already treats a transfer's own "received" state as derived rather than
stored (`PurchaseInvoice.balance_due`-style precedent). `REVIEWED` was
dropped: nothing operationally different happens at "reviewed" versus
"approved" in this design (no separate reviewer-vs-approver role is
requested anywhere else in the task), so keeping it would be exactly the
"unnecessary state" the task itself warns against.

**Design Decision 5 — Multi-store sourcing splits a shortfall across
sibling plans, never one plan with multiple sources.**
A `ReplenishmentPlan` always has exactly one source (one supplier, or one
source store) — matching Section 8's field list (`source`, `source_type`,
`suggested_quantity`, all singular). When one shortfall needs more than
one source store to cover (the task's own A→B:50, C→B:30 example),
`generate_replenishment_plans` creates **multiple sibling rows** sharing a
`generation_batch_id` (a UUID stamped once per generation call, purely for
grouping/explainability — not a new lifecycle entity; the task explicitly
says to introduce a plan/batch concept "if useful" and to "not create
unnecessary states"), each independently reviewable, approvable, and
executable. Their `suggested_quantity` values sum to at most the
shortfall computed at generation time; "remaining need" for a shortfall is
computed on read as `needed_quantity - Σ(suggested_quantity of EXECUTED
siblings)` — no separate stored running total, avoiding a second
representation of the same fact.

**Design Decision 6 — Deterministic ranking, computed by two small pure
functions, not query row order.**
`rank_source_stores(db, product, shortfall)` returns source-store
candidates sorted by descending own-surplus (ties by ascending
`store_id`), consuming from each until the shortfall is met, per Design
Decision 5. `rank_suppliers(db, product, quantity)` returns
`SupplierProduct` candidates sorted by the six-tier rule in design answer
#11. Both are ordinary Python sorts over an already-fetched, small
candidate list (bounded by "how many stores/suppliers carry this SKU," not
by total system size) — no query-level `ORDER BY` is trusted to encode the
business ranking.

**Design Decision 7 — One rounding function, used everywhere a purchase
quantity is finalized.**
`apply_moq_and_pack_rounding(need, moq, pack_size)` = `ceil_to_pack(max(need,
moq or 0), pack_size or 1)`, implemented once in
`app.modules.replenishment.service`, called at plan generation (for the
*displayed* suggested quantity) and again at execution (recomputed fresh
against the revalidated need, per Design Decision 8) — never duplicated
inline at either call site.

**Design Decision 8 — Execution is one transaction: lock plan, lock
affected product(s), revalidate, round, create document, link, audit,
commit.**
Mirrors M8's `post_stock_count`/`ship_transfer` locking discipline
exactly: lock the `ReplenishmentPlan` row first (serializes concurrent/
retried execution of the *same* plan), then the destination product row
(and, for a transfer, the source product row, in ascending `product_id`
order alongside the destination when both must be locked — the same
deadlock-safe convention used everywhere else in this codebase), then
recompute the authoritative position (Design Decision 1) under those
locks, apply the cap from design answer #17 and the rounding from Design
Decision 7, create the DRAFT PO/transfer via the `_inner` function (Design
Decision 3), set `status='EXECUTED'` and the link FK, write one audit
entry recording original vs. executed quantity/price when they differ,
and commit once. No separate "reservation" table is introduced — the task
lists reservation as one of several acceptable strategies and explicitly
endorses transactional revalidation as "usually" the safest default; a
reservation system would need its own expiry/release/concurrency model
for a benefit (blocking a *different* plan from being generated against
the same shortfall while this one is mid-review) that duplicate
generation already can't produce incorrect results from, since every
generation call recomputes the position fresh from real rows, not from
a previous recommendation's stale number.

**Design Decision 9 — Executed documents are indistinguishable from
manually created ones, and never advance past DRAFT.**
Submission (`submit_purchase_order`) and shipment (`ship_transfer`) remain
separate, later, manual actions through the *existing* endpoints/
permissions (`purchasing.write`+submit, `inventory.transfer.ship`) — M9
never calls them. This keeps the accounting boundary (design answer #20)
true by construction rather than by a special-cased guard, and keeps
"generated PO/transfer must behave exactly like a manually created one"
literally true: after `EXECUTE`, the only fact distinguishing a generated
PO from a hand-entered one is the nullable `replenishment_plan_id` FK on
`PurchaseOrder`/`InterStoreTransfer` pointing back at the plan that
created it (for traceability, per Section 9/10's "link PO/transfer back
to the replenishment plan") — every other column, every subsequent
lifecycle transition, and every accounting consequence is identical.

**Design Decision 10 — Replenishment "policy" fields live on `Product`,
not a new (store, product)-keyed table.**
`Product` is already 1:1 with `(store, product)` (re-confirmed in Phase
0). A separate `ReplenishmentPolicy` table keyed by `(store_id,
product_id)` would be a redundant second identity for the exact same row
— the precise "duplicate representation" the task's own Section 6 warns
against, just relocated to Section 1. `reorder_point` and
`default_supplier_id` already exist and are reused unchanged; two new
nullable columns are added directly to `Product`:
`target_stock_quantity` (the level a recommendation aims to restore stock
*to* — defaults to `reorder_point` when unset, so existing M8 data needs
no backfill to keep working) and `minimum_stock_quantity` (the urgent-
priority floor, design answer #12; distinct from `reorder_point`,
collapsed with "safety stock" into one field rather than two synonymous
ones the task warns against multiplying). `is_active` (already exists) is
the field's "active/inactive." No effective-dating is added to these
fields — unlike a price, an out-of-date reorder point does not need to be
provably preserved on a historical document; changing it is a simple
update, and the task itself says "effective dates if required," judged
not required here.

## What M9 explicitly does NOT do (deferred; see the hardening audit's
"Known limitations" for the complete list)

No machine-learning or trend-based forecasting (explicit task
instruction). No lead-time-driven order-quantity inflation (design answer
#7). No purchase-unit conversion distinct from `Product.unit_of_measure`
(Design Decision 2). No monetary-threshold approval ladder (design answer
#19). No reservation subsystem (Design Decision 8). No background job that
proactively marks plans `STALE` — staleness is only ever detected at an
actual execution attempt, which is the only moment it has a financial
consequence to protect against.
