# M19 Hardening Audit — Supply Chain & Vendor Operations Completion

Companion to `M19_DISCOVERY.md`, `M19_DESIGN.md`, and
`M19_TESTING_SESSIONS.md`. Classifies every finding from this milestone
into confirmed defects (fixed), missing capabilities (intentionally
deferred), intentional design (not a defect), deferred functionality,
production prerequisites, and false positives — per this milestone's
explicit instruction not to turn an absent feature into a "bug" merely
because it is common in other ERPs.

## 1. Confirmed defects — fixed this milestone

| # | Defect | Root cause | Fix | Regression coverage |
|---|---|---|---|---|
| 1 | Purchase-order creation had no idempotency protection | `PurchaseOrder` was the one mutating write in this domain with no `client_transaction_id` column/check, unlike `GoodsReceipt`/`PurchaseReturn`/`PurchaseInvoice`/`SupplierPayment`/`SupplierCreditNote` | Added `client_transaction_id` (unique, NOT NULL, backfilled migration `3a0d50ccc909`) + the same two-layer fast-path/`IntegrityError`-recovery idempotency pattern every other module uses | Session B, G, H |
| 2 | Purchase returns validated only against current on-hand stock, never against what the specific PO actually received | `create_purchase_return` had no query against `PurchaseOrderItem.quantity_received`/prior `PurchaseReturnItem` rows for that PO | Added a `received`/`already_returned` computation (locked `FOR UPDATE`, closing a concurrency gap as a side effect) with a new `RETURN_EXCEEDS_RECEIVED_QUANTITY` rejection | Session C, H |
| 3 | `Supplier.default_payment_terms_days` had no write path | Column existed since M6 and was already correctly consumed by AP's due-date calculation, but `SupplierCreate`/`SupplierUpdate`/`create_supplier`/`update_supplier` never exposed it | Added the field to both schemas (`ge=0` validated) and threaded it through both service functions | Session D |
| 4 | `create_purchase_invoice`'s idempotency recovery could reject a genuine concurrent duplicate retry with `DUPLICATE_SUPPLIER_INVOICE_NUMBER` instead of transparently returning the winner | The `IntegrityError` handler branched on which of two UNIQUE constraints fired before checking for a `client_transaction_id` match — under an identical-values race, Postgres deterministically reported the invoice-number constraint first | Reordered the recovery block to check for the idempotent match first, unconditionally (matching every other module) | Session E (this defect was FOUND by the new concurrency test required for coverage item "real concurrency test for purchase-invoice-creation dedup" — not pre-scoped in `M19_DESIGN.md`'s original 3 items, added as §5 once discovered) |

All 4 fixes are narrow and mechanical: an added idempotency column +
check (matching an existing, proven pattern 5 times over), one new
validation query before an existing lock, one schema/service field
addition, and a 6-line exception-handler reordering. None redesigned
stable financial logic; none introduced new business concepts.

## 2. Missing capabilities — intentionally deferred (not built)

- **PO approval workflow.** Discovery confirmed no approval gate exists
  between DRAFT and ORDERED. This is an intentional M3 scope decision,
  not a regression — inventing one now without a confirmed business
  requirement would be exactly the speculative functionality this
  milestone's instructions forbid.
- **Automatic `PurchaseReturn` → `SupplierCreditNote` linkage.** The
  manual two-step pattern (record the return, then separately record the
  credit note) matches how settlement already works elsewhere in AP
  (e.g. a payment is a separate step from an invoice). Not found
  documented as a defect anywhere in this codebase's history; not built.
- **Per-receipt-lot cost tracking for returns.** `PurchaseReturn` costs
  at the product's *current* WAC, not the original receiving lot's cost
  — an accepted limitation from M3, restated and left unchanged (see
  `M19_DESIGN.md` §2's non-goal). The schema (`PurchaseReturnItem` has no
  `goods_receipt_item_id`) doesn't support finer granularity, and adding
  it would be a schema-level feature addition well beyond this
  milestone's fix-scoped mandate.
- **Frontend void-invoice button.** `void_purchase_invoice` has real,
  tested backend behavior; the frontend simply has no button wired to
  it. Low-severity UI gap, not a functional defect — deferred rather
  than built speculatively.

## 3. Intentional design (confirmed correct, not a defect)

- **Three-way matching (PO vs. receipt vs. invoice) is enforced at
  posting time, not creation time.** A DRAFT invoice can be created
  before its lines are validated against received quantities; the real
  ceiling check happens in `post_purchase_invoice`. This matches how a
  real AP clerk works (draft first, review, then commit) and was
  confirmed by discovery, not something this milestone changed.
- **WAC is recomputed fresh from `(qty, cost)` pairs on every receipt,
  never incrementally adjusted**, and only `receive_goods` ever passes
  `new_product_cost` to `record_movement` — every other movement type
  (sale, sale return, stock adjustment, purchase return) moves quantity
  but deliberately never touches WAC. Verified by reading every call
  site; unchanged by this milestone.
- **Supplier master data is global/shared across stores**, not
  store-scoped — confirmed by discovery to be the existing, intentional
  design (a supplier is a single real-world vendor relationship a
  multi-store company negotiates once). Preserved as-is; M19's own
  instructions explicitly warned against forcing store-scoping onto
  shared reference data.
- **Multiple independent, redundant guards against negative inventory**,
  discovered while mutation-testing "bypass inventory mutation
  protection" (see §5 below): `create_purchase_return`'s own on-hand
  check, `record_movement`'s own guard (BR-7), AND a database-level
  CHECK constraint (`ck_products_stock_non_negative_unless_allowed`) all
  independently prevent the same failure mode. This is deliberate
  belt-and-suspenders defense-in-depth, consistent with this codebase's
  established philosophy elsewhere (e.g. the DB balance trigger backing
  up application-level journal-entry balancing) — not duplicated/dead
  code.
- **Cancelling a `PARTIALLY_RECEIVED` purchase order retains the
  inventory already received.** Confirmed and newly tested (Session K):
  cancellation stops future receiving against that PO; it does not
  reverse a receipt that already posted. Matches the append-only-ledger
  philosophy applied everywhere else in this codebase (a sale return
  reverses a sale via a NEW, linked transaction — it never edits the
  original).

## 4. Deferred functionality (out of scope, documented impact)

None identified beyond what is already listed under §2 — this
milestone's discovery did not surface any functionality that is
partially built and stalled; everything found was either complete,
correctly deferred by design, or one of the 4 confirmed defects fixed
above.

## 5. Mutation testing (8 required targets, all caught)

Each mutation was a live, manual source-code edit (verified against a
`cp` backup both before and after, with `diff` confirming a byte-for-byte
clean revert), run against the relevant test(s), confirmed red, then
reverted and confirmed green again — per this codebase's established
discipline for inline-expression protections that aren't cleanly
interceptable at a monkeypatchable function boundary.

| # | Target | Mutation | Caught by | Notes |
|---|---|---|---|---|
| 1 | Remove supplier authorization | `_create_purchase_order_inner`'s `INVALID_SUPPLIER` check neutered (`if False:`) | `test_purchase_order_cannot_be_created_for_an_inactive_supplier` | Caught on first attempt. |
| 2 | Remove store isolation | `_enforce_store_access`'s condition replaced with `if False:` | `test_store_scoped_user_cannot_create_po_for_another_store`, `test_cross_store_purchase_return_is_rejected` | Caught 2 tests; `test_store_scoped_user_cannot_receive_against_another_stores_po` was unaffected because `receive_goods` has its own inline store check rather than calling the shared helper — confirmed intentional, not a gap (both code paths are independently tested). |
| 3 | Disable duplicate-receipt protection | `receive_goods`'s fast-path `client_transaction_id` lookup neutered | `test_exact_duplicate_receipt_request_returns_the_same_receipt` | The fast-path alone is backed by the `IntegrityError`-recovery layer as a second line of defense (same pattern everywhere in this codebase); in THIS specific test scenario the retry instead hit `INVALID_PO_STATE` (409) because the first receipt fully received the PO, changing its status before the retry reached the recovery block — still a real, caught failure, just via a different code path than initially expected. |
| 4 | Change accounting sign | `post_goods_receipt_journal`'s Dr/Cr swapped (Inventory↔Purchase Clearing) | `test_comprehensive_reconciliation_scenario_store_a`, `test_ap_and_purchase_clearing_reconciliation_after_partial_invoice_and_payment`, `test_ap_reconciliation_holds_after_supplier_payment_reversal` | 3 reconciliation tests broke decisively (GL balance off by 2x the transaction value). |
| 5 | Bypass quantity validation | The new `RETURN_EXCEEDS_RECEIVED_QUANTITY` check (fix #2) neutered | `test_purchase_return_cannot_exceed_received_quantity`, `test_second_partial_return_cannot_push_total_past_received_quantity` | Both new M19 regression tests caught it immediately. |
| 6 | Bypass inventory mutation protection | `record_movement`'s own negative-stock guard (BR-7) neutered | New test `test_record_movement_itself_refuses_to_go_negative` (added during this phase — see below) | **Finding**: the purchasing-level `INSUFFICIENT_STOCK` pre-check in `create_purchase_return` did NOT catch this mutation (it has its own earlier check that still ran); neither did any existing sales/inventory/concurrency test, because every one of them exercises a CALLER's own pre-check, never `record_movement`'s own guard directly. A new, dedicated unit test was added to close this proof gap. The mutation was still caught even without that new test, via the database CHECK constraint `ck_products_stock_non_negative_unless_allowed` — but as a raw, unhandled `IntegrityError` rather than the intended, clean `ConflictError`, confirming a real (if minor) gap: without the app-level guard, a caller would see a 500-level internal error instead of a clean 409, even though no actual data corruption occurs. |
| 7 | Bypass idempotency | PO creation's fast-path lookup AND `IntegrityError`-recovery winner-lookup both neutered together | `test_exact_duplicate_po_creation_request_returns_the_same_purchase_order` | Disabling only the fast path did not break this test (the recovery layer caught it transparently, proving genuine defense-in-depth); disabling both layers together produced an unhandled `IntegrityError`, correctly failing the test. |
| 8 | Bypass WAC calculation / alter arithmetic | `compute_new_wac` changed from the weighted formula to a simple average of the two costs | `test_wac_matches_worked_example_100_at_10_plus_50_at_14`, `test_wac_after_third_purchase_at_a_different_cost`, `test_wac_resets_cleanly_after_stock_reaches_zero_and_reopens`, `test_wac_matches_worked_example_at_fractional_quantities` | 4 tests caught it decisively. |

**Net result of mutation testing**: all 8 required targets caught. One
new permanent regression test was added
(`test_inventory.py::test_record_movement_itself_refuses_to_go_negative`)
to close a proof gap target #6 surfaced — the underlying protection was
already correct (and, in fact, backed by a third layer at the database
level that no test had previously exercised either), so this is a
test-coverage addition, not a behavior change.

## 6. False positives

One agent-reported finding from the discovery phase was investigated and
found to be incorrect, corrected before it was ever written into
`M19_DISCOVERY.md` as fact: an Explore agent auditing the AP module
claimed `reverse_supplier_payment`/`reverse_supplier_credit_note` "do not
exist anywhere in this codebase." Independent verification (`grep`/`Read`
against `app/modules/ap/service.py` and `app/modules/ap/models.py`,
cross-checked against test files) confirmed both functions, their
dedicated `SupplierPaymentReversal`/`SupplierCreditNoteReversal` models,
and their test coverage all genuinely exist and were built/hardened
across M16/M17. The agent had likely read only the generic
`reverse_journal_entry`'s deliberate refusal to reverse
`AUTOMATED_SOURCE_TYPES` entries, without finding the dedicated
operational reversal functions that exist specifically to handle that
case. Documented in `M19_DISCOVERY.md` §7 at the time; restated here per
this document's own false-positives section.

## 7. Production prerequisites

None newly identified by this milestone. M18's completion report already
documented the standing production prerequisites (PITR/WAL archiving,
genuine off-box backup transport, external Alertmanager notification
channel, audit-log capacity planning, target-specific `pg_hba.conf`,
production resource-limit tuning) and explicitly scoped them out of M19
unless discovery proved an immediate dependency — discovery did not.

## 8. Repository audit

- Working tree clean after all mutation-testing edits were reverted
  (`git diff --stat` after the final revert showed only the intended,
  reviewed M19 changes — verified by `diff` against a pre-mutation
  backup after every single one of the 8 mutations, not only at the
  end).
- Single Alembic head (`3a0d50ccc909`), confirmed by
  `test_migrations.py`'s full upgrade/downgrade/re-upgrade cycle.
- No secrets, debug code, or hardcoded test IDs introduced.
- No frontend money arithmetic — `SupplyChainPage.tsx` and its new test
  file only ever display server-computed decimal strings.
- No accidental generated files — `/tmp/m19_mutation_backups/*` never
  entered the repository; it is a scratch location outside the working
  tree.
