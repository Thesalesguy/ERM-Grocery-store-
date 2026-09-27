# M24 Discovery: Inter-Store Transfer Lifecycle and Inventory-in-Transit Integrity

**Status:** Discovery only. No production code, migration, seed data, test, or
frontend file changed. Starting HEAD: `08f397e` (M23B commit).

## 1. Executive summary

The M21/M22 finding ("inter-store transfers have no cancellation/loss/write-off
path after shipment, leaving potential unreconciled Inventory In Transit
balances") is **re-confirmed accurate at HEAD `08f397e`, with more precision
than the original finding stated.** The forward path (DRAFT → SHIPPED →
possibly-partial RECEIVED, or DRAFT → CANCELLED) is complete, well-tested,
correctly locked, correctly costed, and correctly accounted — no defect was
found anywhere in the path the system actually supports. The gap is exactly
where M21 (Finding F3) said it was: **once a transfer is SHIPPED, there is no
state, event, or code path that ever moves it anywhere else.** A transfer
whose goods are lost, damaged, or simply never received stays permanently
`SHIPPED` forever, and its `Inventory In Transit` GL balance never zeroes.

Two things this discovery adds beyond the original finding:

1. **The stock ledger itself is never wrong.** A lost shipment's source store
   already correctly shows the goods gone (`TRANSFER_OUT` posted at ship
   time) and the destination store correctly shows they never arrived
   (nothing is incremented until a real receipt). The entire defect is
   **GL-only** — a permanent `Inventory In Transit` balance with no physical
   counterpart anywhere — not an inventory-quantity discrepancy. This means
   the eventual fix is an accounting-only compensating entry with **no**
   paired `InventoryMovement`, a genuinely new shape for this codebase (every
   other `post_*_journal` call is paired with a real operational movement).
2. **Detection already exists and works**, it is just not automatic.
   `transfers/service.py::inventory_in_transit_reconciliation` (company-wide
   GL-vs-subledger comparison) and `reports/service.py::inventory_in_transit`
   (a live, per-line, per-transfer drill-down) are both real, tested,
   reachable via API, and the drill-down is rendered on the Reports page.
   An operator who looks can always find every currently-outstanding
   in-transit line and its age since shipment. There is no automatic alert,
   timeout, or staleness flag — someone has to go look.

No defect was found in locking, idempotency, costing, or store isolation for
any operation the system actually implements. Every genuine gap traces to the
same root cause M21 already identified: no terminal state exists for a
SHIPPED transfer other than "eventually fully received."

## 2. Current transfer state machine

```
        create_transfer                ship_transfer              receive_transfer (1..N times)
              │                              │                            │
              ▼                              ▼                            ▼
          [DRAFT] ───cancel_transfer───► [CANCELLED]   [DRAFT] ──► [SHIPPED] ──► (fully RECEIVED, derived)
                                                                        │
                                                                        └──► (partially RECEIVED, derived) ──► stays SHIPPED forever
                                                                              if no further receipt ever arrives
```

`TRANSFER_STATUSES = ("DRAFT", "SHIPPED", "CANCELLED")`
(`backend/app/modules/transfers/models.py:40`) — three stored values, no
more. `_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)` — cancellation is legal
from exactly one state. "RECEIVED" is never a stored status; it is the
derived fact `status == "SHIPPED"` and every line's `received_quantity ==
shipped_quantity` (`docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design Decision 7,
deliberately, for the same reason `PurchaseInvoice.balance_due` is never
stored). There is no `LOST`, `DAMAGED`, `WRITTEN_OFF`, `REJECTED`, or
`RETURNED_TO_SOURCE` state anywhere in the model, the migration, or any
constant. **SHIPPED has no successor state at all** — it is a true dead end
for a transfer that is never fully received.

### States/events checklist (per the brief's list of 17)

| # | State/event | Exists? | Evidence |
|---|---|---|---|
| 1 | created/draft | Yes | `create_transfer` → `status="DRAFT"` |
| 2 | approved | **No — deliberately** | M8 Design Decision 7: "REQUESTED and APPROVED are deliberately NOT separate states" |
| 3 | shipped/dispatched | Yes | `ship_transfer` → `status="SHIPPED"` |
| 4 | in transit | Yes, but derived only | `status=="SHIPPED"` with `received_quantity < shipped_quantity` on any line — never a stored value |
| 5 | received | Yes, but derived only | every line's `received_quantity == shipped_quantity`; `status` itself never changes |
| 6 | cancelled before shipment | Yes | `cancel_transfer`, `_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)` |
| 7 | cancelled after shipment | **No** | `cancel_transfer` raises `INVALID_TRANSFER_STATE` for any non-DRAFT status |
| 8 | lost in transit | **No** | no code path, no status value, no source_type, zero repo mentions |
| 9 | damaged in transit | **No** | same as above |
| 10 | written off | **No** | same as above |
| 11 | partially received | Yes, but derived only | same shape as "received," just `received_quantity` strictly between 0 and `shipped_quantity` |
| 12 | over-received | Rejected, not a state | `receive_transfer` raises `OVER_RECEIPT` if `quantity_received > shipped_quantity - received_quantity` |
| 13 | under-received | Yes, but derived (= "partially received") | same as #11 |
| 14 | duplicate receipt | Handled as idempotency, not a state | `client_transaction_id` fast-path + DB unique constraint + `IntegrityError` recovery returns the original receipt |
| 15 | rejected receipt | **No** | no concept of a receiving party refusing goods; every `receive_transfer` call is assumed accepted |
| 16 | returned to source | **No** | no reverse-transfer concept exists |
| 17 | other terminal states | None found | `CANCELLED` (DRAFT-only) is the only true terminal state besides fully-RECEIVED |

No state was invented for this table merely because it would be useful — every
"No" is a verified absence (model CHECK constraint, source_type list, and
repo-wide grep all agree).

## 3. Complete transfer lifecycle trace

For each existing state/event (`backend/app/modules/transfers/service.py`):

**`create_transfer` (DRAFT)**
- Trigger: any authenticated user holding `inventory.transfer.write`.
- Store scope: `caller_store_id` must be `None` (unrestricted) or equal to
  **either** `from_store_id` **or** `to_store_id` — not both (Finding F7,
  Section 9 below).
- Quantity rules: every line's `requested_quantity > 0`; destination product
  resolved by SKU match in the destination store, never auto-created.
- Inventory movements: none.
- GL movements: none.
- Transaction boundary: single transaction, `db.commit()` at the end of the
  thin wrapper (`_create_transfer_inner` does the work without committing,
  for M9's replenishment-execution reuse).
- Idempotency: none needed — a DRAFT has no side effect to duplicate; two
  identical requests simply create two DRAFT transfers (a real, accepted
  behavior, unchanged from M8).
- Locking: none — no row anywhere is mutated except the new insert.
- Audit: `TRANSFER_CREATED`.
- Predecessor: none (initial state). Successor: `SHIPPED` or `CANCELLED`.

**`ship_transfer` (DRAFT → SHIPPED)**
- Trigger: `inventory.transfer.ship`, and `caller_store_id` must equal
  `transfer.from_store_id` (or be unrestricted) — the **source** store only.
- Quantity rules: per-line `quantity_to_ship > 0` and `<=
  requested_quantity` (a genuine partial shipment is legal;
  `OVER_SHIPMENT` otherwise); a line omitted from the request ships 0.
- Inventory movements: one `TRANSFER_OUT` per shipped line, at the source
  store, `quantity_delta = -quantity_to_ship`, `unit_cost_at_movement =
  product.current_cost` (frozen into `unit_cost_at_shipment` at this exact
  moment).
- GL movements: `post_transfer_shipment_journal` — `Dr Inventory In Transit /
  Cr Inventory`, posted against the source store, for
  `Σ quantity_to_ship × unit_cost_at_shipment`.
- Transaction boundary: one transaction; `db.flush()` only inside
  `ship_transfer` itself, committed by the route handler.
- Idempotency: `ship_client_transaction_id`, unique on the transfer row —
  pre-lock fast path, post-lock re-check, `IntegrityError` recovery
  fallback (identical shape to `receive_goods`).
- Locking: transfer header row `with_for_update()` first, then every
  distinct **source** product row via `lock_product_for_update`, ascending
  id order.
- Audit: `TRANSFER_SHIPPED`.
- Predecessor: `DRAFT` only (`INVALID_TRANSFER_STATE` otherwise). Successor:
  no further status transition ever (only receipts, which don't change
  `status`) — this is the crux of the finding.

**`receive_transfer` (SHIPPED, 1..N events)**
- Trigger: `inventory.transfer.receive`, `caller_store_id` must equal
  `transfer.to_store_id` (or unrestricted) — the **destination** store only.
- Quantity rules: per line, `quantity_received > 0` and `<=
  (shipped_quantity - already-received)` across all prior receipts;
  `OVER_RECEIPT` otherwise.
- Inventory movements: one `TRANSFER_IN` per received line, at the
  destination store, `quantity_delta = +quantity_received`, valued at the
  frozen `unit_cost_at_shipment`, rolled into the destination product's WAC
  via `compute_new_wac` (the same function goods receiving uses).
- GL movements: `post_transfer_receipt_journal` — `Dr Inventory / Cr
  Inventory In Transit`, posted against the destination store, for
  `Σ quantity_received × unit_cost_at_shipment` (the same frozen cost, never
  the destination's own post-receipt WAC — would be circular).
- Transaction boundary: one transaction per receipt event; `db.flush()`
  inside the function, committed by the route handler.
- Idempotency: `client_transaction_id`, unique on the receipt row — same
  fast-path/re-check/`IntegrityError`-recovery shape.
- Locking: transfer header row first, then every distinct **destination**
  product row, ascending id order.
- Audit: `TRANSFER_RECEIPT_COMPLETED`.
- Predecessor: `status == "SHIPPED"` only. Successor: none (status never
  changes; the transfer simply accumulates `received_quantity` until fully
  received, or stops accumulating forever if goods never arrive).

**`cancel_transfer` (DRAFT → CANCELLED)**
- Trigger: `inventory.transfer.write`, `caller_store_id` must be `None` or
  **either** `from_store_id` or `to_store_id` — either party may cancel a
  DRAFT.
- Quantity rules: none (no inventory has moved).
- Inventory movements: none.
- GL movements: none.
- Transaction boundary: single transaction, commits at the end.
- Idempotency: cancelling an already-`CANCELLED` transfer is a no-op
  (`return transfer` before the state-check, mirroring
  `void_purchase_invoice`'s own idempotent-no-op pattern).
- Locking: transfer header row only — nothing else needs it, since a DRAFT
  transfer has touched no product.
- Audit: `TRANSFER_CANCELLED` (with an optional free-text `reason`, recorded
  only in the audit log's `after` payload, not on the transfer row itself).
- Predecessor: `DRAFT` only (`INVALID_TRANSFER_STATE` for any other status,
  including `SHIPPED`). Successor: terminal.

## 4. Inventory movement trace

Every transfer-related movement type (`backend/app/modules/inventory/models.py`
`MOVEMENT_TYPES`): exactly `TRANSFER_OUT` (decreasing) and `TRANSFER_IN`
(increasing) — no `TRANSFER_LOSS`, `TRANSFER_WRITE_OFF`, or any other
transfer-adjacent movement type exists. `REFERENCE_TYPES` includes
`inter_store_transfer` as the single reference type both movement types use
(`reference_id = transfer.id`), so both legs of one line's journey are
traceable back to the same transfer, but there is no distinct movement `Product` for what would be an inventory-side write-off — **because there is
nothing to write off on the inventory side**: the source's `TRANSFER_OUT` and
the (never-posted) destination `TRANSFER_IN` already correctly represent
physical reality. A stock adjustment at either store cannot fix a lost
in-transit shipment, because neither store's `current_qty_on_hand` is wrong —
the goods were never counted as on-hand at the destination, and were already
correctly removed from the source's on-hand count at ship time.

## 5. GL/journal trace

Two `post_*_journal` functions, both in `AUTOMATED_SOURCE_TYPES`
(`accounting/models.py`) — meaning `reverse_journal_entry`'s generic
mechanism is correctly blocked from touching either, for the same reason
every other automated source is blocked (a bare journal reversal would not
undo the real inventory movement):

- `post_transfer_shipment_journal`: `Dr Inventory In Transit (1520) / Cr
  Inventory (1500)`, `source_type="INTER_STORE_TRANSFER_SHIP"`,
  `source_id=transfer.id`, `store_id=transfer.from_store_id`, `posting_date
  = transfer.shipped_at.date()` (server-set, never backdatable — re-verified
  in this discovery, consistent with `docs/M23_DISCOVERY.md` Phase 6's
  corrected finding about server-timestamp-derived posting dates).
- `post_transfer_receipt_journal`: `Dr Inventory / Cr Inventory In Transit`,
  `source_type="INTER_STORE_TRANSFER_RECEIVE"`, `source_id=receipt.id`,
  `store_id=transfer_receipt.store_id`, `posting_date =
  transfer_receipt.received_date` (a genuine, caller-supplied,
  backdatable request field).
- **No P&L impact from either** — both legs are pure balance-sheet
  reclassification of the same asset (M8 Design Decision 9), which is
  exactly why a permanently-stuck `Inventory In Transit` balance is a real
  problem: it silently overstates total assets forever, with nothing ever
  correcting it back down through the P&L (there is no expense recognized
  for the loss unless a future milestone builds one).
- Both `post_*_journal` calls pass through `_post_journal`'s
  `_enforce_period_open` gate exactly like every other posting in the
  system (M22, re-verified unchanged) — a shipment or receipt dated into a
  closed period is correctly refused, with the same "sharp edge" already
  documented for every other forward-posting function.

## 6. Inventory In Transit reconciliation analysis

`transfers/service.py::inventory_in_transit_reconciliation` compares the GL's
`Inventory In Transit` balance (`trial_balance()`, company-wide, no
`store_id` filter — deliberately, since the account represents value
*between* stores) against `Σ (shipped_quantity − received_quantity) ×
unit_cost_at_shipment` across every line where `shipped_quantity >
received_quantity`. **This correctly detects any divergence between the GL
and the transfer subledger** (e.g., a bug that posted the wrong amount) — it
is a real, working, tested reconciliation.

**What it cannot detect:** a transfer that is legitimately, permanently
"lost" is, by construction, indistinguishable from one that is still
genuinely in transit and will be received tomorrow — both look identical to
this reconciliation (a line with `shipped_quantity > received_quantity` and
a matching GL balance). The reconciliation proves **internal consistency**
(the GL agrees with what the subledger says is outstanding), not **business
correctness** (whether "outstanding" actually still means "in transit" or
has quietly become "gone"). This is the precise, evidence-based version of
the M21 finding: the GL balance is never *wrong* relative to the subledger,
it is simply *permanently unresolvable* once goods stop moving, and nothing
in the system distinguishes "will arrive eventually" from "will never
arrive."

**Can an unresolved balance currently be detected?** Yes, in the sense that
`reports/service.py::inventory_in_transit` (rendered on the Reports page,
per-line, per-transfer, with `from_store_id`/`to_store_id`/quantity/value)
lets a human operator see every outstanding line and its two stores at any
time. **No**, in the sense that nothing computes or surfaces *how long* a
line has been outstanding, and nothing alerts anyone automatically — an
operator has to think to look.

## 7. Cost/WAC (costing) analysis

- Valuation basis: Weighted Average Cost, the same basis used everywhere
  else in this system (purchasing, sales COGS).
- Cost capture point: `unit_cost_at_shipment` is captured **at ship time**,
  from the **source** product's `current_cost`, under that product's row
  lock (`ship_transfer`, line ~381) — frozen permanently on the
  `InterStoreTransferLine` row from that moment on.
- Frozen at shipment or receipt? **Shipment.** Every subsequent receipt
  event (there may be several, for one line) reuses the exact same frozen
  value — never the source's current cost at receipt time, and never the
  destination's own WAC.
- Partial receipt costing: each partial receipt event costs its
  `quantity_received` at the same frozen `unit_cost_at_shipment` — no
  re-averaging, no re-derivation.
- Loss/write-off valuation: **undefined**, since no loss/write-off path
  exists. If one is built, the only value with any evidentiary basis is the
  same frozen `unit_cost_at_shipment` already used for both existing legs —
  using anything else would require a new, unjustified valuation policy.
- Same cost used by inventory and GL? **Yes, provably** — `ship_transfer`
  computes `total_shipped_value` from the exact same
  `(quantity, unit_cost_at_shipment)` pairs used for each `TRANSFER_OUT`
  movement, and passes that single number into
  `post_transfer_shipment_journal` (never independently recomputed,
  matching this codebase's "never independently compute two sides of a pair
  that must match" rule). Same structure for receipt.
- Race between WAC changes and transfer operations: **none exists.** Because
  the cost is captured once, under a row lock, at ship time, and never
  re-read from the source product afterward, a source WAC change between
  shipment and any later receipt event has zero effect on the transfer's
  own valuation. The only race that could matter — two operations reading
  the source product's `current_cost` concurrently at ship time — is
  already prevented by `lock_product_for_update`.

## 8. Store-isolation analysis

Traced directly in `transfers/service.py` (all four mutating functions) and
`api/v1/endpoints/transfers.py`:

| Operation | Authorization check | Verified correct? |
|---|---|---|
| `create_transfer` | `caller_store_id in (None, from_store_id, to_store_id)` | Yes |
| `ship_transfer` | `caller_store_id in (None, transfer.from_store_id)` | Yes — destination-store users and unrelated stores are both rejected |
| `receive_transfer` | `caller_store_id in (None, transfer.to_store_id)` | Yes — source-store users and unrelated stores are both rejected (directly tested: `test_store_scoped_source_user_cannot_receive`) |
| `cancel_transfer` | `caller_store_id in (None, from_store_id, to_store_id)` | Yes |
| `get_transfer` / `list_transfer_receipts` | 404 (not 403) for a store-scoped caller not on either side | Yes, matches the established 404-not-403 direct-ID-read convention |
| `list_transfers` | store-scoped caller sees only transfers touching their store on either side | Yes |

**The brief's specific question** — can an authenticated Manager from Store A
manipulate a transfer whose source and destination are both outside Store A?
**No, for every operation.** Every check above is `caller_store_id in
(from_store_id, to_store_id)` or a single-store equality — Store A never
appears in that set, so `create`, `ship`, `receive`, and `cancel` all raise
`ForbiddenError`/`STORE_ACCESS_DENIED` for such a caller, and reads 404. No
source/destination ID substitution, forged transfer ID, or cross-store
object access was found to succeed anywhere. This matches M21's own
adjacent-bypass sweep finding that transfers were not among the isolation
gaps it found (F1/F5/F6 were HR/AP; transfers were never flagged as a
isolation defect, only as an authorization-*symmetry* question, F7 below).

No new store-isolation defect was found. This is a clean result, not an
oversight — the same locking-then-checking discipline used everywhere else
in this codebase (M6/M7/M9) is applied consistently here too.

## 9. Authorization analysis (transfer creation/approval — Finding F7)

**Current documented behavior:** `inventory.transfer.write` is required to
create a transfer, and the caller must be scoped to **either** the source
**or** the destination store — not both. Held by `Manager` and `Inventory
Clerk` (`app/modules/auth/permissions.py`, re-verified at HEAD `08f397e`);
not held by `Cashier` or `Auditor`. There is no separate approval permission
or approval state (`docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design Decision 7,
explicit: "REQUESTED and APPROVED are deliberately NOT separate states...
creating a transfer already requires `inventory.transfer.write` at *both*
the source and destination store's operators' discretion" — this sentence
describes the *intent* that either side can act, but the *code* only ever
checks that the caller is on **one** side, meaning a Store-B user can name
Store A as source or destination without any Store-A user's own consent).

**Classification: unchanged from M21 — INCONSISTENT BEHAVIOR / UNRESOLVED
BUSINESS DECISION**, not an authoritative existing policy. The design doc's
prose describes symmetric discretion; the code implements one-sided
authorization. `docs/M21_DISCOVERY.md` Finding F7 (MEDIUM: "bounded impact —
no inventory moves, no data leak — both parties can already see the transfer
once drafted") and `docs/M22_DISCOVERY.md`/`docs/M23_DISCOVERY.md` both
carried this forward untouched. This discovery adds no new resolution:
**F7 remains open.** M24/M25 must not silently decide it.

**Answers to the brief's specific business-policy questions 1-4** (create,
approve, ship, receive) are IMPLEMENTED BEHAVIOR (Manager/Inventory Clerk at
one relevant store, no approval tier) but the *design intent* behind
one-sided creation authorization specifically (F7) is unresolved, per above.

## 10. Concurrency/idempotency analysis

Re-verified directly against the current source (`ship_transfer`,
`receive_transfer`, `cancel_transfer`), cross-checked against
`docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design Decision 10 and the existing
concurrency test suite (`tests/test_transfers_concurrency.py`, 5 scenarios,
5 iterations each):

| Race | Protection | Verified by |
|---|---|---|
| Two shipment requests, same transfer | Transfer header `with_for_update()` locked first; loser re-reads post-lock `status`/`shipped_quantity` under the lock, `OVER_SHIPMENT` if it would over-ship | `test_a_five_iterations_two_concurrent_shipments_of_same_transfer` |
| Two receipt requests, overlapping quantities | Same header-lock-first pattern; `OVER_RECEIPT` if it would exceed `shipped_quantity` | `test_b_five_iterations_overlapping_receives_never_exceed_shipped_quantity` |
| Shipment vs. cancellation | Header lock makes them mutually exclusive; cancellation only legal from DRAFT, so a shipment that wins moves to SHIPPED first, and cancellation (now seeing SHIPPED) is rejected | Design Decision 10, not separately tested but structurally guaranteed by the shared header lock + status re-check |
| Receipt vs. cancellation | Not reachable — cancellation is DRAFT-only, receipt is SHIPPED-only; the two states are mutually exclusive by construction, no shared code path to race | N/A — structurally impossible, not merely untested |
| Receipt vs. loss/write-off | **Not applicable — loss/write-off does not exist** | — |
| Two loss/write-off requests | **Not applicable** | — |
| Partial receipt vs. full receipt | Same header-lock-first + per-line remaining-quantity check under the lock; both are just `receive_transfer` calls, correctly serialized | `test_partial_shipment_and_multiple_partial_receipts` (sequential); concurrent overlapping case covered by scenario B above |
| Concurrent inventory/WAC changes | Source product row locked at ship time (freezes cost); destination product row locked at receipt time (for the WAC roll-forward) — `test_c_shipment_concurrent_with_unrelated_adjustment_on_source_product` | Confirmed |
| Duplicate shipment (retry) | `ship_client_transaction_id` unique constraint, pre-lock fast path + post-lock re-check + `IntegrityError` recovery | `test_ship_is_idempotent_by_client_transaction_id`, `test_d_concurrent_duplicate_ship_requests_create_only_one_shipment` |
| Duplicate receipt (retry) | `client_transaction_id` unique constraint, identical shape | `test_e_concurrent_duplicate_receive_requests_create_only_one_receipt` |

**Does the application rely on a status check alone?** No — every mutating
function locks the transfer header row *before* reading or checking
`status`, so the check is always against a fresh, lock-protected read, not a
stale one. **Can a valid terminal transition happen exactly once?** Yes for
every transition that exists (ship, one line's full receipt, cancel) — each
is provably idempotent and race-free by the table above. There is no
terminal transition to test for loss/write-off because none exists.

## 11. Failure-atomicity analysis

Every transfer-mutating function follows this codebase's universal pattern:
no `db.commit()` inside the service function itself for the multi-step
paths (`ship_transfer`, `receive_transfer` both end in `db.flush()` only;
the route handler commits once). This means:

- **Inventory movement succeeds, GL fails:** impossible to persist partially
  — `record_movement` and `post_transfer_shipment_journal`/
  `post_transfer_receipt_journal` execute in the same uncommitted
  transaction; an exception from either propagates up through the route
  handler without ever reaching `db.commit()`, so Postgres rolls back both
  together.
- **GL succeeds, inventory movement fails:** same reasoning — GL posting
  happens *after* all `record_movement` calls in `ship_transfer`'s and
  `receive_transfer`'s source order, so this specific ordering can't occur,
  and even if it could, the same all-or-nothing commit boundary protects it.
- **Transfer status changes, inventory movement fails:** in `ship_transfer`,
  `transfer.status = "SHIPPED"` is set *after* every `record_movement` call
  in source order — not reachable as described; protected regardless by the
  shared transaction.
- **Inventory movement succeeds, status change fails:** same — status change
  is the last mutation before the audit log and GL post; any failure after
  a movement still rolls back the movement too.
- **Audit event fails:** `audit_service.log_event` only calls
  `db.add`/`db.flush()`, not `db.commit()` — a failure there rolls back
  everything in the same transaction, same guarantee.
- **Idempotency record succeeds, business operation fails:** the idempotency
  key (`ship_client_transaction_id` / receipt's `client_transaction_id`) is
  written to the **same row/table** the rest of the operation mutates, in
  the same transaction — there is no separate "idempotency record" that
  could commit independently of the business operation it guards.

No gap was found here. This is the same transactional-atomicity discipline
already verified for every other financial/inventory operation in this
codebase (M4's accounting module docstring, M6/M7's AP module, M22's
testing).

## 12. Accounting-period analysis

Both existing transfer postings already pass through `_enforce_period_open`
(M22, unchanged, re-verified). A **future** transfer-loss/write-off/
cancellation-after-shipment journal is not designed here, but this
discovery determines the following from the repository's own established
patterns (per `docs/M23A_POLICY.md` Section 3.5's reasoning, directly
applicable):

- **Post on the event date?** By the established pattern (every
  compensating/correction entry in this codebase — supplier payment
  reversal, credit note reversal, payroll reversal, and now purchase
  invoice void — posts at `date.today()`, never a historical date), a
  future transfer-loss entry would, by consistency, also post at
  `date.today()` (the date the loss/write-off is *recorded*, not
  necessarily the date the goods actually vanished, which may be unknown).
  This is a strong precedent, not a decision made here.
- **Require the current period to be open?** Yes, necessarily — any future
  posting goes through `_post_journal`, and there is no path in this
  codebase that bypasses `_enforce_period_open`.
  before this codebase change is desired, this is a fact about the
  architecture, not a proposal.
- **Ever attempt to post using the original shipment date?** Only if a
  future implementation copied `post_purchase_invoice_void_journal`'s
  *pre-M23B* behavior — which M23A/M23B just established as the *wrong*
  pattern for this exact reason (closed-period blocking). A future
  transfer-loss journal should not repeat that mistake, per the precedent
  M23A set, but this is not decided or implemented here.
- **Require a reversal?** Not applicable in the traditional sense — a loss
  entry would be a **new** entry (Dr Loss/Shrinkage Expense, Cr Inventory In
  Transit), not a reversal of the original shipment entry (the shipment
  entry itself was correct at the time; the loss is a new fact discovered
  later, exactly like `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE`'s existing
  role for ordinary stock-count shrinkage).
- **Require special closed-period behavior?** No evidence either way — this
  is exactly the kind of business-policy question this discovery is
  instructed not to invent.

**Classified: UNRESOLVED BUSINESS DECISION** (accounting treatment of
transfer loss, and the accounting date for corrective transfer events) —
carried into Section 17/20 below, with the one qualifier that *if* built,
the `date.today()` precedent is the only evidence-backed choice consistent
with this codebase's own established pattern.

## 13. Reporting/analytics analysis

| Surface | Transfer/in-transit representation | Consistent? |
|---|---|---|
| `inventory_in_transit_reconciliation` (transfers module) | GL balance vs. Σ outstanding line value, company-wide | Single source of truth |
| `reports/service.py::in_transit_reconciliation` | Thin wrapper reusing the above verbatim | Yes — no duplicate calculation |
| `reports/service.py::inventory_in_transit` | Per-line, per-transfer drill-down (from/to store, qty, value), excludes `CANCELLED` transfers | New, correctly scoped, rendered on the Reports page |
| P&L (`profit_and_loss`) | No transfer effect at all (by design — Design Decision 9, no P&L impact) | Correctly absent |
| Stock valuation (`inventory_valuation`-type reports) | In-transit stock is counted at **neither** store's on-hand valuation (`docs/M11_DESIGN.md` Section 5.3) — its own separate bucket | Correct — avoids double-counting |
| Store-level analytics | Each store's own operational stock reports never include in-transit goods (correct — you cannot sell what hasn't arrived) | Correct |
| Audit reports | `TRANSFER_CREATED`/`TRANSFER_SHIPPED`/`TRANSFER_RECEIPT_COMPLETED`/`TRANSFER_CANCELLED` all logged via `audit_service.log_event`; `inter_store_transfer`/`inter_store_transfer_receipt` are both classified in `audit/service.py`'s `_OR_STORE_ENTITY_MODELS`/`_DIRECT_STORE_ENTITY_MODELS`-equivalent join logic for store-scoped audit-log reading | Consistent with the rest of the audit system |

**Can an unresolved in-transit balance currently be detected?** Yes (Section
6) — via the reconciliation and drill-down reports, both already built and
reachable. **Is it ever surfaced automatically, or does it silently persist
until someone looks?** It silently persists — there is no scheduled job,
alert, or dashboard flag for "this line has been outstanding for N days."

## 14. Known defects

1. **No terminal state for a SHIPPED transfer that is never fully received**
   (the M21/M22 finding, re-confirmed). Not a coding bug — a deliberately
   deferred feature (`docs/M8_ADVANCED_INVENTORY_DESIGN.md` "Deferred /
   known limitations": "Cancelling or reversing a SHIPPED (in-transit)
   transfer — deferred, the same way M6 deferred voiding a paid invoice").
2. **A permanently-lost shipment's `Inventory In Transit` balance overstates
   total assets forever**, with no expense ever recognized and no
   accounting-side mechanism to correct it (Section 5). Direct financial
   consequence of #1.
3. **A permanently-stuck SHIPPED transfer silently distorts the
   replenishment report** (`docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design
   Decision 11): `inventory_position = current_qty_on_hand + Σ(shipped −
   received) on open inbound transfers + ...` counts the lost quantity as
   "still coming," permanently suppressing a legitimate re-order suggestion
   for a product that actually needs one. Not previously documented in M21
   (M21 noted the condition "is not even surfaced in the replenishment
   module's own exceptions list" — this discovery traces the *mechanism* of
   that distortion precisely).
4. **Transfer-creation authorization asymmetry (F7)** — unresolved business
   decision, unchanged since M21.
5. **No detection automation** for a stale in-transit line (Section 6/13) —
   detection is possible but manual-only.

No new defect beyond what M21/M22/M23 already identified was found in the
locking, idempotency, costing, or isolation layers — those are all correct
and well-tested for every path the system actually implements.

## 15. Severity classification

| # | Defect | Severity | Rationale |
|---|---|---|---|
| 1 | No terminal state for a stuck SHIPPED transfer | **HIGH** (unchanged from M21's own F3 rating) | Permanent, unbounded GL/asset distortion with no recovery path; grows without limit over the life of the business |
| 2 | Overstated assets / no loss expense recognized | **HIGH** | Direct financial-statement consequence of #1; compounds every time a shipment is lost |
| 3 | Replenishment report distortion | **MEDIUM** | Operational (stock-outs from suppressed reorder suggestions), not a financial-integrity defect, and self-limiting per affected SKU |
| 4 | Transfer-creation authorization asymmetry (F7) | **MEDIUM** (unchanged from M21) | Bounded impact — no inventory moves, no data leak, both parties can see the transfer — but genuinely undecided |
| 5 | No automated staleness detection | **LOW** | A working manual detection path already exists; this is a UX/operations gap, not a correctness defect |

## 16. Business-policy decisions already established

- Transfers are per-store documents; no company/organization entity exists
  above `Store` (consistent with `docs/M23A_POLICY.md` Section 3.6).
- No approval workflow for transfer creation is wanted (M8 Design Decision
  7, explicit and reasoned, not merely absent).
- No P&L impact from a transfer in the ordinary ship/receive path (M8
  Design Decision 9, explicit).
- WAC/frozen-shipment-cost valuation for transfers (M8 Design Decision 4/9,
  explicit, re-verified unchanged).
- Cancellation is legal only from DRAFT (M8 Design Decision 7, explicit,
  re-verified unchanged).
- The `date.today()` compensating-entry pattern is this codebase's
  established convention for corrections (M23A Section 3.5) — directly
  informative for, but not a decision about, a future transfer-loss entry.

## 17. Unresolved business decisions

Per the brief's list of 18, evaluated against repository evidence:

1. Who can create a transfer? — **Implemented** (Manager/Inventory Clerk,
   either store), but the one-sided-authorization *design intent* is
   unresolved (F7).
2. Who can approve? — **N/A**, no approval concept exists by deliberate
   design (M8 Decision 7) — not unresolved, decided as "none."
3. Who can ship? — **Implemented and consistent**: source-store
   Manager/Inventory Clerk only.
4. Who can receive? — **Implemented and consistent**: destination-store
   Manager/Inventory Clerk only.
5. Who can cancel? — **Implemented and consistent**: either store,
   DRAFT-only.
6. Can a shipped transfer be cancelled? — **No, by deliberate design**
   (deferred, not unresolved as a yes/no — but *how* it should eventually
   work, if ever built, is fully unresolved).
7. Who declares goods lost? — **UNRESOLVED**, no mechanism exists.
8. Who records damage? — **UNRESOLVED**, no mechanism exists.
9. Who authorizes write-off? — **UNRESOLVED**, no mechanism, no permission
   tier reserved for it (unlike `accounting.admin`, which M22 found already
   reserved for period administration — no equivalent exists here).
10. Is partial receipt allowed? — **Yes, implemented, working.**
11. Is over-receipt allowed? — **No, implemented, rejected
    (`OVER_RECEIPT`).**
12. What happens to a discrepancy (shipped vs. received quantity mismatch
    the destination reports)? — **UNRESOLVED** beyond the existing
    `OVER_RECEIPT` guard; there is no "the destination says fewer arrived
    than were shipped and we need to record why" workflow — a permanently
    partial receipt is indistinguishable from "the rest is still coming."
13. Can goods be returned to source? — **UNRESOLVED**, no mechanism exists.
14. Is an in-transit timeout required? — **UNRESOLVED**, no repository
    evidence either way.
15. Does an unresolved transfer eventually require mandatory reconciliation?
    — **UNRESOLVED**, no repository evidence.
16. Which store owns the loss? — **UNRESOLVED.** No evidence either way;
    plausible candidates (source, destination, split) are all equally
    unsupported by anything in the repository.
17. Which GL account records the loss/write-off? — **UNRESOLVED**, though
    `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (already seeded, already used for
    ordinary stock-count shrinkage) is the most evidence-adjacent existing
    candidate — reusing an existing account rather than inventing one would
    match this codebase's own stated preference (`docs/M22_DISCOVERY.md`'s
    P&L fix reused existing accounts rather than adding new ones), but this
    is not a decision made here.
18. What accounting date applies to post-shipment corrective events? —
    **UNRESOLVED** as a formal decision, though Section 12 above identifies
    `date.today()` as the only evidence-consistent choice if one is ever
    needed.

## 18. Proposed implementation boundary

**A. Defects fixable without a business-policy decision:** none identified.
Every concrete fix for the HIGH/MEDIUM findings above (a loss/write-off
path, F7's authorization symmetry, a discrepancy-recording workflow) requires
at least one of: who may act, which account records it, what date it uses,
or whose consent is required — all of which are the unresolved items in
Section 17. There is no purely-mechanical bug to fix here the way M23B found
one in `PURCHASE_INVOICE_VOID` — that fix was possible without a policy
decision only because the *existing* codebase already had four separate
functions agreeing on the right pattern, and the void was purely
inconsistent with them. No analogous internal-consistency case exists for
transfer loss, because there is no existing transfer-loss code at all to be
inconsistent with.

**B. Changes requiring an explicit policy decision (candidate M25
implementation, only once M24A-equivalent policy resolution happens):**
- A `LOST`/`WRITTEN_OFF` terminal state (or a single combined one) for a
  SHIPPED transfer, and the permission tier that may set it.
- The GL entry that closes out `Inventory In Transit` for a written-off
  line (account, amount = frozen `unit_cost_at_shipment` × outstanding
  quantity, posting date).
- Resolution of Finding F7 (symmetric vs. asymmetric transfer-creation
  authorization).
- A discrepancy-recording mechanism for a receiving party who believes
  fewer goods arrived than were ever going to (distinct from ordinary
  partial receipt, which assumes the rest is still coming).

**C. Optional future enhancements (no policy decision blocks these, but
none is proposed for implementation now):**
- An "outstanding since" age column or filter on the existing
  `inventory_in_transit` drill-down report (pure reporting addition, no
  new state or accounting).
- A scheduled/alerting job surfacing lines outstanding beyond some
  configurable threshold (itself a policy question — what threshold — so
  listed here only as a *possible* enhancement, not proposed).

**D. Out of scope for M24/M25 entirely:**
- Returned-to-source workflows.
- Serialized/lot tracking for transfers (already out of scope per M8).
- Any change to the ordinary DRAFT→SHIPPED→RECEIVED path, which this
  discovery found fully correct.
- Fiscal-year, Equity, or Balance Sheet concerns (M23/M23A territory,
  unaffected by anything here).

## 19. Testing-session design (for a future M25 implementation — not run now)

Every session below states its exact invariant; none is implemented or
executed in this discovery.

| # | Session | Objective | Invariant | Setup | Operation | Expected result | Level | Failure mode prevented |
|---|---|---|---|---|---|---|---|---|
| A | Transfer creation | A DRAFT transfer is created correctly from either store's authorized caller | Creation never moves inventory or posts GL | Two active stores, a shared-SKU product in each | `create_transfer` from a source-store caller | `status="DRAFT"`, zero `InventoryMovement`/`JournalEntry` rows | Service | A future change accidentally gives creation a side effect |
| B | Shipment | Shipping freezes cost and posts both legs correctly | `unit_cost_at_shipment` frozen; `TRANSFER_OUT` + shipment journal both correct | DRAFT transfer, source product with known `current_cost` | `ship_transfer` | Line's `unit_cost_at_shipment == current_cost` at ship time; journal `Dr In-Transit/Cr Inventory` balances | Service | Cost re-derived at the wrong time |
| C | Receipt | Receiving uses frozen cost, rolls destination WAC, posts the mirror journal | Frozen cost used, never destination's own WAC | SHIPPED transfer | `receive_transfer` | `TRANSFER_IN` at frozen cost; destination WAC updated via `compute_new_wac`; journal `Dr Inventory/Cr In-Transit` balances | Service | Circular WAC derivation |
| D | Cancellation before shipment | A DRAFT transfer cancels cleanly | No inventory/GL effect from cancellation | DRAFT transfer | `cancel_transfer` | `status="CANCELLED"`, zero movements/journals | Service | Cancellation gains an unintended side effect |
| E | Post-shipment cancellation (only if policy B decides to permit it) | Whatever policy is decided is implemented exactly | Depends on the resolved policy — **do not implement this session until Section 17 item 6/16/17/18 are resolved** | SHIPPED transfer | attempted cancellation | Per the resolved policy only | Service | Implementing a guessed policy |
| F | Loss/write-off (only if policy B decides to build it) | Whatever policy is decided is implemented exactly | **Do not implement until Section 17 items 7/9/16/17/18 are resolved** | SHIPPED transfer, partially or fully unreceived | the new operation | Per the resolved policy only; `Inventory In Transit` correctly zeroed for the written-off quantity; no `InventoryMovement` created (Section 4) | Service | Inventing a policy under test-writing pressure |
| G | Partial receipt | Multiple receipt events correctly sum | `Σ received_quantity` across events never exceeds `shipped_quantity`; each event's own journal is independently correct | SHIPPED transfer, 2+ receipt calls | sequential `receive_transfer` calls | Running totals correct after each; reconciliation shows the correct still-outstanding remainder | Service | Double-counting across receipt events |
| H | Over-receipt | Receiving more than shipped is rejected | `received_quantity <= shipped_quantity` always | SHIPPED transfer | `receive_transfer` with excess quantity | `OVER_RECEIPT`, zero mutation | Service | Silent over-recognition of destination inventory |
| I | Duplicate receipt | Retrying the same receipt is a no-op | Idempotency by `client_transaction_id` | SHIPPED transfer | same receipt request sent twice | Exactly one `InterStoreTransferReceipt`/journal; second call returns the first's result | Service | Double-posting on client retry |
| J | Duplicate shipment | Retrying the same shipment is a no-op | Idempotency by `ship_client_transaction_id` | DRAFT transfer | same ship request sent twice | Exactly one `TRANSFER_OUT` set/journal; second call returns the first's result | Service | Double-posting on client retry |
| K | Concurrent shipment | Two simultaneous ship attempts on the same transfer never both win | Header lock + post-lock re-check | DRAFT transfer, two threads | simultaneous `ship_transfer` | Exactly one succeeds fully; the other sees `SHIPPED`/`INVALID_TRANSFER_STATE` or the idempotent winner, per the exact `client_transaction_id`s used | Concurrency | Lost update / double shipment |
| L | Concurrent receipt | Two simultaneous receive attempts never jointly over-receive | Header lock + per-line remaining-quantity check under lock | SHIPPED transfer, two threads requesting overlapping quantities | simultaneous `receive_transfer` | Combined received quantity never exceeds shipped; loser gets `OVER_RECEIPT` or the idempotent winner | Concurrency | Lost update / over-receipt race |
| M | Shipment vs. cancellation/loss | A transfer cannot be both shipped and cancelled/written-off | Header lock makes the two mutually exclusive | DRAFT transfer, two threads: one ships, one cancels | simultaneous `ship_transfer` / `cancel_transfer` | Exactly one succeeds; the other sees the post-transition state and is correctly rejected | Concurrency | A transfer ending up in an inconsistent combined state |
| N | Store isolation | No cross-store manipulation of a transfer whose source/destination both exclude the caller's store | `caller_store_id in (from, to)` enforced on every mutating path | Three stores, transfer between two of them | Third store's user attempts create/ship/receive/cancel | `ForbiddenError`/`STORE_ACCESS_DENIED` on every attempt; reads 404 | Service + adversarial | Cross-store data manipulation |
| O | Accounting/GL reconciliation | Every posted transfer leg balances and reconciles | `Σdebit == Σcredit`; GL matches subledger | Several transfers at various stages | run `trial_balance`, `inventory_in_transit_reconciliation` | Zero discrepancy, all entries balanced | Service | Unbalanced or divergent postings |
| P | Inventory In Transit reconciliation | The company-wide reconciliation is correct under multiple concurrent transfers | `gl_in_transit_balance == outstanding_in_transit_total` when nothing is stuck | Multiple transfers at DRAFT/SHIPPED/partially-received/fully-received | run the reconciliation | Discrepancy is exactly zero except for genuinely outstanding lines, which match exactly | Service | Silent GL/subledger drift |
| Q | WAC/cost integrity | Transfer cost never leaks the wrong value into either store's WAC | Source's cost unaffected by transfer; destination's WAC correctly blended | Source product with a WAC that changes AFTER shipment, before receipt | ship, then change source WAC via an unrelated purchase, then receive | Destination WAC uses the frozen `unit_cost_at_shipment`, not the source's new WAC | Service | Cost race / wrong-cost contamination |
| R | Mutation testing | Every invariant above is actually enforced by the code, not merely by the absence of a bad test | See individual mutation targets (Section 20) | Baseline passing suite | apply one mutation at a time, run the relevant session, revert, diff-verify | Each mutation produces a RED result in the session designed to catch it | Mutation | A passing suite that doesn't actually test the invariant it claims to |

21 sessions (A-R plus the two explicitly-conditional E/F), matching or
exceeding the requested minimum, with E and F deliberately left as
placeholders that must not be filled in until their blocking policy
questions (Section 17) are resolved.

### Mutation targets for Session R (defined only — none executed in this discovery)

Concrete mutations a future implementation's test suite must catch, mapped
to the session that must catch each one:

| Mutation | Target function | Session that must catch it |
|---|---|---|
| Remove the transfer state guard (`if transfer.status != "DRAFT"` / `!= "SHIPPED"`) | `ship_transfer`, `receive_transfer`, `cancel_transfer` | A, B, C, D, M |
| Bypass source-store authorization | `ship_transfer`'s `_enforce_store_access` call | N |
| Bypass destination-store authorization | `receive_transfer`'s `_enforce_store_access` call | N |
| Remove the transfer-header row lock (`with_for_update()`) | `ship_transfer`, `receive_transfer`, `cancel_transfer` | K, L, M |
| Duplicate the inventory movement (call `record_movement` twice per line) | `ship_transfer`, `receive_transfer` | G, O, P |
| Omit the `Inventory In Transit` posting | `post_transfer_shipment_journal`, `post_transfer_receipt_journal` | O, P |
| Reverse the `Inventory In Transit` account (swap debit/credit) | `post_transfer_shipment_journal`, `post_transfer_receipt_journal` | O, P |
| Change the transfer cost (use current cost instead of frozen `unit_cost_at_shipment`) | `receive_transfer` | Q |
| Allow duplicate receipt (remove idempotency check) | `receive_transfer` | I |
| Allow receipt after a terminal loss/write-off (once F exists) | `receive_transfer` | F, once built |
| Allow cancellation after an irreversible terminal state | `cancel_transfer`'s `_CANCELLABLE_TRANSFER_STATUSES` check | D, M |
| Bypass accounting-period enforcement | `_enforce_period_open` (shared with every other posting, M22) | O |
| Remove idempotency protection (ship or receive) | `ship_transfer`, `receive_transfer` | I, J, K, L |

Each mutation is to be applied one at a time to a copy of the real source,
proven to make the correct session fail RED for the stated reason, then
reverted with a byte-identical diff check — the exact methodology already
used in M21/M22/M23B. **None of this is executed now**; discovery defines
the targets only.

## 20. Explicit stop conditions

Per the brief, M25 implementation must stop and obtain an explicit policy
decision before proceeding on any of the following — all currently
unresolved per Section 17:

- Ownership of transfer loss (which store bears it).
- Authorization to create/approve transfers (Finding F7 — symmetric
  consent vs. current one-sided implementation).
- Partial-receipt discrepancy policy (distinguishing "still coming" from
  "the rest is short and here's why").
- Over-receipt policy — **not a stop condition**, already resolved
  (rejected, `OVER_RECEIPT`).
- Post-shipment cancellation semantics.
- Write-off authority (who, and via which permission).
- Accounting treatment of transfer loss (which account, whether an expense
  is recognized).
- Accounting date for corrective transfer events (evidence points to
  `date.today()`, per Section 12, but this is not itself a ratified
  decision).
- Destination/source store responsibilities more generally (in-transit
  timeout, mandatory reconciliation) — no evidence exists to answer these
  at all.

**This document does not resolve any of the above.** A policy-resolution
phase (mirroring M23A's structure) is the recommended next step before any
M25 implementation, exactly as the brief's own "Implementation Boundary"
section anticipates. M24 does not propose that phase automatically — it is
named here only as the logical continuation this discovery's own findings
point to.
