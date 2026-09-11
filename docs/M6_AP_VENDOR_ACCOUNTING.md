# M6 — Accounts Payable, Purchase Invoices & Vendor Settlement

This document records the M6 milestone's design decisions and explains
the Accounts Payable module (`backend/app/modules/ap/*`) and its
integration into the M0–M5 foundation. Read alongside
`docs/M3_PURCHASING_RECEIVING_WAC.md`, `docs/M4_ACCOUNTING_CORE.md`,
`docs/M4_HARDENING_AUDIT.md`, `docs/M5_RETURNS_VOIDS_REFUNDS.md`, and
`docs/M5_HARDENING_AUDIT.md`, which remain authoritative for anything
this document doesn't override.

M6's objective: turn Purchase Clearing — an M3/M4-era interim liability
with no per-supplier subledger behind it — into a controlled Accounts
Payable workflow, **without renaming it and calling it done**. Purchase
Clearing and Accounts Payable are now two distinct, real liabilities with
distinct meanings: the first tracks *received, not yet invoiced*; the
second tracks *invoiced, not yet paid*. A supplier invoice is the event
that moves value from one to the other.

---

## 1. Pre-implementation review

Traced the actual code before writing anything:

- `Supplier` (M3): already had `code`, `contact_name`, `phone`, `email`,
  `address`, `tax_id`, `is_active` — global/shared reference data, RBAC
  reused from `purchasing.read`/`purchasing.write`. The only genuinely
  missing AP field was payment terms (see §2).
- `PurchaseOrder`/`PurchaseOrderItem`/`GoodsReceipt`/`GoodsReceiptItem`
  (M1/M3): `PurchaseOrderItem.quantity_received` is a maintained cache,
  incremented by `receive_goods` under a lock on the parent
  `PurchaseOrder` row. `GoodsReceiptItem.unit_cost` is the *actual*
  received cost (independent of the PO's own estimated `unit_cost`) —
  this is the authoritative, per-lot historical cost M6's matching logic
  reuses unchanged.
- `post_goods_receipt_journal` (M4): **Dr Inventory / Cr Purchase
  Clearing**, for `Σ quantize(quantity × unit_cost)` per receipt line,
  quantized at the same `Decimal("0.000001")` ledger precision WAC
  already uses. This exact, unmodified function is still what credits
  Purchase Clearing — M6 adds nothing here.
- `ACCOUNT_PURCHASE_CLEARING` (M4, `docs/M4_ACCOUNTING_CORE.md` §9): its
  own docstring already called it "named 'clearing' only because there is
  no per-supplier open-balance subledger yet" — i.e. M4 already
  anticipated this exact gap and named the account accordingly rather
  than calling it "the" payable.
- No `PurchaseInvoice`/`SupplierPayment` model, service, schema, or
  endpoint existed anywhere — fully greenfield, confirmed by a
  case-insensitive grep for "invoice" across the codebase before writing
  any code.

Conclusion: build AP as a **new module** (`app/modules/ap/`), the same
way `accounting` was split out from `sales`/`purchasing` in M4 rather
than crammed into an existing module — this is a genuinely new bounded
context. Reuse `GoodsReceiptItem.unit_cost` and `PurchaseOrderItem
.quantity_received` unchanged as the source of truth for matching;
extend `PurchaseOrderItem` with one new cache column
(`quantity_invoiced`) mirroring `quantity_received`'s own pattern
exactly; never touch `post_goods_receipt_journal` or the Purchase
Clearing posting itself.

---

## 2. Supplier master

Added exactly one field: `Supplier.default_payment_terms_days: int |
None` (nullable — `None` means "no standing term," due immediately).
Everything else the M6 task's checklist asked about already existed
(`tax_id`, contact fields, `is_active`) or was deliberately **not**
added:

- **No default currency** — this system has no multi-currency support
  anywhere (`Sale`, `JournalLine`, `PurchaseOrder` all assume one
  implicit system currency); adding a currency field with no FX-rate
  handling behind it would misstate accounting rather than genuinely
  support multi-currency. `PurchaseInvoice` has no `currency` column for
  the same reason — deferred as a real limitation, not silently assumed.
- **No payment account/reference** — would be either unused metadata or
  exactly the "banking credentials" the task says not to store.

---

## 3. Purchase invoice lifecycle

`PurchaseInvoice.status`: `DRAFT → POSTED → PARTIALLY_PAID → PAID`, with
`VOIDED` reachable from `DRAFT` or `POSTED` (not from `PARTIALLY_PAID`/
`PAID` — see §12).

- **DRAFT**: `create_purchase_invoice` records exactly what the
  supplier's paper/PDF invoice says — supplier, PO, invoice number/date,
  lines (quantity, unit price, discount, tax per line). **No accounting
  effect, no `quantity_invoiced` change.** This is deliberate: an
  operator can enter an invoice before every expected unit has arrived
  (e.g. a partial shipment invoice) without the system pretending
  anything has been matched or committed yet.
- **POSTED**: `post_purchase_invoice` is the financial commitment point
  — three-way-match validation (§4), FIFO cost matching (§5), the
  Purchase Clearing/AP/variance/tax/discount journal (§6), and the
  `quantity_invoiced` update, all in one transaction. **A POSTED
  invoice's lines are never edited again** — the only way to undo one is
  `void_purchase_invoice` (§12), a real operational reversal, never a
  raw field edit.
- **PARTIALLY_PAID / PAID**: driven purely by `amount_paid` vs.
  `grand_total`, set by `record_supplier_payment` (§9).

Posting is **idempotent by state**, not by a separate key — mirrors
`submit_purchase_order`'s status-transition precedent (no
`client_transaction_id`), not `create_sale_return`'s
resource-creation precedent: calling `post_purchase_invoice` on an
already-POSTED/PARTIALLY_PAID/PAID invoice is a no-op that returns the
current state, since posting a DRAFT is the only transition this
function performs and it cannot happen twice.

**Invoice numbering**: `invoice_number` is the **supplier's own**
document number — operator-entered, never system-generated (a business
cannot invent its supplier's numbering). Unique **per supplier**
(`uq_purchase_invoices_supplier_invoice_number`, a composite
`(supplier_id, invoice_number)` constraint), explicitly **not** globally
unique — two different suppliers routinely reuse the same numbering
sequence, and a global-uniqueness assumption would eventually reject a
perfectly legitimate second supplier's "INV-0001." A **separate**
`client_transaction_id` is the create-request idempotency key (M2/M3/M5
pattern) — both matter, for different failure modes: the key protects a
retried *create attempt* (double-click, network retry); the per-supplier
uniqueness protects against *genuinely re-entering the same real-world
document* later under a fresh key.

**One invoice references exactly one `purchase_order_id`** — an invoice
spanning multiple purchase orders is deferred (see §15). This still
supports every scenario the M6 task requires: one invoice against
multiple receipts (its lines reference `purchase_order_item_id`s that
may span several `GoodsReceipt`s — §5) and multiple invoices against one
PO (no exclusivity constraint prevents a second invoice against the same
PO).

**Draft editing/deletion**: not implemented in M6. A DRAFT has zero
financial/quantity effect, so a mistaken DRAFT is harmless clutter, not a
correctness risk — an operator who makes a mistake leaves the bad DRAFT
un-posted (or voids it, a pure status flip for a DRAFT — §12) and
creates a corrected one.

---

## 4. Three-way matching policy

The binding ceiling for how much of a PO item can be invoiced is
**RECEIVED, not ORDERED**: `quantity_invoiced ≤ quantity_received`,
enforced identically at the application layer (`OVER_INVOICING`,
checked at POST time — see below) and at the database layer
(`ck_purchase_order_items_qty_invoiced_bounds`, a same-row CHECK). You
owe a supplier for what physically arrived — even more than ordered, if
over-received (M3's own existing, unchanged policy) — and never for more
than arrived.

| Scenario | Policy |
|---|---|
| invoice > received | **Blocked** (`OVER_INVOICING`) — the M6 task's own example: PO=100, received=80, invoice=100 must not silently become valid, and does not. |
| invoice > ordered, ≤ received | **Allowed** — matches M3's existing over-receipt-allowed policy; you received it, you owe for it. |
| received > ordered | Unchanged M3 behavior (already allowed, flagged) — not M6's concern beyond reusing `quantity_received`. |
| invoice < received | **Allowed** — an ordinary partial invoice; the PO item stays open for further invoicing later. |
| price variance | **Detected and posted automatically** to a dedicated account — see §7. |
| tax variance | **Not modeled** — see §8 for why. |

**The check runs at POST time, not CREATE time.** A DRAFT is a faithful
record of the paper invoice regardless of whether matching would
currently succeed (maybe the rest of the shipment arrives tomorrow);
posting is where the system actually commits to a matched, financially
real transaction, so that is where the ceiling is enforced. This also
means the same `client_transaction_id`/`(supplier_id, invoice_number)`
document can be entered once and matched later without re-entering it.

`get_invoice_matching_status(db, purchase_order_id)` is a read-only
helper (mirrors `app.modules.sales.service.get_return_eligibility`'s
shape) surfacing `quantity_ordered`/`received`/`invoiced`/`invoiceable`
per PO item — the API's matching-status endpoint and the frontend both
use this directly rather than recomputing it.

---

## 5. FIFO receipt-lot matching — the core technique

A `PurchaseOrderItem` can be received across **several `GoodsReceipt`s
at different costs** — the exact scenario that drives WAC (M3). So "the
cost of the units being invoiced" is not a single number when an
invoice's quantity spans more than one receipt lot. `_fifo_clearing_amount`
(`app/modules/ap/service.py`) resolves this by walking that item's
`GoodsReceiptItem` rows in receipt order (oldest first), **skipping**
whatever quantity earlier invoices against the same item already
consumed (tracked via the running `quantity_invoiced` cache — exactly
how `quantity_received` is already tracked), and pricing the
newly-invoiced quantity at the **actual recorded cost** of whichever
lot(s) it falls into.

This is what lets Purchase Clearing's GL balance always be traced back
to real receipt data (M6 task §5: "No orphan clearing balances") — the
clearing amount for any invoice is never invented, only ever the sum of
`(quantity × that lot's own recorded unit_cost)` for real
`GoodsReceiptItem` rows.

Proven by a dedicated test
(`test_fifo_matching_across_two_receipts_at_different_costs`): receive
10 @ 10.00 then 5 @ 20.00 against the same item; invoice 12 units at
15.00. Clearing correctly prices the first 10 units at 10.00 and the
next 2 at 20.00 (`10×10 + 2×20 = 140.00`), never a single blended number.

---

## 6. Purchase Clearing lifecycle & AP accounting

**`ACCOUNT_ACCOUNTS_PAYABLE` (2010)** is a new, separate liability
account — established only when a `PurchaseInvoice` is POSTED, reduced
only by a `SupplierPayment`. One shared control account for all
suppliers (not one GL account per supplier); the supplier dimension
lives on `PurchaseInvoice`/`SupplierPayment` rows (an AP subledger — §10),
mirroring how the store dimension already lives on `JournalEntry
.store_id` rather than on the account itself.

**`ACCOUNT_PURCHASE_CLEARING` (2000)** is renamed from "Purchase Clearing
(Accounts Payable)" to plain "Purchase Clearing" — it was *never* itself
the payable to the supplier, only the interim received-not-yet-invoiced
position, and now that a real AP account exists the old parenthetical is
actively misleading. Its posting rule is **completely unchanged**: still
credited only by `post_goods_receipt_journal` (M4, untouched), still
debited only by `post_purchase_return_journal` (M3/M4, untouched) — M6
adds a **third**, new debit source:

**`post_purchase_invoice_journal`** (Dr Purchase Clearing [+ Dr/Cr
Purchase Price Variance] [+ Dr Purchase Tax Expense] [+ Cr Purchase
Discounts] / Cr Accounts Payable):

```
clearing_amount           = Σ FIFO-matched receipt cost (§5)
price_variance_amount     = Σ (invoiced value − clearing_amount), signed
tax_total, discount_total = read directly off the invoice header
grand_total                = subtotal − discount_total + tax_total   (already stored)

Debits:  clearing_amount (if > 0)
       + price_variance_amount (if > 0, unfavorable)
       + tax_total (if > 0)
Credits: |price_variance_amount| (if < 0, favorable)
       + discount_total (if > 0)
       + grand_total (if > 0)
```

This balances **by construction**, not by two independent computations
happening to agree: `clearing_amount + price_variance_amount ≡ Σ
(quantity × unit_price)` = the invoice's own `subtotal` exactly, by how
`price_variance_amount` is defined (invoiced value minus the FIFO-matched
value) — so `clearing_amount + price_variance_amount + tax_total −
discount_total ≡ grand_total` always holds. Proven in
`test_post_purchase_invoice_clean_match_establishes_ap_and_clears_purchase_clearing`
and the fixed end-to-end scenario in `test_ap_e2e_scenario.py`.

**"Why does this Purchase Clearing balance exist?"** — always
answerable: `purchase_clearing_reconciliation` (§11) sums, per PO item
with `quantity_received > quantity_invoiced`, the exact FIFO-priced value
of the un-invoiced remainder — the same function `_fifo_clearing_amount`
underpins both the posting AND the reconciliation, so the two can never
silently disagree about what "the cost of these units" means.

---

## 7. Invoice variances

**Price variance** (invoice unit price ≠ the FIFO-matched receipt cost)
is **posted automatically** to a new, dedicated `ACCOUNT_PURCHASE_PRICE_VARIANCE`
(5100, EXPENSE/DEBIT-normal — but, like `ACCOUNT_SALES_DISCOUNTS`'s
contra-revenue precedent, routinely carries a *credit* balance for
favorable variances; one column, `normal_balance`, not a second
mechanism). This was judged safe to automate — unlike quantity variance
— because the formula is exact and unambiguous:
`invoiced_value − FIFO_matched_clearing_value`, both derived from
already-validated, already-stored numbers.

**Quantity variance** (invoice qty > received qty) is the one the M6
task explicitly flags as too risky to silently accept: it is **hard
blocked** (`OVER_INVOICING`, §4), not automatically posted anywhere —
there is no legitimate accounting entry for "the cost of units that were
never received." A visible exception (a clear `ConflictError`, not a
corrupted ledger) is exactly the "controlled exception state" the task's
own §11 asks for when automatic posting would be unsafe.

**Tax variance** is **not modeled** — see §8: since M6 does not compute
an "expected" tax from any rate, there is no expected-vs-actual
comparison to make. Deferred alongside recoverable-tax accounting
generally.

---

## 8. Purchase invoice tax and discounts

**Deliberately no automatic purchase-tax-rate calculation** — inventing
one "because sales tax exists" was explicitly the trap the M6 task
warned against, and there is no established "purchase tax rate" concept
anywhere in this schema to reuse safely. Instead: `tax_amount` is an
explicit, **stated** amount per invoice line (matching whatever the real
paper invoice says), tax-exclusive (unit prices are entered before tax).

**Recoverable vs. non-recoverable**: M6 treats invoice tax
**conservatively as non-recoverable** — expensed outright to
`ACCOUNT_PURCHASE_TAX_EXPENSE` (5200) when the invoice posts, never
capitalized into Inventory (which would corrupt WAC for a value
uninvolved in any inventory movement) and never claimed as a
tax-authority-recoverable asset without the reconciliation/remittance
workflow that would require. This is an honest, documented
simplification — a jurisdiction where this business reclaims input VAT
would need a real "Input Tax Recoverable" asset account and its own
netting-against-remittance workflow, which M6 does **not** build. The
architecture supports adding it later without a rewrite: swapping the
debit target in `_purchase_invoice_journal_lines` (§6) for tax is a
single, localized change, exactly the same shape as
`SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE`'s existing "one mapping, one
place" pattern.

**Discounts**: a supplier-granted, invoice-stated discount is a real,
distinct concept from price variance (an explicit negotiated reduction
vs. an unexplained per-unit price difference) — it gets its **own**
account, `ACCOUNT_PURCHASE_DISCOUNTS` (5150, the direct purchase-side
mirror of `ACCOUNT_SALES_DISCOUNTS`), credited when present.

**Freight/landed cost**: **deferred entirely** — no field, no account.
Landed-cost allocation (spreading freight proportionally across
lines/units) is a genuinely separate, larger feature requiring
allocation logic this milestone does not build; adding a freight field
without allocation logic would either silently misstate unit costs or do
nothing meaningful, so it is left out and documented rather than
half-built.

---

## 9. Supplier payments

`SupplierPayment` is applied to **exactly one `PurchaseInvoice`** — a
payment split across multiple invoices, or an unapplied supplier
credit/advance, is deferred (a real but separable feature; every
explicit M6 requirement is satisfiable with one-payment-one-invoice).

**Payment methods**: `SUPPLIER_PAYMENT_METHODS = (CASH, BANK_TRANSFER,
CHEQUE, OTHER)` — a **deliberately separate** set from sales'
`PAYMENT_METHODS` (`CASH, CARD, MOBILE_MONEY, BANK_TRANSFER, OTHER`).
CARD/MOBILE_MONEY describe how a *customer* paid *this* business, not
how this business settles a *supplier*; CHEQUE (an ordinary B2B vendor
settlement instrument) is added instead.

**"This is NOT a banking integration"** — recording a `BANK_TRANSFER`
supplier payment means the operator attests the transfer was made
through their own online banking/branch, exactly the same honesty
boundary `Payment.reference` already implies for sale payments. No code
anywhere claims to initiate, verify, or settle a real bank transfer.

**Overpayment is rejected outright** (`OVERPAYMENT`), never silently
accepted or capped — M6 does not implement a supplier-credit/advance
model, so there is nowhere honest to put an overpayment's excess.
Backstopped at the DB layer by `ck_purchase_invoices_amount_paid_bounds`
(`amount_paid ≥ 0 AND amount_paid ≤ grand_total`).

`record_supplier_payment` can only apply to an invoice in `POSTED` or
`PARTIALLY_PAID` status — a DRAFT invoice has no AP liability yet to
settle (`INVALID_INVOICE_STATE`).

---

## 10. Payment accounting

**`post_supplier_payment_journal`**: **Dr Accounts Payable / Cr
`<the real asset account the payment method maps to>`** —
`SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE` (`accounting/constants.py`), a
**deliberately separate** mapping from sales'
`PAYMENT_METHOD_ACCOUNT_CODE`:

| Method | Account | Why not reuse the sales-side account |
|---|---|---|
| CASH | Cash on Hand (1000) | Reused — physical cash decreasing is the same real asset regardless of direction. |
| BANK_TRANSFER / CHEQUE / OTHER | **Bank Account (Operating)** (1050, new) | The sales-side "Bank Transfer Clearing" account models money customers paid that hasn't settled into the business's bank yet (a receivable-in-transit) — the *opposite* economic direction from money actually leaving a real bank account. Reusing it would mix two unrelated flows into one balance and make it unauditable to a real financial reviewer. |

One amount (`supplier_payment.amount`), used for both journal lines —
balances by construction, can never fail to balance.

**Automated-source reversal protection**: `SUPPLIER_PAYMENT` (and
`PURCHASE_INVOICE`/`PURCHASE_INVOICE_VOID`) are members of
`AUTOMATED_SOURCE_TYPES`, so `reverse_journal_entry`'s existing
automated-source block (the M4 hardening audit's CRITICAL fix) already
covers them — reversing a payment or invoice journal directly through
the generic accounting-only mechanism is refused
(`OPERATIONAL_REVERSAL_REQUIRED`), proven live (not just by tuple
membership) by a mutation test
(`test_mutation_removing_supplier_payment_from_automated_sources_allows_bare_journal_reversal`).

---

## 11. AP subledger, aging, and reconciliation

`SupplierApSummary` (`get_supplier_ap_summary`): `total_owed` (Σ
outstanding `POSTED`/`PARTIALLY_PAID` invoice balances), `total_overdue`
(the portion past `due_date`), `total_current` (the rest),
`total_paid` (lifetime, all statuses), `outstanding_purchase_clearing`
(FIFO-priced, per this supplier's own PO items — §6). Every figure is
derived directly from `PurchaseInvoice`/`SupplierPayment` rows — never a
separately-maintained running total, so it always reconciles to what the
rows actually say.

`ap_aging`: standard current/1-30/31-60/61-90/90+ day buckets against
`due_date`, per supplier — a well-known, standard accounting convention,
not invented.

`ap_reconciliation`: compares the Accounts Payable **GL control account**
balance (from `accounting_service.trial_balance`) against the **AP
subledger total** (Σ outstanding invoice balances). Expected to match
exactly, for the same reason M4's `inventory_reconciliation` matches
exactly: every `SupplierPayment` reduces both the invoice's own
`amount_paid` and posts the identical amount to the AP account in the
same transaction, and every posted invoice's `grand_total` is exactly
what gets credited to AP.

`purchase_clearing_reconciliation`: compares the Purchase Clearing GL
balance against Σ FIFO-priced outstanding (received, not yet invoiced)
value across every PO item (§6).

Both reconciliation functions were proven to be **real, working checks**
— not decorative always-zero reports — by a mutation test that directly
corrupts a posted invoice's stored `grand_total` via raw SQL (bypassing
every service-layer guard) and confirms the discrepancy becomes nonzero
and points at exactly the corrupted amount
(`test_mutation_ap_reconciliation_detects_a_corrupted_invoice_total`).

Existing M4 `inventory_reconciliation`/`profit_and_loss` (COGS) are
**completely untouched** by M6 — new AP accounts (Bank Account, Accounts
Payable, Purchase Price Variance, Purchase Discounts, Purchase Tax
Expense) never appear in COGS or Inventory's own balance calculations,
proven directly in the end-to-end scenario test.

---

## 12. Void

`void_purchase_invoice` handles two genuinely different cases under one
function:

- **DRAFT → VOIDED**: a pure status flip. Nothing financial ever
  happened, so nothing needs reversing.
- **POSTED → VOIDED** (only while `amount_paid == 0`): reverses
  `quantity_invoiced` on every affected PO item, and posts an exact
  compensating `PURCHASE_INVOICE_VOID` journal entry. Crucially, this
  reversal **reads the original posting's own journal lines back**
  (`_extract_clearing_and_variance_from_journal`) rather than
  recomputing `clearing_amount`/`price_variance_amount` via FIFO again —
  recomputing would be **unsafe** if other invoices against the same PO
  items posted in between (their consumption would have shifted the FIFO
  window this invoice originally occupied). Reading the actual posted
  numbers back and mirroring them (`_purchase_invoice_journal_lines(...,
  reverse=True)`) guarantees the void is provably the algebraic opposite
  of what was actually posted, regardless of what happened in between.
- **PARTIALLY_PAID / PAID → rejected** (`INVOICE_HAS_PAYMENTS`): M6 does
  not implement unwinding a recorded payment as part of a void. An
  invoice with a payment against it needs a real credit-note/refund
  workflow to correct — deferred (see §15) rather than partially built.

`void_purchase_invoice` is idempotent (voiding an already-VOIDED invoice
is a no-op).

---

## 13. Credit notes

**Deferred entirely.** The required end-to-end scenario (M6 task §29)
and the mutation-testing list (§27) both omit credit notes; the task's
own instruction — "do not implement an incomplete credit-note workflow
just to increase feature count" — applies directly. A future milestone
implementing them has a natural landing spot: they would reduce AP the
same way a `SupplierPayment` does (Dr Accounts Payable), just without an
actual cash/bank credit, and would need their own idempotency/audit
treatment mirroring `SupplierPayment`'s exactly.

---

## 14. RBAC

Four new, minimal permissions — not a reuse of `accounting.reverse`/
`accounting.post` — reflecting that posting (a financial commitment) and
paying (moving cash) are meaningfully heavier than drafting an invoice
from paperwork:

| Permission | Admin | Manager | Inventory Clerk | Auditor | Cashier |
|---|---|---|---|---|---|
| `ap.read` | ✓ | ✓ | ✓ | ✓ | |
| `ap.write` (create/void a DRAFT) | ✓ | ✓ | ✓ | | |
| `ap.post` (post or void a POSTED invoice) | ✓ | ✓ | | | |
| `ap.pay` | ✓ | ✓ | | | |

Inventory Clerk can draft/match an invoice against a receipt
(purchasing-adjacent data entry) but cannot commit it financially —
matching the M6 task's explicit "Do not give Cashiers AP payment/posting
privileges," extended to the role whose day-to-day work is genuinely
adjacent to this one. Cashier gets nothing AP-related at all.

Seeded by the M6 migration using the same idempotent `ON CONFLICT DO
UPDATE`/`DO NOTHING` seed pattern M4/M5 established.

---

## 15. Known limitations / deferred functionality

- **No external payment/banking integration** — by design (§9), not an
  oversight.
- **One invoice ↔ one purchase order** — an invoice spanning multiple
  POs is deferred; every explicit M6 requirement is satisfiable without
  it.
- **One payment ↔ one invoice** — payment allocation across multiple
  invoices, or an unapplied supplier credit/advance, is deferred (§9).
- **No multi-currency** — no currency field anywhere in this system;
  adding one without FX-rate handling would misstate accounting (§2).
- **No recoverable input tax / VAT-reclaim workflow** — invoice tax is
  conservatively non-recoverable (§8); the architecture supports adding
  recoverability later without a rewrite.
- **No freight/landed-cost allocation** — deferred entirely (§8).
- **No credit notes** (§13) — deferred entirely, not partially built.
- **Voiding a paid/partially-paid invoice is not supported** (§12) —
  needs a future credit-note/refund workflow.
- **Draft invoices cannot be edited or deleted** — an operator corrects
  a mistake by leaving the DRAFT unposted (or voiding it) and creating a
  fresh one (§3).
- **Reporting-path N+1**: `ap_reconciliation`,
  `purchase_clearing_reconciliation`, and `get_supplier_ap_summary` each
  call `_fifo_clearing_amount` once per PO item with an outstanding
  balance — one extra query per such item. Accepted, documented debt:
  these are periodic reporting functions, not hot transactional paths,
  and the same per-item-loop tradeoff already exists elsewhere in this
  codebase (e.g. `receive_goods`'s per-product locking loop). The one
  genuine N+1 found in a **creation** path
  (`create_purchase_invoice`'s per-line `PurchaseOrderItem`/`Product`
  lookups) was fixed — batched into two `IN(...)` queries, mirroring the
  identical fix M5 applied to `create_sale_return`.

See `docs/M6_HARDENING_AUDIT.md` for the full adversarial/mutation-testing
evidence, the real migration-safety finding from testing against
populated data, and the final verdict.
