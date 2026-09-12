# M7 — Hardening Audit

Companion to `docs/M7_ADVANCED_AP_SETTLEMENT.md` (architecture/design). This
document is the test inventory, the concurrency/failure-injection/mutation
evidence, the two real defects this milestone's own testing discipline
caught and fixed, the performance review, the 19-question final auditor
self-review, and the verdict.

## 1. Test inventory (by session)

| Session | File(s) | What it proved |
|---|---|---|
| A — schema/migrations | `alembic/versions/a4f2c8e91b6d_*.py`, `test_migrations.py` | Full up/down/up cycle from scratch (38 tables at M7 head, was 33 at M6); the M7 downgrade guard fires correctly against real M7-only data (new `test_m7_downgrade_refuses_when_credit_note_data_exists`) — proven against the guard, not just asserted to exist. |
| B — invoice relationship model | `test_ap_invoices.py` (unchanged, still 18/18 green) | `purchase_order_id` becoming optional/non-authoritative did not disturb a single existing M6 invoice test — proof the new model is a strict superset, not a rewrite of working behavior. |
| C — matching engine | `service.py::_fifo_match_slices`, `post_purchase_invoice`, `test_ap_invoices.py` | FIFO receipt-lot matching now persists per-slice rows (`PurchaseInvoiceReceiptMatch`) instead of only summing; existing hand-verified two-cost-lot test still passes unchanged. |
| D — multiple invoices/receipts | `test_ap_e2e_scenario.py` | Two invoices against one PO (10 units then 5 units, different receipts) both post correctly; `quantity_invoiced` reaches exactly 15/15; a third invoice attempting anything more is rejected (`OVER_INVOICING`). |
| E — invoice exceptions/variances | `test_ap_e2e_scenario.py`, `test_ap_mutation.py` | A deliberate 10.00 unfavorable variance (5 units invoiced at 22.00 against a 20.00 receipt) posts automatically to Purchase Price Variance; `OVER_INVOICING` remains a hard, visible, synchronous rejection (the chosen "exception" mechanism — see design doc Section 7 for why a stateful approval queue was not built). |
| F — multi-payment allocation | `test_ap_payments.py`, `test_ap_concurrency.py` (B), `test_ap_e2e_scenario.py` | One payment settling two invoices in a single journal; an invoice receiving two separate payments; `ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT` rejects any unapplied remainder. |
| G — supplier credits | `test_ap_e2e_scenario.py`, `test_ap_concurrency.py` (D), `test_ap_failure_injection.py`, `test_ap_mutation.py` | A COMMERCIAL_DISCOUNT credit note reduces AP and an invoice's outstanding balance atomically; `OVER_ALLOCATION` rejects over-crediting; idempotency proven with the same monkeypatch-mutation pattern as invoices/payments. |
| H — AP subledger | `test_ap_reconciliation.py`, `service.py::get_supplier_ap_summary` | `total_owed`/`total_paid`/`total_credited` all independently hand-derived and matched exactly across a partial-invoice-plus-partial-payment-plus-credit scenario. |
| I — AP aging | `test_ap_reconciliation.py`, `test_ap_e2e_scenario.py` | Bucketing uses `grand_total - amount_paid - amount_credited`; a credit note demonstrably moves an invoice out of the aging report entirely once fully settled. |
| J — supplier statements | `test_ap_reconciliation.py`, `test_ap_e2e_scenario.py` | Full-history closing balance proven to equal `get_supplier_ap_summary().total_owed` exactly; a **real ordering bug** (see Section 3) was caught and fixed here, then locked down with a deterministic regression test. |
| K — reconciliation | `test_ap_reconciliation.py`, `test_ap_mutation.py` | AP-control-vs-subledger and Purchase-Clearing-vs-outstanding-receipts both proven exact across multi-invoice/multi-payment/credit-note scenarios; the M6 corrupted-`grand_total` mutation test still passes unchanged. |
| L — concurrency | `test_ap_concurrency.py` (5 races × 5 reps = 25 real trials) | See Section 2 — including a **second real bug** this session's own discipline caught and fixed. |
| M — failure injection | `test_ap_failure_injection.py` (9 tests, 2 new for credit notes) | Forced failures after invoice/payment/credit-note creation, during accounting posting, and during audit logging all leave zero financial residue. |
| N — security/RBAC/store isolation | `test_ap_api.py` (15/15, extended for `/ap/payments`, `/ap/credit-notes`) | Cashier has no AP access; Inventory Clerk can write but not post/pay/credit; Manager has all four; Auditor is read-only; the store-isolation mutation test (service-layer check as independent second line of defense) still passes unchanged. |
| O — mutation testing | `test_ap_mutation.py` (16 tests) | See Section 4 — 11 named targets, each demonstrably detected. |
| P — frontend | `AccountsPayablePage.tsx`, `AccountsPayablePage.test.tsx` (4 tests) | Multi-PO lookup, persisted match/variance display, multi-allocation payment UI, credit-note UI, and supplier statement/aging UI — see Section 6. |
| Q — end-to-end scenario | `test_ap_e2e_scenario.py` (1 test, 23 explicit steps) | The full lifecycle: multi-invoice, price variance, credit note, multi-invoice payment, statement/aging/reconciliation, then every adversarial attempt (cross-store, duplicate invoice/payment, over-allocation, overpayment, over-invoicing) — every result explicit, all hand-derived numbers matched on the first run. |
| R — migration downgrade/upgrade | `test_migrations.py` | Upgrade to head from scratch, full downgrade to base, re-upgrade to head — table/permission counts asserted at each milestone boundary; **the M7 downgrade guard tested against real populated data** (Section 5). |

## 2. Concurrency results (real PostgreSQL, independent connections, 5× each)

All five required races (M7 task Section 22) pass, 25/25 real trials:

- **A — two payments race one invoice.** Exactly one of two overlapping 60.00 payments against a 100.00 invoice succeeds; the loser gets `OVERPAYMENT`; `amount_paid` never exceeds `grand_total`.
- **B — two payments allocate across overlapping invoices.** Payment 1 → (X, Y), Payment 2 → (Y, Z), deliberately requested in different id order to prove `record_supplier_payment`'s own ascending-id lock order (not caller-supplied order) prevents deadlock. Both succeed; Y receives exactly its 100.00 balance from the two 50.00 allocations, no overpay, no deadlock.
- **C — two invoice postings race the same PO item's received quantity.** Two DRAFT invoices each for 6 of 10 received units; exactly one posts, the other gets `OVER_INVOICING`.
- **D — a payment and a credit note race to settle the same invoice.** (Substituted for "two users allocate the same supplier credit" — M7 credit notes fully allocate at creation with no unapplied pool to race over; see design doc Section 14/22 for why this is the real equivalent race.) Exactly one of a 60.00 payment and a 60.00 credit note succeeds against a 100.00 invoice; the loser gets `OVERPAYMENT` or `OVER_ALLOCATION`.
- **E — two identical payment requests, same idempotency key.** Exactly one `SupplierPayment` row created; both callers observe the identical id; `amount_paid` reflects the payment once.

### A real concurrency bug found and fixed

Writing race B (and rerunning C) surfaced a genuine defect in the M7
refactor of `post_purchase_invoice`/`void_purchase_invoice`: both functions
added a "preview" query to discover which `PurchaseOrder` rows need locking
(now potentially several, for a multi-PO invoice). That preview queried
full `PurchaseOrderItem` **entities**, which populates SQLAlchemy's session
identity map *before* the lock is acquired. A later "fresh" read of the
same entities, issued *after* the lock, silently returned the **same
cached, pre-lock objects** — SQLAlchemy does not refresh an already-tracked
entity's attributes from a subsequent query by default. The practical
effect: two concurrent invoice postings against the same PO item both
observed a stale `quantity_invoiced` and **both succeeded**, each thinking
it had the lock-protected latest value — race C's exact failure mode,
caught live (`test_c` failed with two `succeeded=True` outcomes instead of
one win/one `OVER_INVOICING`).

**Fix:** the preview query now selects only the scalar `purchase_order_id`
column (`select(PurchaseOrderItem.purchase_order_id)`), never the full
entity — this does not populate the identity map, so the real post-lock
entity load is the *first* load of those rows in the session and is
therefore guaranteed fresh. Applied identically in both
`post_purchase_invoice` and `void_purchase_invoice`. All five concurrency
tests pass cleanly after the fix; this is now documented inline at both
call sites as a load-bearing comment, not just a passing test.

## 3. A real statement-ordering bug found via live smoke test

A live browser smoke test (Playwright, real backend + frontend, real
Postgres) drove a full invoice → payment → credit-note → supplier-statement
flow and produced a nonsensical statement order: the credit note listed
*before* the payment *before* the invoice it was raised against, even
though all three were created today, in that exact chronological order.

**Root cause:** `get_supplier_statement`'s same-day tiebreaker sorted by
each row's own primary key (`inv.id`, `pay.id`, `cn.id`). `PurchaseInvoice`,
`SupplierPayment`, and `SupplierCreditNote` are three **independent**
auto-increment sequences — comparing their raw ids across tables is
meaningless. Whichever table happened to have fewer historical rows in
this dev database sorted first, regardless of real creation order.

**Fix:** sort by `created_at` (from `TimestampMixin`, present on all three
models) instead of `id`. Locked down with a new, deterministic regression
test (`test_supplier_statement_orders_same_day_events_by_created_at_not_row_id`)
that forces the adversarial id/time mismatch directly (via a raw-SQL
backdate) regardless of which table's sequence happens to be ahead in a
given test run — proven to fail under the original id-based code and pass
under the fix by literally re-mutating and re-running it during this audit.

## 4. Mutation testing (11 named targets, M7 task Section 31)

All 11 required targets, plus the two carried over from M6 that remain
relevant, are covered — 16 mutation tests total in `test_ap_mutation.py`,
each mutate → prove the targeted test fails → restore → prove green again:

1. **Invoice matching protection** (`OVER_INVOICING`) — unchanged M6
   coverage, still exercised end-to-end in Session Q.
2. **Duplicate supplier invoice detection** — `_match_or_reject_idempotent_invoice`
   neutered → retry hits the unique constraint instead of returning
   transparently.
3. **Payment allocation bounds** — `_outstanding_balance` neutered →
   the application-level `OVERPAYMENT` guard disappears, but the DB's
   `ck_purchase_invoices_settlement_bounds` CHECK constraint still refuses
   the write (a raw `IntegrityError` instead of a clean domain error) —
   proving BOTH layers matter: the app check for a clean rejection, the DB
   check as the actual last-resort backstop.
4. **Credit allocation bounds** (`OVER_ALLOCATION`) — exercised directly in
   `test_ap_e2e_scenario.py` and `test_ap_concurrency.py` race D.
5. **Payment idempotency** — `_match_or_reject_idempotent_payment` neutered
   → retry re-validates against the now-settled invoice instead of
   returning transparently.
6. **Credit-note idempotency** — new test,
   `_match_or_reject_idempotent_credit_note` neutered → retry hits the
   `(supplier_id, credit_number)` unique constraint.
7. **AP/GL reconciliation detection** — unchanged M6 test (raw-SQL
   `grand_total` corruption) still passes; `ap_reconciliation`'s
   discrepancy is proven a real computation.
8. **Purchase Clearing reconciliation detection** — unchanged M6 coverage,
   still exercised in Session K/Q.
9. **Store isolation** — unchanged M6 mutation test
   (`test_store_isolation_mutation_test_removing_enforce_store_access`)
   still passes: the service-layer check is an independent second line of
   defense to the route-layer one.
10. **Historical receipt cost vs. current WAC** — new test: a product's
    `current_cost` is bumped to 999.00 after receiving at 10.00; posting an
    invoice against the original receipt still clears at 10.00/unit
    (100.00 total), never at the drifted 999.00 WAC — proven by reading the
    actual posted `JournalLine.debit` on the Purchase Clearing account.
11. **Automated-source reversal protection** — unchanged M6 test:
    `SUPPLIER_PAYMENT` removed from `AUTOMATED_SOURCE_TYPES` →
    `reverse_journal_entry` succeeds where it should refuse.

## 5. Migration results

- Fresh-database up/down/up cycle: 33 tables at M6 head → 38 at M7 head (5
  new tables: `purchase_invoice_receipt_matches`, `supplier_payment_allocations`,
  `supplier_credit_notes`, `supplier_credit_note_lines`,
  `supplier_credit_allocations`); 24 permissions (was 23; `ap.credit` added).
- **Against the real, populated `erp_dev` database** (223 purchase invoices,
  162 supplier payments from real M6-era usage/testing): upgrade succeeded;
  all 162 existing `SupplierPayment` rows migrated into exactly 162
  `SupplierPaymentAllocation` rows with byte-for-byte preserved
  `(payment_id, invoice_id, amount)` triples (verified by direct SQL
  comparison before/after); a full downgrade-then-re-upgrade cycle against
  this same populated database completed cleanly with zero data loss
  (verified: `purchase_invoice_id` correctly restored on every
  `supplier_payments` row after downgrade).
- **The M7 downgrade guard, tested against real populated data, not just
  asserted to exist**: `test_m7_downgrade_refuses_when_credit_note_data_exists`
  inserts a real `supplier_credit_notes` row via raw SQL, attempts
  `alembic downgrade`, and confirms it fails loudly with the guard's own
  message *before* any destructive DDL runs (Postgres transactional DDL
  rolls the whole failed migration back — table count and current revision
  unchanged) — directly extending the exact discipline that caught M6's
  own downgrade-vs-populated-data bug to this milestone's own new guards
  (multi-invoice-payment guard, `amount_credited` guard, `purchase_order_id`
  NULL guard, credit-note-existence guard — four independent checks, all
  placed before any `DROP`/`DELETE`).

## 6. Frontend

`AccountsPayablePage.tsx` was extended, not rewritten:

- PO lookup accepts a comma-separated list (`12,13`) for a genuinely
  multi-PO invoice; the matching-status call switches between the
  single-PO and multi-PO endpoints automatically.
- Each invoice line now shows its persisted matched cost and variance
  (summed across its `PurchaseInvoiceReceiptMatch` rows), color-coded for
  favorable/unfavorable — the first place in the UI a user can actually
  see *why* a Purchase Clearing amount is what it is.
- The payment form defaults to fully allocating to the invoice being
  viewed, with an "allocate to another invoice" control for the genuine
  multi-invoice-payment case — never more than one journal per payment
  regardless of how many rows are added.
- A new credit-note form (reason picker, amount, number) — `GOODS_RETURN`
  is flagged in the UI as requiring a purchase-return reference not yet
  exposed by this minimal UI (creatable via the API directly; documented
  as a known frontend gap, not silently hidden).
- A new supplier-tools panel (statement + aging + summary) reachable from
  the landing page by supplier id.

All 4 `AccountsPayablePage.test.tsx` tests pass (1 new: creates a
multi-allocation payment and a credit note against a live-posted invoice,
asserting the exact request bodies sent). Full frontend suite: 30/30.
`tsc -b`, `oxlint`, and `vite build` all clean.

**Live smoke test** (Playwright, real backend + Postgres + Vite dev
server, not mocked): logged in as a real Manager user, looked up a real
PO, created and posted a real invoice (with a genuine `DUPLICATE_SUPPLIER_INVOICE_NUMBER`
rejection rendered live on a retried invoice number), recorded a real
40.00 payment, issued a real 5.00 credit note, and confirmed the invoice's
displayed balance due (55.00) and status (Partially Paid) exactly matched
server-computed values — then looked up the supplier and confirmed the
live statement/aging view. This smoke test is what caught the statement-
ordering bug in Section 3.

## 7. Performance review

- `get_supplier_statement`/`get_supplier_ap_summary`/`get_supplier_transaction_history`
  each issue 2-3 queries (one per entity type, no per-row N+1) — unchanged
  shape from M6, now extended to include credit notes the same way.
- `post_purchase_invoice`'s multi-PO lock loop issues one `SELECT ... FOR
  UPDATE` per distinct PO touched by the invoice (typically one; more only
  for a genuinely multi-PO invoice) — no N+1 within a single PO's items.
- `ap_aging`/`purchase_clearing_reconciliation` remain O(suppliers ×
  purchase orders × items) in-Python loops calling `_fifo_clearing_amount`
  per outstanding item — unchanged, already-documented M6 debt (fine at
  current data volumes; would want a SQL-side rewrite before this scales
  to thousands of open PO items per store). Not addressed in M7 — out of
  scope for this milestone, and premature to optimize without a real
  volume problem.
- No new indexes were required beyond the ones the migration already adds
  on every new table's FK columns (`ix_pi_receipt_matches_*`,
  `ix_supplier_payment_allocations_*`, `ix_supplier_credit_*`) — every new
  query path filters or joins on one of these.
- No denormalized cache was introduced without a reconciliation check:
  `PurchaseInvoice.amount_paid`/`amount_credited` are maintained caches
  (same pattern as M6's `amount_paid`), and `ap_reconciliation`/
  `purchase_clearing_reconciliation` are exactly the reconciliation checks
  that prove they never drift from the underlying rows.

## 8. Final auditor self-review (M7 task Section 35)

Can the system:

1. **Invoice the same received quantity twice?** No — `OVER_INVOICING`,
   enforced both at the application layer (checked at POST time against
   live `quantity_received`) and by the same-row DB CHECK
   (`ck_purchase_order_items_qty_invoiced_bounds`, unchanged from M6).
2. **Pay an invoice twice?** No — idempotency by `client_transaction_id`
   (checked pre- and post-lock) plus the per-allocation `OVERPAYMENT`
   bound plus the DB `ck_purchase_invoices_settlement_bounds` backstop
   (Section 4, target 3).
3. **Allocate a payment twice?** No — `uq_supplier_payment_allocations_payment_invoice`
   prevents the same payment allocating to the same invoice twice within
   one request (also rejected explicitly as `DUPLICATE_ALLOCATION_TARGET`
   before that constraint would even be reached).
4. **Allocate more payment than exists?** No — allocations must sum to
   exactly the payment's own `amount` (`ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT`).
5. **Allocate more credit than exists?** No — same rule for credit notes
   (`ALLOCATION_MUST_EQUAL_CREDIT_AMOUNT`), plus `OVER_ALLOCATION` per
   invoice.
6. **Create a supplier credit with no financial source?** No —
   `GOODS_RETURN` requires a real `purchase_return_id` reference (validated
   to belong to the same store/supplier, and bounded to that return's own
   value); `COMMERCIAL_DISCOUNT` requires no physical reference and posts
   through the existing Purchase Discounts account, never inventing a new
   one.
7. **Make AP subledger differ from GL?** No — `ap_reconciliation` proven a
   real, working check (Section 4, target 7); zero discrepancy across
   every multi-invoice/multi-payment/credit-note scenario tested.
8. **Make Purchase Clearing differ from actual unmatched receipts?** No —
   `purchase_clearing_reconciliation` unchanged and still exact; credit
   notes never touch this account (design doc Section 13).
9. **Silently accept a price variance?** No — automated only for the
   *quantity* actually received (never invented), posted to a named,
   auditable account, and now also persisted per-lot
   (`PurchaseInvoiceReceiptMatch.variance_amount`) for full traceability.
10. **Silently accept an invoice above received quantity?** No — hard
    `OVER_INVOICING` rejection, never silently capped or accepted.
11. **Duplicate supplier invoices?** No — composite
    `(supplier_id, invoice_number)` uniqueness, unchanged from M6, plus the
    same protection now extended identically to credit notes
    (`(supplier_id, credit_number)`).
12. **Modify posted invoices?** No — no endpoint or service function
    exposes a field-level edit of a POSTED invoice's financial values;
    `amount_paid`/`amount_credited` change only through
    `record_supplier_payment`/`create_supplier_credit_note` under a row
    lock, by construction, never by direct assignment elsewhere.
13. **Modify posted payments?** No — `SupplierPayment`/`SupplierPaymentAllocation`
    rows are never updated after creation by any code path.
14. **Bypass operational reversal through accounting?** No —
    `SUPPLIER_CREDIT_NOTE` added to `AUTOMATED_SOURCE_TYPES`, blocking the
    generic `reverse_journal_entry` path exactly like every other automated
    source type (Section 4, target 11, for the pre-existing
    `SUPPLIER_PAYMENT` case; the same mechanism now also covers credit
    notes by construction since it's the identical block list).
15. **Cross store boundaries?** No — every mutating AP function validates
    `store_id` against every invoice/payment/credit-note it touches
    (including each one of a multi-invoice payment's targets
    individually), at both the route layer and the service layer
    independently (Section 1, Session N).
16. **Bypass RBAC?** No — `ap.credit` added as its own permission,
    correctly withheld from Cashier and Inventory Clerk, granted to
    Admin/Manager only (Session N).
17. **Leave partial state after a failure?** No — 9 failure-injection
    tests (2 new for credit notes) covering accounting-posting and
    audit-logging failure points, all proving zero residue (Section 1,
    Session M).
18. **Produce incorrect aging?** No — bucketing uses the same
    `_outstanding_balance` helper as the subledger and reconciliation
    reports (a single definition, not three independent ones that could
    drift).
19. **Produce an incorrect supplier statement?** **This was YES until this
    audit caught and fixed it** (Section 3) — a real same-day ordering bug
    from comparing cross-table row ids. Fixed, and locked down with a
    deterministic regression test proven to fail under the original code
    and pass under the fix. The closing-balance arithmetic itself was
    never wrong (proven exact in Session J); only the *display order* of
    same-day lines was.
20. **Create duplicate cash/bank accounting for one supplier payment?**
    No — `post_supplier_payment_journal` posts exactly one journal entry
    per payment regardless of how many invoices it allocates across
    (Section 1, race B proves this doesn't fragment under concurrency
    either).

No unresolved "yes" remains. Item 19 was a real, caught, fixed, and
regression-tested defect — disclosed here rather than omitted, per the
instruction that a financial auditor's job is to find problems, not hide
them.

## 9. Known limitations / deferred functionality

Unchanged from `docs/M7_ADVANCED_AP_SETTLEMENT.md`'s "Deferred" section:
multi-currency; recoverable/input purchase tax; freight/landed-cost
allocation; supplier debit notes; unapplied supplier payments/credits and
supplier advances; draft-invoice editing; voiding a paid/partially-paid
invoice; a stateful, queryable exception-approval workflow beyond the
existing hard-block-at-post mechanism; per-supplier GL sub-accounts. Also
still deferred from M6 and unchanged by M7: recoverable input tax,
freight/landed cost (same items, restated for continuity).

The frontend's `GOODS_RETURN` credit-note path requires a
`purchase_return_id` the minimal UI does not yet collect (documented in
Section 6) — creatable via the API; a follow-up UI affordance, not a
missing capability.

## 10. Final validation gates

- Backend: **349/349** tests pass (was 339 at M6; net +10 across new
  concurrency/failure-injection/mutation/migration/reconciliation tests,
  after accounting for consolidation of the e2e scenario file).
- Frontend: **30/30** tests pass (was 29 at M6; +1).
- `ruff check .`: clean. `black --check .`: clean. `mypy app`: clean (61
  source files). `tsc -b`: clean. `oxlint`: clean. `vite build`: clean.
- Migration up/down/up: clean against both a fresh database and the real
  populated `erp_dev` database; the new downgrade guard verified to
  actually fire against real populated data, not just asserted to exist.
- Real PostgreSQL concurrency tests: 5 races × 5 repetitions = 25/25 real
  trials pass; one genuine defect (Section 2) was caught, fixed, and
  reverified.
- Failure injection: 9/9 tests pass, zero residue in every case.
- Mutation testing: 16/16 tests pass, each independently proven to detect
  its target mutation and recover after restore.
- Live API + live frontend smoke test: full invoice → payment →
  credit-note → statement lifecycle exercised through the real browser
  against the real backend and a real Postgres database; one genuine
  defect (Section 3) was caught, fixed, and reverified live.

## Verdict: **PASS**

No unresolved financial-control defect remains. Both real defects this
milestone's own required testing discipline surfaced (the identity-map
concurrency bug in Section 2, and the cross-table-id statement-ordering
bug in Section 3) were found, root-caused, fixed, and locked down with a
regression test proven to fail under the original code — exactly the
process this document exists to make visible, not merely a list of green
checkmarks.
