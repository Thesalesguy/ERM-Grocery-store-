# M6 — Hardening Audit: Accounts Payable, Purchase Invoices & Vendor Settlement

Companion to `docs/M6_AP_VENDOR_ACCOUNTING.md` (design/behavior). This
document is the adversarial self-audit, the mutation-testing evidence,
the real migration-safety finding from testing against populated data,
and the final verdict for M6, in the same style and rigor as
`docs/M4_HARDENING_AUDIT.md` and `docs/M5_HARDENING_AUDIT.md`.

---

## 1. Test inventory

| File | Tests | Session |
|---|---|---|
| `tests/test_ap_invoices.py` | 18 | B/C/D/E (domain, lifecycle, matching, accounting) |
| `tests/test_ap_payments.py` | 8 | F (payment domain/accounting) |
| `tests/test_ap_reconciliation.py` | 2 | G (AP/Purchase Clearing reconciliation, aging, summary) |
| `tests/test_ap_concurrency.py` | 3 (×5 repeats each) | H (concurrency) |
| `tests/test_ap_failure_injection.py` | 5 | I (failure injection) |
| `tests/test_ap_api.py` | 15 | J (API/RBAC/security) |
| `tests/test_ap_mutation.py` | 4 | L (permanent automated mutation tests) |
| `tests/test_ap_e2e_scenario.py` | 1 (24-step comprehensive scenario) | M (end-to-end) |
| **New tests total** | **56** | |
| Full backend suite (pre-M6: 283) | **339** | all pass |
| Frontend (`AccountsPayablePage.test.tsx`, new) | 3 | K (frontend) |
| Full frontend suite (pre-M6: 26) | **29** | all pass |

Plus three mutation exercises performed live during this audit as a
deliberate, reverted source-code edit (not permanent test files, since
each protection is *which computation an inline expression uses*, not a
call to a separately-patchable function) — see §5.

---

## 2. What each test session actually proved

- **Sessions B–E** (`test_ap_invoices.py`): a DRAFT invoice has zero
  accounting/quantity effect; due dates default correctly from supplier
  payment terms; `(supplier_id, invoice_number)` duplicate detection
  works and is scoped correctly (the same number is legitimately reused
  across different suppliers); idempotent creation with both matching
  and conflicting payloads behaves correctly; posting a clean match
  establishes AP and clears Purchase Clearing for the exact matched
  value; posting is idempotent by state; over-invoicing is rejected at
  POST time specifically (not at creation), matching the task's own
  worked example; partial invoicing correctly leaves a PO item open for
  further invoicing; **FIFO matching across two receipts at different
  costs prices each invoiced unit at its own lot's actual cost** (the
  single most important correctness property in this milestone),
  computed both for unfavorable and favorable price variance; tax and
  discount post to their own dedicated accounts and the entry still
  balances; the matching-status read helper reflects
  ordered/received/invoiced/invoiceable correctly; voiding a DRAFT is a
  pure status flip while voiding a POSTED invoice reverses
  `quantity_invoiced` and posts an exact mirror journal (re-derived from
  the ORIGINAL posted journal's own lines, not recomputed — see
  §"Void" in the design doc for why recomputation would be unsafe); a
  voided PO item can be re-invoiced from scratch; voiding twice is
  idempotent.
- **Session F** (`test_ap_payments.py`): partial and full (two-
  installment) payments correctly update `amount_paid`/status and post
  Dr AP / Cr the correct real asset account per payment method
  (proving `SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE` is a genuinely
  separate mapping from the sales-side one); overpayment is rejected
  both as a single oversized payment and as a second payment that would
  push the total over the balance; a DRAFT invoice cannot accept a
  payment; idempotent retry does not double-apply an amount; a
  conflicting payload under the same key is rejected; an invoice with a
  recorded payment cannot be voided.
- **Session G** (`test_ap_reconciliation.py`): after a partial invoice
  and partial payment, `ap_reconciliation`, `purchase_clearing_reconciliation`,
  `get_supplier_ap_summary`, `ap_aging`, and
  `get_supplier_transaction_history` all independently agree with
  hand-derived expected values (both tests passed on the first run,
  confirming the hand arithmetic and the implementation matched
  independently) — including correct current/overdue bucketing against
  `due_date`.
- **Session H** (concurrency, 5× each): two concurrent payments together
  exceeding an invoice's balance — exactly one wins, the loser gets
  `OVERPAYMENT`, `amount_paid` never exceeds `grand_total`; two
  concurrent invoice postings together exceeding a PO item's received
  quantity — exactly one wins, the loser gets `OVER_INVOICING` (or, when
  the FIFO walk itself detects the shortfall first,
  `MATCH_DATA_INCONSISTENT` — a second independent layer, not a bug, see
  §4); two concurrent payments with the same idempotency key — exactly
  one `SupplierPayment` row, both callers see the identical id. All
  three scenarios were designed with the double idempotency-check
  pattern (pre-lock fast path + post-lock re-check) already baked in
  from the start — the exact fix M5 had to discover mid-milestone — and
  passed cleanly on the first run and every repeat.
- **Session I** (failure injection): forced `RuntimeError` at five
  distinct points — inside invoice-posting accounting, inside
  invoice-posting audit logging, inside void accounting, inside
  payment accounting, inside payment audit logging — each proving a
  complete rollback: no quantity change, no status change, no payment
  row, no journal entry survives.
- **Session J** (API/security): RBAC boundaries hold at the HTTP layer
  for all four permissions across Cashier (none), Inventory Clerk
  (write only), Manager (all), Auditor (read only); multi-store
  isolation holds and is proven independent at two layers by a mutation
  test; idempotency holds at the HTTP boundary including the
  conflicting-payload case (`409`); the adversarial-input battery
  (over-invoicing, tampered PO-item id, negative/zero quantity via
  schema `422`, malformed invoice id, a SQL-injection-shaped query
  parameter, an oversized 501-line payload) all behave correctly.
- **Session L** (mutation testing): all 8 required targets exercised —
  4 as permanent automated tests (store isolation — reusing the same
  pattern proven independently in Session J; invoice idempotency;
  payment idempotency; AP-to-GL reconciliation detecting real
  corruption; automated-source block for `SUPPLIER_PAYMENT`), 3 as live
  reverted edits during this audit (overpayment protection,
  over-invoicing protection, historical receipt-cost usage in FIFO
  matching) — see §5 for exact evidence. (That is 4 automated + 3 manual
  + store isolation counted once = the 8 required targets.)
- **Session M** (`test_ap_e2e_scenario.py`): the full required 24-step
  scenario — supplier → PO → two receipts at different costs → WAC
  verification → a two-line invoice matching both receipts exactly (zero
  variance by construction) → posting → Purchase Clearing fully cleared
  → AP established → two partial payments → PAID → supplier balance →
  AP-vs-GL reconciliation → Purchase Clearing reconciliation →
  Inventory-vs-GL reconciliation (unchanged, same bounded WAC-rounding
  drift) → COGS unaffected (0, nothing sold) → duplicate invoice
  rejected → duplicate payment idempotent (not double-applied) → cross-
  store access rejected (both payment and void) → overpayment rejected
  on a fully-paid invoice → over-invoicing rejected on a fully-matched PO
  item — passed on the first run, confirming every hand-derived number
  in the design doc.
- **Session N** (migration, real populated data): see §6 — found and
  fixed a real bug.
- **Session K** (frontend): typecheck/lint/build clean; 3 new component
  tests pass; a live-browser smoke test against the real running backend
  (not mocked) walked the full DRAFT → POSTED → PARTIALLY_PAID flow —
  created a real invoice against a real receipt, posted it, recorded a
  partial payment, and confirmed the UI's balance-due figure (40.00)
  matched the server's own computation exactly, with a screenshot
  captured at each step.

---

## 3. Performance review

- **One real N+1 found and fixed**: `create_purchase_invoice`'s per-line
  `PurchaseOrderItem`/`Product` lookups (`db.get()` per line, up to 500)
  were batched into two `IN(...)` queries — the identical fix M5 applied
  to `create_sale_return` for the identical reason (a plain read with no
  locking/ordering requirement).
- **Accepted, documented debt**: `ap_reconciliation`,
  `purchase_clearing_reconciliation`, and `get_supplier_ap_summary` each
  call the FIFO-matching helper once per PO item with an outstanding
  balance. These are periodic reporting functions, not hot transactional
  paths — not optimized in M6, matching the task's own "do not
  prematurely optimize unrelated modules" instruction.
- **Indexes**: `purchase_invoices` (`store_id`, `supplier_id`,
  `purchase_order_id`, `status`), `purchase_invoice_lines`
  (`purchase_invoice_id`, `purchase_order_item_id`), `supplier_payments`
  (`store_id`, `supplier_id`, `purchase_invoice_id`) all present; the
  FIFO query's own join target, `ix_goods_receipt_items_purchase_order_item_id`,
  already existed from M3. No missing index found.
- **Dependency audit**: no new backend or frontend dependency added by
  M6. `pip-audit` reports the same pre-existing `pip`/`setuptools`
  tooling CVEs unrelated to any application dependency (unchanged from
  M5's own finding). `npm audit`: 0 vulnerabilities.

---

## 4. Final self-audit — the 16 required questions

**1. Can the system create an AP liability without a real supplier
invoice?** No — the only code path that credits `ACCOUNT_ACCOUNTS_PAYABLE`
is `post_purchase_invoice_journal`, called exclusively from
`post_purchase_invoice`, which requires a real, already-created
`PurchaseInvoice` with real lines matched against real
`GoodsReceiptItem` data.

**2. Can the system pay the same invoice twice?** No — proven under a
genuine concurrent race (not just sequential retries), 5×
(`test_c_two_concurrent_payments_with_same_idempotency_key_create_exactly_one`),
using the double idempotency-check pattern (pre- and post-lock) from the
start.

**3. Can the system pay more than is owed?** No — `OVERPAYMENT` at the
application layer, backstopped by `ck_purchase_invoices_amount_paid_bounds`
at the database layer (proven load-bearing by a mutation test that
disabled the application check and confirmed the DB constraint caught
the resulting write).

**4. Can the system invoice more than was received without an explicit
exception?** No — `OVER_INVOICING` at posting time, backstopped by the
FIFO-matching logic's own `MATCH_DATA_INCONSISTENT` defense (proven by a
mutation test that disabled the primary check and confirmed the FIFO
walk itself refused to fabricate a clearing amount for units that were
never received).

**5. Can the system clear Purchase Clearing incorrectly?** No — every
clearing amount is FIFO-matched against real `GoodsReceiptItem` rows
(§5 of the design doc), proven exact in
`test_fifo_matching_across_two_receipts_at_different_costs` and in the
end-to-end scenario; a mutation test confirmed that substituting the
product's *current* cost for the historical receipt cost breaks this
exact test.

**6. Can the AP subledger differ from the GL?** Not without detection —
`ap_reconciliation`'s discrepancy is proven to be a real, working
computation (not a decorative always-zero report) by directly corrupting
a posted invoice's stored total via raw SQL and confirming the
discrepancy becomes nonzero and points at exactly the corrupted amount.

**7. Can a posted invoice be changed?** No API endpoint or service
function edits a `PurchaseInvoiceLine`, or `PurchaseInvoice.subtotal`/
`discount_total`/`tax_total`/`grand_total`/`invoice_number`/dates, after
creation. The only fields that legitimately change post-creation are
`status`, `amount_paid`, `posted_at`, `voided_at` — each touched by
exactly one dedicated, guarded function.

**8. Can historical invoice economics be modified?** No — `unit_price`,
`quantity_invoiced`, `discount_amount`, `tax_amount` on
`PurchaseInvoiceLine` are set once at creation and never touched again by
any code path.

**9. Can a supplier payment be created without accounting?** No —
proven by forced-failure injection: when `post_supplier_payment_journal`
is forced to raise, the entire attempt (including the `SupplierPayment`
row itself, already flushed) rolls back completely.

**10. Can accounting be created without a supplier transaction?** No —
`post_purchase_invoice_journal`/`post_purchase_invoice_void_journal`/
`post_supplier_payment_journal` are each called from exactly one
operational service function, always with a real, already-persisted
source row (`source_id`) to reference.

**11. Can store isolation be bypassed?** No — checked at two independent
layers (route + service), proven independent by a mutation test that
disabled the route-layer check and confirmed the service-layer check
alone still rejected the request.

**12. Can RBAC be bypassed?** No — four permissions
(`ap.read`/`write`/`post`/`pay`), each independently tested for every
role (Cashier: none; Inventory Clerk: read+write only; Manager: all;
Auditor: read only).

**13. Can an automated payment journal be reversed manually?** No —
`SUPPLIER_PAYMENT` (and `PURCHASE_INVOICE`/`PURCHASE_INVOICE_VOID`) are
`AUTOMATED_SOURCE_TYPES` members, so `reverse_journal_entry`'s existing
block refuses them (`OPERATIONAL_REVERSAL_REQUIRED`) — proven live, not
just by tuple membership, by a mutation test that removed
`SUPPLIER_PAYMENT` from the tuple and confirmed the bare reversal would
otherwise succeed.

**14. Can partial state be left after a failure?** No — five distinct
forced-failure points (invoice-posting accounting/audit, void
accounting, payment accounting/audit) each proved a complete rollback to
the exact pre-attempt state.

**15. Can duplicate liabilities be created under concurrency?** No —
proven for both invoice-posting and payment-creation races, 5× each,
against real independent PostgreSQL connections.

**16. Can a Purchase Clearing discrepancy be hidden?** No —
`purchase_clearing_reconciliation` computes the exact FIFO-priced
outstanding value from live PO-item data and compares it against the GL
balance; the same underlying mechanism proven real (not decorative) for
`ap_reconciliation` in question 6 applies identically here, since both
functions share the identical "read the actual ledger, compute the
actual subledger total, subtract" shape.

No answer to any of these 16 questions is "yes" in a way that would
constitute silent financial corruption or uncontrolled financial
mutation.

---

## 5. Mutation testing — all 8 required targets

| # | Protection | Method | Result |
|---|---|---|---|
| 1 | AP store isolation | Permanent automated test — monkeypatched out the route-layer `enforce_store_access` call | Request still `403`'d by the independent service-layer check |
| 2 | Invoice idempotency | Permanent automated test — monkeypatched `_match_or_reject_idempotent_invoice` to always return `None` | A same-key retry against an already-numbered invoice raised `DUPLICATE_SUPPLIER_INVOICE_NUMBER` instead of transparently returning the original |
| 3 | Payment idempotency | Permanent automated test — monkeypatched `_match_or_reject_idempotent_payment` to always return `None` | A same-key retry against a now-fully-paid invoice raised `INVALID_INVOICE_STATE` instead of transparently returning the original payment |
| 4 | Overpayment protection | Live, reverted edit — disabled the `OVERPAYMENT` application check | Two tests failed as expected; the DB `CHECK` constraint (`ck_purchase_invoices_amount_paid_bounds`) then rejected the write with a `CheckViolation` — a real second layer, not decorative |
| 5 | Over-invoicing protection | Live, reverted edit — disabled the `OVER_INVOICING` application check | Two tests failed as expected; the FIFO-matching helper's own `MATCH_DATA_INCONSISTENT` guard then rejected the attempt instead — a second independent layer catching the same corruption a different way |
| 6 | AP-to-GL reconciliation protection | Permanent automated test — after a clean post, directly corrupted the invoice's stored `grand_total` via raw SQL | `ap_reconciliation`'s discrepancy changed from `0.00` to exactly `500.00` (the corruption amount) — confirming the check is a real, working computation |
| 7 | Historical invoice/receipt values | Live, reverted edit — changed `_fifo_clearing_amount` to use the product's *current* cost instead of each receipt lot's own frozen `unit_cost` | `test_fifo_matching_across_two_receipts_at_different_costs` failed (`159.999996` vs. expected `140.000000`) |
| 8 | Automated-source reversal protection for supplier payments | Permanent automated test — monkeypatched `AUTOMATED_SOURCE_TYPES` to exclude `SUPPLIER_PAYMENT` | The direct reversal, normally blocked with `OPERATIONAL_REVERSAL_REQUIRED`, succeeded instead — confirming the tuple membership (not some other guard) is what stops it |

Every live, reverted edit was confirmed reverted and the full 339-test
backend suite confirmed green again immediately afterward, before moving
to the next mutation.

---

## 6. Migration safety (Session N) — a real finding

Verified via a genuine downgrade→upgrade cycle against the real,
populated `erp_dev` database (223 `purchase_invoices`, 223
`purchase_invoice_lines`, 111 `supplier_payments`, 167
`purchase_order_items` with `quantity_invoiced > 0` at the time of the
test) — not just the from-scratch cycle in `test_migrations.py`.

**The first attempt crashed**: `alembic downgrade -1` raised a raw
`psycopg.errors.ForeignKeyViolation` — the migration's `DELETE FROM
accounts WHERE code = ANY(...)` step tried to delete the five new
accounts while real `journal_lines` (from real posted M6 transactions)
still referenced them. Alembic's transactional DDL rolled the whole
downgrade back cleanly (confirmed: `alembic current` still showed head
immediately after), so no data was corrupted — but a raw driver
exception crashing mid-migration is not an acceptable failure mode.

**Fixed**: moved the guard to the very start of `downgrade()`, checking
`journal_lines` joined to `accounts` directly (the actual failure
condition) rather than only checking `journal_entries.source_type` (an
approximation that happened to cover this case but wasn't the literal
constraint being violated). Re-ran the downgrade against the same
populated database: it now fails **cleanly**, with a clear, actionable
`RAISE EXCEPTION` message naming exactly which accounts are still
referenced and why, before touching anything else.

**Both required outcomes verified**:
- Against a **fresh, empty database** (the dedicated `test_migrations.py`
  cycle): the full upgrade → downgrade → upgrade cycle completes
  cleanly, table count and revision both correct
  (`test_full_upgrade_downgrade_upgrade_cycle`,
  `test_rbac_seed_data_present_after_upgrade`, permission count 23).
- Against the **real, populated `erp_dev` database**: the downgrade
  correctly and cleanly **refuses** (rather than crashing or silently
  destroying/orphaning ledger history) once real M6 financial
  transactions exist — which is the financially correct outcome, not a
  workaround. `alembic current` confirmed no partial damage from either
  the original crash or the now-clean refusal.

This is exactly the kind of defect Session N's "test against real
populated data" requirement exists to catch, and it is the second
milestone in a row (after M5's idempotency-race finding) where this
milestone's own testing discipline surfaced and fixed a real bug rather
than merely confirming the absence of known ones.

---

## 7. Final validation gates

| Gate | Result |
|---|---|
| Backend full suite | 339 passed |
| Frontend full suite | 29 passed |
| `black --check` | clean |
| `ruff check` | clean |
| `mypy app` | Success: no issues found in 61 source files |
| `tsc --noEmit` | clean |
| `oxlint` | clean |
| `prettier --check` | clean |
| `npm run build` | succeeds |
| Migration up/down/up (dedicated DB, `test_migrations.py`) | passes |
| Migration up/down (real populated `erp_dev`) | correctly refuses once real M6 data exists — see §6 |
| Concurrency (5× each of 3 scenarios) | all pass |
| Failure injection (5 scenarios) | all pass |
| Mutation testing (8 required targets) | all pass |
| `pip-audit` | pre-existing, non-application tooling CVEs only |
| `npm audit` | 0 vulnerabilities |
| Live API/browser smoke test | passes (see §2 Session K) |
| End-to-end 24-step financial scenario | passes |

---

## 8. Verdict

**PASS**

No unresolved financial-control defect exists: no duplicate financial
posting, no incorrect AP balance, no incorrect Purchase Clearing, no
incorrect supplier payment, no broken three-way matching, no broken
atomicity, no concurrency overpayment, no concurrency over-invoicing, no
broken idempotency, no mutable posted financial data, no cross-store
financial mutation, no unauthorized financial action, no unexplained
reconciliation discrepancy.

The one real defect this milestone's own testing surfaced — the
migration crashing (rather than failing safely) against populated data —
was found and fixed within the same development pass, and the fix was
verified against both a fresh database and the actual real, populated
database that originally exposed it, before this audit was written.
There is no known condition attached to shipping.

---

## 9. Known remaining risks / deferred (unchanged from the design doc)

See `docs/M6_AP_VENDOR_ACCOUNTING.md` §15 — no external payment/banking
integration, one invoice per PO and one payment per invoice, no
multi-currency, no recoverable input tax, no freight/landed-cost
allocation, no credit notes, no voiding a paid/partially-paid invoice, no
draft editing, and one documented reporting-path N+1. None of these are
security or correctness gaps; all are explicit, honest scope boundaries.

---

## 10. Delivery

- Implementation and this documentation are committed **separately**,
  per the task's explicit instruction.
- No pull request opened.
- M7 not started.
