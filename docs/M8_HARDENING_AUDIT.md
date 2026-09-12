# M8 — Hardening Audit

Companion to `docs/M8_ADVANCED_INVENTORY_DESIGN.md` (Phase 0 tracing and
design decisions). This document is the test inventory, the concurrency/
failure-injection/mutation evidence, the migration verification (including
against the real populated `erp_dev` database), the performance review,
the final auditor self-review, and the verdict.

## 1. Test inventory (by session)

| Session | File(s) | What it proved |
|---|---|---|
| A — migration | `alembic/versions/b7e3f1a29c5d_*.py`, `test_migrations.py` | Full up/down/up cycle from scratch (44 tables at M8 head, was 38 at M7); the M8 downgrade guard fires correctly against real M8-only data (new `test_m8_downgrade_refuses_when_transfer_movement_data_exists`); the M7 downgrade test was fixed to target `M7_HEAD_REVISION` explicitly rather than `"head"` (Section 5 explains why). |
| B — stock count lifecycle | `test_stock_counts.py` (19 tests) | DRAFT→OPEN→COUNTED→REVIEWED→POSTED and CANCELLED from every pre-POSTED status; category+explicit-product scoping; expected-quantity snapshotting exactly at OPEN, never retroactively updated; first-time count vs. recount re-snapshotting; zero/shrinkage/surplus posting with correct GL lines; idempotent-by-state open/post; store-isolation rejection. |
| C — stock count drift detection | `test_stock_counts.py::test_post_refuses_and_names_drifted_lines_without_posting_anything` | The mandatory policy (design doc "Design Decision 5"): a movement against a counted product after the snapshot causes the ENTIRE posting to be refused, naming the drifted line, with zero partial application — even for the other, non-drifted line in the same count. |
| D — stock count concurrency | `test_stock_counts_concurrency.py` (4 tests, real PostgreSQL, independent connections) | See Section 2. |
| E — stock count API/RBAC | `test_stock_counts_api.py` (4 tests) | Inventory Clerk can write but not review/post; Manager can review and post; a store-scoped user gets 404 on another store's count via GET; a mutation test proves the service-layer `caller_store_id` check on `open_stock_count` is independent of any route-layer call (there is none for this route). |
| F — transfer lifecycle | `test_transfers.py` (13 tests) | Destination product resolved by SKU match, rejected (`DESTINATION_PRODUCT_NOT_FOUND`) when none exists — never auto-created; same-store transfers rejected; over-shipment/over-receipt rejected; ship/receive idempotency by `client_transaction_id`; partial shipment with multiple partial receipts fully reconciling; cancellation only from DRAFT; in-transit reconciliation. |
| G — transfer cost determinism | `test_transfers.py::test_receive_uses_frozen_shipment_cost_not_destination_current_wac` | The central M8 transfer requirement: `unit_cost_at_shipment` is frozen at ship time and used unchanged at every receive event, even when both the source's and the destination's own current costs drift wildly afterward. |
| H — transfer concurrency | `test_transfers_concurrency.py` (5 tests, real PostgreSQL, independent connections) | See Section 2. |
| I — transfer API/RBAC | `test_transfers_api.py` (4 tests) | Cashier cannot create a transfer; Inventory Clerk can create/ship/receive; a source-store-scoped user cannot receive at the destination store; an unrelated store gets 404 on GET. |
| J — replenishment | `test_replenishment.py` (6 tests) | `inventory_position` correctly nets in-transit (shipped-not-received) and open-PO (ordered-not-received) quantities so a shortfall already covered in transit is never double-suggested; the transfer-vs-purchase split only ever suggests a transfer up to a sister store's own surplus above ITS OWN reorder point; the report never creates a `PurchaseOrder` or `InterStoreTransfer` as a side effect, proven by calling it twice and diffing row counts. |
| K — failure injection | `test_m8_failure_injection.py` (4 tests) | Forced failures during stock-count-variance accounting posting, transfer-shipment accounting posting, transfer-receipt accounting posting, and stock-count audit logging — all four leave zero residue (status unchanged, quantities unchanged, zero adjustments/movements/receipts created). |
| L — mutation testing | Manual, documented in Section 4 | Three targeted source mutations, each proven caught by the existing automated suite, then reverted. |
| M — frontend | `StockCountsPage.tsx`, `TransfersPage.tsx`, `InventoryPage.tsx` (extended) | See Section 6. |
| N — existing-suite regression | Full `tests/` run | All 350 pre-M8 tests still pass unchanged after every M8 schema/model change — two were legitimately UPDATED, not broken: `test_manager_can_read_accounts_and_journals`'s expected account count (18→19, the new Inventory In Transit account) and `test_unknown_movement_type_rejected`'s probe value (`TRANSFER_IN` is no longer an invalid movement type — replaced with a value that is still genuinely invalid). |

Total: **409/409** backend tests pass (350 pre-existing + 59 new M8 tests
across the files above). **30/30** frontend tests pass (unchanged — no
existing frontend test needed updating).

## 2. Concurrency results (real PostgreSQL, independent connections)

### Stock counts (M8 Section 2, mandatory) — `test_stock_counts_concurrency.py`

- **A — a movement during the OPEN window, then post.** A stock adjustment
  posts against a counted product after review but before posting; the
  post is refused with `STOCK_COUNT_DRIFT_DETECTED`, never silently
  applied on top of the untracked change. Run once (deterministic — no
  race between two callers, just sequencing of a real committed movement
  against a later post attempt).
- **B — two concurrent posts of the same REVIEWED count.** The header
  lock serializes them: exactly one round of adjustments is created
  (never duplicated), and the "loser" observes the already-POSTED result
  cleanly (idempotent-by-state), never an error.
- **C — two counters submit the SAME product on the SAME count
  concurrently.** The deliberately coarser header lock (design doc
  "Design Decision 5") serializes them: both writes apply in sequence
  (`recount_number` reaches 2), and the final `counted_quantity` is
  whichever write acquired the lock second — never a corrupted or
  half-written value.
- **D — posting races an adjustment on a DIFFERENT, uncounted product in
  the same store, run 5 times.** Since the two operations touch disjoint
  products, both must always succeed independently, proving the
  per-product lock granularity does not over-serialize unrelated
  inventory activity during a count's REVIEWED window.

All 4 tests pass; re-run 3 additional times back-to-back with no
flakiness observed.

### Inter-store transfers (M8 Section 5, mandatory) — `test_transfers_concurrency.py`

- **A — two concurrent shipments of the same DRAFT transfer, run 5
  times.** Exactly one ships; the other sees `INVALID_TRANSFER_STATE`
  (already SHIPPED); exactly one `TRANSFER_OUT` movement results,
  verified by direct ledger query each iteration.
- **B — two overlapping receive requests against the same shipped line,
  run 5 times**, each individually valid (30 of 50 shipped) but together
  exceeding what was shipped (60 > 50). Exactly one succeeds; the other
  gets `OVER_RECEIPT`; `received_quantity` never exceeds `shipped_quantity`.
- **C — shipment races an unrelated stock adjustment on the SAME source
  product.** Both go through `lock_product_for_update`, so the final
  on-hand quantity reflects both changes exactly regardless of
  interleaving order.
- **D — concurrent duplicate ship requests, same `client_transaction_id`.**
  Exactly one `TRANSFER_OUT` movement results; the source's on-hand
  quantity reflects only one shipment's worth.
- **E — concurrent duplicate receive requests, same `client_transaction_id`.**
  Exactly one `InterStoreTransferReceipt` row results.

All 5 tests pass (A and B internally loop 5 times each — 10 additional
trials beyond the base 5 test invocations); re-run 3 additional times
back-to-back with no flakiness observed.

No global inventory lock was used anywhere in either test file — every
scenario above is proven correct using only the header-row lock (stock
count / transfer) and per-product row locks (`lock_product_for_update`),
consistent with the design doc's explicit prohibition on a global lock.

## 3. Mutation testing

Following the M6/M7 precedent of a mix of permanent automated tests
(Sections 1-2 above already cover most of the load-bearing protections)
plus a small number of manual, documented, reverted source mutations for
protections that are inline expressions rather than separately-patchable
functions:

1. **Stock-count drift detection.** `post_stock_count`'s
   `if product.current_qty_on_hand != line.expected_quantity:` comparison
   was replaced with `if False:` (disabling drift detection entirely).
   Result: `test_post_refuses_and_names_drifted_lines_without_posting_anything`
   and `test_a_sale_during_open_window_causes_drift_detection_not_silent_loss`
   both failed immediately (the count posted successfully instead of being
   refused) — confirming the tests actually exercise this exact
   comparison, not a coincidentally-passing side effect. Reverted;
   verified green again.
2. **Transfer cost determinism.** `receive_transfer`'s
   `unit_cost = db_line.unit_cost_at_shipment` was replaced with
   `unit_cost = product.current_cost` (using the destination's current
   WAC instead of the frozen shipment cost — precisely the mistake the
   design doc's "Design Decision 9" explicitly forbids). Result:
   `test_receive_uses_frozen_shipment_cost_not_destination_current_wac`
   failed (computed WAC was `2.000000` instead of the expected
   `6.000000`). Reverted; verified green again.
3. **Over-shipment bound.** `ship_transfer`'s
   `if input_line.quantity_to_ship > db_line.requested_quantity:` guard
   was replaced with `if False:`. Result:
   `test_ship_rejects_over_shipment` still failed — but not with the
   expected `ConflictError(OVER_SHIPMENT)`. Instead it failed with a raw
   `IntegrityError` from the database's own
   `ck_inter_store_transfer_lines_shipped_bounds` CHECK constraint
   (`shipped_quantity <= requested_quantity`), which fired independently
   the instant the application tried to persist the over-shipped value.
   **This is a genuine defense-in-depth finding, not a weaker result**:
   the application-level check exists to fail cleanly with a named error
   code before touching the database; the DB CHECK constraint is the
   actual last-resort backstop if it were ever bypassed — exactly the
   same two-layer pattern M7's mutation testing found for AP's
   `OVERPAYMENT` bound (M7 audit Section 4, target 3). Reverted; verified
   green again.

All three mutations were applied with a single `Edit` tool call, verified
against the specific test(s) named above, then reverted with a second
`Edit` call restoring the exact original text (confirmed via
`grep -n "MUTATION TEST"` returning no matches afterward), followed by a
full `pytest`/`ruff`/`mypy` re-run confirming a clean baseline.

## 4. A process note: a git-checkout recovery during this audit

While reverting an earlier ad hoc mutation, `git checkout --
app/modules/inventory/service.py` was run to undo a one-line edit — this
command discards ALL uncommitted changes to a file, not just the last
edit, and since no M8 commit existed yet at that point, it silently
reverted the entire M8 stock-count implementation in that file back to
its M7 state (roughly 620 lines lost: the whole "Stock counts / physical
inventory" section, the `stock_count_id` parameter on
`create_stock_adjustment`, and the `_enforce_store_access` helper).

This was caught immediately by the very next verification step (running
the stock-count test files, which failed with `AttributeError`/import
errors), root-caused via `git status`/`git diff --stat` showing the file
had reverted to a tracked-HEAD state, and fully reconstructed from this
conversation's own record of every edit made to that file — re-verified
line-for-line against `ruff`/`mypy`/the full test suite (409/409 passed
identically to before the incident) before continuing. No data or design
decision was actually lost; the incident cost time, not correctness. Every
subsequent mutation-test revert in Section 3 used a targeted `Edit`
tool call (exact old-string → new-string) instead of any `git` command,
specifically to avoid a repeat.

Disclosed here rather than omitted, per the same principle M7's audit
applied to its own two real defects: an audit's job is to make problems
visible, not to hide the process that found and fixed them.

## 5. Migration results

- Fresh-database up/down/up cycle: 38 tables at M7 head → 44 at M8 head (6
  new tables: `stock_counts`, `stock_count_lines`, `inter_store_transfers`,
  `inter_store_transfer_lines`, `inter_store_transfer_receipts`,
  `inter_store_transfer_receipt_items`); 30 permissions (was 24; six new:
  `inventory.count.write/review/post`, `inventory.transfer.write/ship/receive`).
- **Against the real, populated `erp_dev` database** (2,711 products,
  4,838 inventory movements, 18 accounts, 24 permissions from real
  M1-M7-era usage/testing): upgrade succeeded cleanly; the new `1520`
  Inventory In Transit account, all six tables, and all six permissions
  (correctly granted to Admin/Manager fully and to Inventory Clerk for
  `count.write`/`transfer.write`/`transfer.ship`/`transfer.receive` only,
  never `count.review`/`count.post`) were verified present via direct SQL
  after upgrade.
- **All four downgrade guards, tested against real data inserted via raw
  SQL, not just asserted to exist** — each tested independently by
  inserting the exact real-world row shape that would violate the
  narrower M7 schema, confirming `alembic downgrade` refuses loudly
  BEFORE any destructive DDL runs (Postgres transactional DDL rolls the
  whole failed migration back — table count and current revision
  unchanged each time), then cleaning up and confirming a subsequent
  downgrade with no such data succeeds cleanly:
  1. A real `TRANSFER_OUT` `inventory_movements` row → guard fires
     (this one can never be worked around once real transfer activity
     exists — the ledger is append-only).
  2. A `stock_adjustments` row with `stock_count_id` set → guard fires.
  3. A `journal_lines` row referencing account `1520` (required a
     genuinely balanced entry to get past the database's own
     `check_journal_entry_balance()` trigger first — confirming that
     trigger is itself a real, independent safeguard, not merely
     decorative) → guard fires.
  4. A `journal_entries` row with `source_type` = `INTER_STORE_TRANSFER_SHIP`
     → guard fires, independently of guard 3 (verified using non-1520
     accounts so only this one guard could be the cause).
- A permanent regression test,
  `test_m8_downgrade_refuses_when_transfer_movement_data_exists`, was
  added to `test_migrations.py` mirroring guard 1 above (the ledger's
  append-only guard — the one real inter-store-transfer activity can
  never route around), following the exact
  `test_m7_downgrade_refuses_when_credit_note_data_exists` precedent.
- **A pre-existing test needed fixing, not just extending**: with M8
  installed, `"head"` no longer means M7. `test_m7_downgrade_refuses_when_credit_note_data_exists`
  used to upgrade to `"head"` (then M7) and downgrade one step to M6 in a
  single-revision transaction; with M8 present, upgrading to `"head"` now
  lands on M8, and downgrading to M6 spans two revisions (M8→M7→M6) in
  ONE `command.downgrade()` call. Alembic batches multiple revisions
  in a single call into one transaction by default, so the (correctly
  firing) M7 guard's exception rolled back BOTH the M7 step's failure
  AND the already-successful M8 step ahead of it — leaving the database
  at the M8 head, not the M7 head the test expected, and failing an
  assertion that had nothing to do with the guard itself. **Fixed** by
  upgrading to `M7_HEAD_REVISION` explicitly (not `"head"`) so the
  test's own downgrade call stays a single revision step, scoped
  correctly to testing M7's guard in isolation, unaffected by whatever
  milestone becomes head next. This is a real, if narrow, finding about
  how `test_migrations.py`'s existing per-milestone guard tests must be
  written going forward: always target a named revision, never `"head"`,
  when the test's own logic depends on the number of steps a
  `command.upgrade`/`command.downgrade` call spans.

## 6. Frontend

Three new files, one extended:

- `src/api/stockCounts.ts`, `src/api/transfers.ts`,
  `src/api/replenishment.ts` — typed clients for every new endpoint.
- `src/pages/StockCountsPage.tsx` — create (store + category and/or
  explicit product list), a master list with live status, and a detail
  panel with per-line expected/counted/variance/recount-count and every
  lifecycle action (open, count/recount, mark-counted, review, reopen,
  post, cancel), each button gated by the actual permission tier
  (`inventory.count.write`/`review`/`post`) via `useAuth().hasPermission`,
  not merely hidden by convention.
- `src/pages/TransfersPage.tsx` — create (from/to store, source product,
  quantity — destination resolved server-side by SKU), a master list, and
  a detail panel with per-line requested/shipped/received quantities plus
  ship (DRAFT) and receive (SHIPPED) actions, each gated by
  `inventory.transfer.write`/`ship`/`receive`.
- `src/pages/InventoryPage.tsx` extended with a read-only "Replenishment
  suggestions" section (inventory position, reorder point, and the
  transfer/purchase split, with the sister store named when a transfer is
  suggested) — explicitly captioned as decision support, never itself
  creating a purchase order or transfer.
- `src/components/layout/navigation.ts`/`src/App.tsx` — two new
  permission-gated routes (`/stock-counts`, `/transfers`), same
  `inventory.read` gate as the existing Inventory nav item (finer-grained
  write/review/post/ship/receive permissions are enforced inside each
  page and by the API itself, not by hiding the nav link).

All 30 pre-existing frontend tests pass unchanged (no new `.test.tsx`
files were added for the two new pages, consistent with this session's
effort allocation toward the backend's concurrency/failure-injection/
migration discipline the task explicitly marked mandatory; this is
recorded as a known gap in Section 7, not silently omitted).
`tsc -b`, `oxlint`, and `vite build` all clean.

**Live smoke test** (Playwright, real backend + Postgres + Vite dev
server, not mocked, using a real Admin user created and then fully
cleaned up afterward — including the FK-dependent `audit_logs`/
`refresh_tokens`/`user_roles` rows a naive single-table `DELETE` misses):
logged in, navigated to Stock Counts (master list of real accumulated
dev-database counts rendered correctly; opened a REVIEWED count's detail
panel showing its line, expected/counted/variance, and the
Reopen/Post/Cancel actions), navigated to Transfers (master list of real
SHIPPED transfers; opened a detail panel showing requested/shipped/received
quantities and a live Receive action), and confirmed the Inventory page's
new Replenishment suggestions section renders correctly (with real data,
"Nothing below its reorder point right now" when nothing qualified).
Exercised a real write path end-to-end through the UI: submitted a count
entry on an OPEN count's line via the "Count"/"Recount" button, confirmed
the line's `recount_number` incremented and the row re-rendered with the
new variance — proving the full request→response→re-render cycle works,
not just that the page loads.

## 7. Performance review

- `post_stock_count` issues one `SELECT ... FOR UPDATE` per distinct
  product referenced by a counted line (not per line — a product counted
  and recounted multiple times is still locked once), plus one
  `create_stock_adjustment` call per nonzero-variance line, each of which
  is itself O(1) (a single product row already locked, one movement
  insert, one journal-entry insert). No N+1 across an arbitrarily large
  count's line list beyond that.
- `ship_transfer`/`receive_transfer` mirror `receive_goods`'s shape
  exactly: one lock per distinct product touched, not per line — a
  transfer with multiple lines against the same product (not possible by
  construction here, since a transfer line is keyed by product, but
  relevant if that constraint is ever relaxed) would still lock once.
- `get_replenishment_suggestions` issues exactly 3 queries independent of
  product count (all active products with a reorder point; all
  SHIPPED-but-not-fully-received transfer lines; all open PO items) plus,
  for each product actually below its reorder point, one additional query
  to find sister-store products by SKU. This is O(shortfall count), not
  O(product count) — acceptable at current data volumes (verified against
  2,711 real products in `erp_dev` with sub-second response) but would
  want batching if the number of simultaneously-short products grows into
  the thousands. Not addressed in M8 — no evidence of a real volume
  problem at this scale, consistent with M7's own stated policy of not
  optimizing without one.
- `inventory_in_transit_reconciliation` computes the outstanding total in
  a single Python loop over not-fully-received transfer lines (typically
  a small, operationally-bounded set — a store does not have thousands of
  transfers perpetually in transit) rather than a SQL aggregate; flagged
  as a candidate for a SQL-side rewrite if that assumption ever stops
  holding, mirroring M7's own documented debt for its aging/clearing
  reports.
- Every new table's FK columns have indexes from the migration itself
  (`ix_stock_counts_store_id`/`status`, `ix_stock_count_lines_stock_count_id`/
  `product_id`, `ix_inter_store_transfers_from_store_id`/`to_store_id`/
  `status`, `ix_inter_store_transfer_lines_*`,
  `ix_inter_store_transfer_receipts_transfer_id`,
  `ix_inter_store_transfer_receipt_items_*`) — every new query path
  filters or joins on one of these.
- The one denormalized cache this milestone touches
  (`products.current_qty_on_hand`/`current_cost`) is the same pre-existing
  M1 cache every other milestone already relies on and reconciles against
  the ledger (`get_quantity_on_hand_from_ledger`); M8 introduces no new
  cache without an accompanying reconciliation check
  (`inventory_in_transit_reconciliation` for the one new balance-sheet
  position M8 adds).

## 8. Final auditor self-review

Can the system:

1. **Create inventory merely because a stock count or transfer document
   exists?** No — a DRAFT stock count and a DRAFT transfer both have zero
   inventory/accounting effect; a count's variance posts only at POST
   (REVIEWED→POSTED), and a transfer's inventory moves only at SHIP
   (source, Dr Inventory In Transit / Cr Inventory) and RECEIVE
   (destination, Dr Inventory / Cr Inventory In Transit) — never at
   creation, matching the task's explicit governing principle verbatim.
2. **Post a stock count's variance twice?** No — `post_stock_count` is
   idempotent by state (an already-POSTED count returns unchanged,
   verified under real concurrency in Session D race B: exactly one round
   of adjustments across two simultaneous post attempts).
3. **Silently overwrite an inventory movement that happened during a
   stock count's OPEN window?** No — the mandatory drift-detection policy
   (Section 1 Session C, Section 2 race A) refuses the ENTIRE posting the
   instant any counted line's book quantity no longer matches its
   snapshot, naming every drifted line, requiring an explicit reopen and
   recount — never a partial or best-effort application.
4. **Count the same product twice on the same count without it being an
   explicit, auditable recount?** No — `record_count_entry` treats a
   second count of the same line as a RECOUNT by construction
   (`recount_number` increments, `expected_quantity` re-snapshots,
   before/after audit-logged as `STOCK_COUNT_LINE_RECOUNTED` distinctly
   from `STOCK_COUNT_LINE_COUNTED`) — there is no separate, unaudited
   "just overwrite it" path.
5. **Ship a transfer twice?** No — idempotent by `client_transaction_id`
   (pre- and post-lock check, `IntegrityError` recovery) plus a hard
   `INVALID_TRANSFER_STATE` rejection of any second ship attempt with a
   different key (Section 2 race A, Section 4 mutation-tested at the
   application layer with the DB CHECK constraint as an independent
   backstop for the related over-shipment bound).
6. **Receive more than was shipped?** No — `OVER_RECEIPT`, enforced both
   per-request and, under concurrency, correctly against the reduced
   remaining quantity a second concurrent receiver actually sees after
   the first commits (Section 2 race B).
7. **Recompute a transfer's historical cost from the destination's
   current WAC instead of the frozen shipment cost?** No — proven both by
   a dedicated domain test that deliberately drifts the source's cost
   between ship and receive (Section 1 Session G) and by a mutation test
   that deliberately introduces exactly this bug and confirms it is
   caught (Section 3, target 2).
8. **Create a transfer to or from an inactive or nonexistent store?** No
   — both stores validated `is_active`; a transfer between the same store
   twice is rejected as `SAME_STORE_TRANSFER`.
9. **Transfer a SKU that doesn't exist at the destination?** No — the
   destination product must already exist (matched by SKU or an explicit
   override); `DESTINATION_PRODUCT_NOT_FOUND` otherwise. The system never
   auto-creates a destination catalog entry, extending the "never create
   inventory merely because a document exists" principle to the catalog
   itself.
10. **Cross store boundaries?** No — every mutating stock-count and
    transfer function validates `caller_store_id` against the actual
    store(s) involved (a transfer checks both sides); GET-by-id routes
    return 404 rather than a store-confirming 403, matching the
    established AP/purchasing precedent (Section 1 Sessions E/I; a
    mutation test on the one route with no separate route-layer check —
    `open_stock_count` — proves the service-layer check is real and
    independent, not merely decorative).
11. **Bypass RBAC?** No — three permission tiers for stock counts
    (write/review/post) and three for transfers (write/ship/receive),
    each independently enforced; Inventory Clerk deliberately excluded
    from `count.review`/`count.post` (a financial-commitment separation
    of duties, mirroring AP's write/post/pay split) but granted all three
    transfer permissions (an operational task, not a financial-approval
    gate) — verified in Sessions E/I.
12. **Leave partial state after a failure mid-posting?** No — 4
    failure-injection tests covering stock-count-variance accounting,
    transfer-shipment accounting, transfer-receipt accounting, and
    stock-count audit logging, all proving zero residue (Section 1
    Session K).
13. **Let replenishment silently place an order or move stock?** No —
    `get_replenishment_suggestions` is proven, by calling it twice and
    diffing row counts, to never create a `PurchaseOrder` or
    `InterStoreTransfer` as a side effect (Section 1 Session J).
14. **Suggest replenishing stock that is already inbound?** No —
    `inventory_position` (not raw on-hand) nets in both shipped-but-not-
    received transfer quantity and ordered-but-not-received PO quantity
    before computing shortfall (Section 1 Session J).
15. **Downgrade the schema after real M8 data exists, silently losing
    it?** No — all four downgrade guards tested against real data,
    proven to refuse loudly before any destructive DDL (Section 5).

No unresolved "yes" remains. One process incident (the accidental
`git checkout` reversion, Section 4) occurred and was fully disclosed,
root-caused, and recovered from with zero net effect on the delivered
code or its correctness — included here for the same reason M7's audit
disclosed its two real code defects: transparency about the process, not
just the outcome.

## 9. Known limitations / deferred functionality

Unchanged from `docs/M8_ADVANCED_INVENTORY_DESIGN.md`'s "Deferred/known
limitations" section: cancelling a SHIPPED transfer (inventory has already
left the source store; a safe reversal is a genuinely separate feature,
not attempted here as an unsafe partial fix); no REQUESTED/APPROVED
transfer states (justified as unneeded process weight in the design doc);
no automated PO/transfer creation from a replenishment suggestion (a
human always initiates the resulting document as a separate, deliberate
step); no "override and post anyway" escape hatch for stock-count drift,
not even for Admin (the mandatory reopen-and-recount path is the only
route to posting once drift is detected); no serialized/lot tracking.

New to this audit: no dedicated frontend `.test.tsx` files were added for
`StockCountsPage`/`TransfersPage` (Section 6) — the pages were verified
via a live Playwright smoke test instead, but that is not a substitute
for permanent, automated frontend regression coverage. A reasonable
follow-up, not a defect in what was delivered.

## 10. Final validation gates

- Backend: **409/409** tests pass (was 350 pre-M8; +59 across the new
  stock-count/transfer/replenishment domain, concurrency, API/RBAC,
  failure-injection, and migration tests).
- Frontend: **30/30** tests pass (unchanged from M7 — no existing test
  needed updating for M8).
- `ruff check .`: clean. `black --check .`: clean. `mypy app`: clean (70
  source files). `tsc -b`: clean. `oxlint`: clean. `vite build`: clean.
- Migration up/down/up: clean against both a fresh database and the real
  populated `erp_dev` database (2,711 products, 4,838 movements); all
  four new downgrade guards verified to actually fire against real data
  inserted via raw SQL, not just asserted to exist; one pre-existing
  migration test (M7's own guard test) was fixed to remain correct now
  that `"head"` means something different (Section 5).
- Real PostgreSQL concurrency tests: 9 dedicated test functions (4 stock
  count + 5 transfer), several internally looping 5 times per the task's
  explicit instruction, covering every mandatory race from both Section 2
  and Section 5 of the task — 9/9 pass, re-run 3 additional times each
  with no flakiness.
- Failure injection: 4/4 tests pass, zero residue in every case.
- Mutation testing: 3/3 targeted manual mutations each independently
  proven caught by the existing automated suite (one revealing a genuine
  defense-in-depth finding — Section 3, target 3), then reverted, full
  suite reverified green.
- Live API + live frontend smoke test: stock count and transfer master
  lists and detail panels, and the inventory page's new replenishment
  section, all exercised through the real browser against the real
  backend and a real, already-populated Postgres database; one real write
  path (a count entry) exercised end-to-end through the UI, not just page
  loads.

## Verdict: **PASS**

No unresolved inventory-integrity or accounting-integrity defect remains.
The three protections targeted by manual mutation testing (Section 3)
were each proven genuinely load-bearing, including one defense-in-depth
finding (the database's own CHECK constraint independently catching an
over-shipment the disabled application check missed). One process
incident occurred (Section 4) and was fully disclosed, root-caused, and
recovered from with zero net effect on delivered correctness. This
document records that process, not merely its outcome, per the same
transparency principle M6 and M7's own audits applied to the real defects
they found.
