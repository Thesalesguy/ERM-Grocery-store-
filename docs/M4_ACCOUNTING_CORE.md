# M4 — Accounting Core, Double-Entry Journal Engine & Financial Reporting Foundation

This document records the M4 milestone's design decisions and explains
the accounting module (`backend/app/modules/accounting/*`) and its
integration into the sales/purchasing/inventory modules built in M1–M3.
Read alongside `docs/M1_DATABASE_DESIGN.md`, `docs/M2_AUTH_AND_POS.md`,
`docs/M2_HARDENING_AUDIT.md`, and `docs/M3_PURCHASING_RECEIVING_WAC.md`,
which remain authoritative for anything this document doesn't override.

M4's objective: establish a real, auditable, double-entry accounting
foundation — a Chart of Accounts and an immutable journal — from which
financial reports are *derived*, not computed ad hoc. This supersedes the
original blueprint's Section G design (a "read-mostly aggregation
service" over `sales`/`purchase_orders`/`inventory_movements`, scheduled
for M6) — a deliberate, explicitly-directed upgrade for this milestone,
not an unrequested redesign.

---

## 1. Pre-implementation architecture review

Traced the actual data flow — SALE → inventory decrease → COGS → payment
→ revenue, and PURCHASE RECEIPT → inventory increase → valuation →
supplier obligation — through the real M1–M3 code (not assumed) before
writing anything. Two pre-existing gaps were found and are reported here
per the task's "stop and report" instruction, rather than silently
designed around:

- **`SaleReturn`/`SaleReturnItem` have existed as ORM models since M1,
  but no service function or API endpoint was ever built for them** in
  M2 or M3. There is no `create_sale_return`, and nothing in
  `sales/service.py` or the sales endpoints creates one.
- **There is likewise no sale-void/refund service.** `Sale.status`'s
  enum includes `VOIDED`/`REFUNDED`/`PARTIALLY_REFUNDED`, but nothing
  ever transitions a sale into those states.

M4 does **not** build these two operational workflows — doing so would
be a new M2/M3-scale feature (RBAC, store-scoping, concurrency,
idempotency, refund calculation, restock logic) under an accounting
milestone, well beyond "establish the accounting engine and integrate it
into existing operational transactions." Instead: `SALE_RETURN` is
included as a supported `JournalEntry.source_type` value so the schema
is ready for it, and the gap is documented here rather than silently
worked around, per the task's explicit "document the boundary" allowance
in its sale-returns section.

Everything else — WAC (`inventory/wac.py`), the M2 sale-finalization
locking/idempotency pattern, and the M3 goods-receiving transaction — was
extended, not replaced.

---

## 2. Chart of Accounts

**Decision: global reference data, code-seeded by migration, not
store-scoped and not creatable through the API in M4** — following the
precedent of `Supplier`/`ProductCategory`/`TaxRate` (M2/M3 §2). The store
dimension for accounting lives on `JournalEntry.store_id`, not on the
account — this is architecture choice **B** from the task's own list
("shared Chart of Accounts with store dimensions"), chosen because it
avoids duplicating account configuration per store while still supporting
store-level P&L (filter journal lines by `journal_entries.store_id`),
consolidated reporting (omit the filter), and store-level reconciliation.

**Eleven accounts, each justified by an actual posting rule** (§6–9
below) — not the blueprint's full example list. No Accounts Receivable
(nothing in M4 sells on credit), no populated Owner Equity/Retained
Earnings (no period-closing workflow exists yet — see §16):

| Code | Name | Type | Normal balance |
|---|---|---|---|
| 1000 | Cash on Hand | ASSET | DEBIT |
| 1010 | Card Clearing | ASSET | DEBIT |
| 1020 | Mobile Money Clearing | ASSET | DEBIT |
| 1030 | Bank Transfer Clearing | ASSET | DEBIT |
| 1040 | Other Payment Clearing | ASSET | DEBIT |
| 1500 | Inventory | ASSET | DEBIT |
| 2000 | Purchase Clearing (Accounts Payable) | LIABILITY | CREDIT |
| 2100 | Tax Payable | LIABILITY | CREDIT |
| 4000 | Sales Revenue | REVENUE | CREDIT |
| 4100 | Sales Discounts | REVENUE | DEBIT (contra) |
| 4900 | Inventory Adjustment Gain | REVENUE | CREDIT |
| 5000 | Cost of Goods Sold | EXPENSE | DEBIT |
| 5900 | Inventory Shrinkage Expense | EXPENSE | DEBIT |

**Contra accounts**: represented by *normal balance*, not a separate
mechanism or account type. Sales Discounts is `account_type = REVENUE`
(it belongs in the revenue section of a report) but `normal_balance =
DEBIT` (a debit to it reduces net revenue) — one explicit column, no
special-cased contra type.

**No hardcoded account IDs anywhere in application code.** Every posting
function refers to accounts by the stable string codes in
`accounting/constants.py` (`ACCOUNT_SALES_REVENUE = "4000"`, etc.),
resolved to a row via one `SELECT ... WHERE code IN (...)` per posting
call — never a raw integer.

---

## 3. Journal entry model

`JournalEntry` + `JournalLine`. Load-bearing decisions:

- **`JournalLine.debit`/`credit` are `Numeric(14, 6)`**, not the app's
  usual 2-decimal money precision. This reuses the WAC quantum
  (`Decimal("0.000001")`, established in `inventory/service.py`) for
  inventory-valuation lines, instead of inventing a second, incompatible
  rounding rule — see §11 for why this makes reconciliation exact rather
  than approximately-exact-within-a-tolerance. Money-side amounts
  (already-exact 2dp Sale values) round-trip losslessly into 6dp.
- **Exactly one of debit/credit is positive per line** — DB `CHECK
  ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))`, not a
  single signed-amount column. Chosen because it matches how a real
  ledger line is written down (a debit column and a credit column, one
  of them blank) and makes the DB reject a line that tries to be both or
  neither.
- **No mutable `status` column.** `UPDATE`/`DELETE` are revoked from the
  application's runtime role (`erp_app`) on both `journal_entries` and
  `journal_lines` in the M4 migration — extending the exact privilege
  pattern M1 established for `audit_logs`/`inventory_movements`, per
  that migration's own documented instruction that a future append-only
  table must opt out of the default broad grant in the same migration
  that creates it. A `POSTED → REVERSED` status column that can
  structurally never be updated would be a lie waiting to happen, so
  there isn't one. `entry_type` (`'STANDARD'` or `'REVERSAL'`) is fixed
  forever at insert; "was this entry reversed" is a **derived** fact —
  does a `REVERSAL`-type entry exist whose `reversal_of_id` points at it
  — never a stored one. This is a deliberate deviation from the task's
  example `DRAFT/POSTED/REVERSED` lifecycle, documented here per its own
  "if you choose a different lifecycle, document why" allowance.
- **Balance, non-zero, and non-empty are enforced by a real Postgres
  trigger**, not just Python:

  ```sql
  CREATE CONSTRAINT TRIGGER trg_journal_lines_balance
  AFTER INSERT OR UPDATE ON journal_lines
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION check_journal_entry_balance();
  ```

  Deferred to end-of-transaction, so it validates the *final* state of a
  multi-line entry rather than rejecting the first line before the
  second is inserted. Deliberately **not** wired to `DELETE`: `erp_app`
  can never delete a line anyway (privilege revoked, above), so the only
  code path that ever deletes a line is superuser/DBA-level manual
  intervention (test cleanup, an exceptional correction) — checking
  `DELETE` too would reject the ordinary "delete the lines, then delete
  the now-orphaned entry" sequence at its first step, since the entry
  briefly has zero lines before it is itself removed. Verified directly
  against `erp_app`: an unbalanced entry is rejected at commit with
  `ERROR: Journal entry N is unbalanced: debits X <> credits Y`; a
  balanced one commits; `UPDATE`/`DELETE` both fail with `permission
  denied for table journal_lines`/`journal_entries`.

- **Source uniqueness backstop**: a partial unique index on
  `(source_type, source_id) WHERE source_id IS NOT NULL AND entry_type =
  'STANDARD'` — see §7.

---

## 4. Posting is inline, in the same transaction

Every `post_*_journal` function (`accounting/service.py`) is called
**inline**, inside the same operational service function that creates
the sale/receipt/return/adjustment it accounts for — `finalize_sale`,
`receive_goods`, `create_purchase_return`, `create_stock_adjustment` —
immediately before that function's own final `db.flush()`. None of them
commit. This is the entire mechanism behind M4 task Section 19's
atomicity requirement: there is no separate "accounting service call"
that could succeed or fail independently of the operational write,
because it's the *same* uncommitted transaction on the *same* session.
If posting raises, everything already flushed in that function (sale,
items, payments, inventory movements, audit row) is discarded when the
route handler's session closes without ever calling `db.commit()` — the
same "nothing commits until the end" convention already established for
`finalize_sale`/`receive_goods` in M2/M3.

**The one governing rule applied in every posting function**: *never
independently compute two sides of a pair that must match.* A value that
has to appear as both a debit and a credit (COGS/Inventory on a sale,
Inventory/Purchase Clearing on a receipt) is computed exactly once and
reused for both lines — the entry balances by construction, not because
two separately-derived numbers happen to agree. The DB trigger is the
backstop for a coding mistake, not the primary correctness mechanism.

---

## 5. Locking

No new locks were introduced for posting itself — it runs inside the
already-locked scope of `finalize_sale`/`receive_goods`/etc. (product
rows, PO rows — see M2/M3 docs), and only ever inserts new rows.

**Reversal** needed a lock to serialize two concurrent reversal attempts
on the *same* entry. `SELECT ... FOR UPDATE` on `journal_entries` was
tried first and rejected: Postgres requires `UPDATE` privilege on a
table to take a row lock via `FOR UPDATE`, which `erp_app` deliberately
does not have (§3) — verified directly (`SELECT ... FOR UPDATE` as
`erp_app` against a scratch table with `UPDATE` revoked raises
`permission denied`, not just a lock). Instead, reversal uses
`pg_advisory_xact_lock(journal_entry_id)` — an application-level lock
keyed by the entry's ID, requiring no table privileges, held only for
the transaction and released automatically at commit/rollback. Verified
under two real concurrent reversal requests against the same entry: both
serialize on the lock, and exactly one `REVERSAL` entry is ever created
(the second request's idempotent fast path returns the first's result).

---

## 6. Sales accounting

For a sale, the full derivation (worked out once, algebraically, before
writing code — see the module docstring's rule above):

**Debits**: one line per `Payment` (by method → clearing/cash account, §8)
+ Sales Discounts (if `discount_total > 0`) + COGS (if any line has a
nonzero cost).
**Credits**: Cash on Hand for change given (if `change_due > 0`) + Sales
Revenue (gross `subtotal`, if `> 0`) + Tax Payable (if `tax_total > 0`) +
Inventory (same value as COGS).

Algebraic proof this always balances: `Σ payment.amount = total_paid`.
`change_due = total_paid - grand_total`. `grand_total = subtotal -
discount_total + tax_total`. So:

```
debits  = total_paid + discount_total
credits = change_due + subtotal + tax_total
        = (total_paid - grand_total) + subtotal + tax_total
        = total_paid - (subtotal - discount_total + tax_total) + subtotal + tax_total
        = total_paid + discount_total
```

`debits == credits`, independent of any rounding — because every value
used is already an exact, previously-validated `Sale`/`Payment` column,
never re-derived. COGS/Inventory is a separate pair, computed once from
`Σ(quantity × unit_cost_at_sale)` per line (the exact same values already
used to post each `SALE` inventory movement) and reused for both sides.

**Gross vs. net**: Sales Revenue is credited at the gross `subtotal`;
discounts are a separate contra-revenue debit — a report can show gross
sales, discounts, and net sales as three distinct numbers, not one
pre-netted figure.

**Zero-price sale**: if `subtotal == 0` (every line free), the Sales
Revenue credit line is **skipped entirely** rather than crediting `$0`
(which would violate the "exactly one side positive" line constraint) —
the entry still balances using only the payment/change lines. Same
treatment for COGS/Inventory when total cost is zero.

**Payment methods**: `PAYMENT_METHOD_ACCOUNT_CODE` maps each of the
five `Sale.PAYMENT_METHODS` (already distinguished by M1/M2) to its own
clearing/cash account — never a generic "Cash" posting regardless of
method (task Section 18).

**COGS is frozen, proven under a later WAC change**: a dedicated test
(`test_sale_cogs_journal_amount_is_immutable_after_a_later_wac_change`)
sells at WAC 5.00, records the journal's COGS line, then receives 50
units at 500.00 (driving the product's WAC far above 5.00), and asserts
the *original* journal line is byte-for-byte unchanged. This works
because the COGS amount comes from `computed_lines` — `SaleItem`'s
already-frozen `unit_cost_at_sale` (M1's BR-2) — never from
`product.current_cost` at report time.

---

## 7. Purchase receiving accounting

`Dr Inventory / Cr Purchase Clearing` for the value actually received —
`Σ(quantity_received × unit_cost)` per receipt line, computed once and
used for both sides. This amount is deliberately the *same* value already
used to post each line's `PURCHASE_RECEIPT` inventory movement (§11).

**Accounts Payable treatment** (task Section 9's explicit question):
"Purchase Clearing" is a **real liability**, not a fabricated one — goods
were received and the store owes the supplier for them, which is
economically true regardless of whether a per-supplier open-balance
subledger exists. It is named "clearing" only because M3's schema has no
`supplier_invoices`/`supplier_payments` pair to track *which* supplier
invoice this aggregate liability nets against — the blueprint itself
(Section F) flagged this as future schema room, not built yet. This
account is the honest interim treatment: it never claims a specific
supplier has been paid or that a specific invoice exists, only that
inventory value increased and a liability increased by the same amount.
The extension point for real AP is a future `supplier_invoices` table
whose payments debit Purchase Clearing.

**Free goods (unit_cost 0)**: `post_goods_receipt_journal` returns `None`
and posts nothing if the total received value is zero — a supplier
giving stock away for free is an explicitly supported M3 scenario, and
the receipt must still succeed operationally; it simply has no dollar
value to record (a zero-total entry would also be rejected by the DB
trigger, §3). This was caught by an actual test failure during
development (`test_concurrent_sale_and_stock_adjustment_serialize_correctly`,
a pre-existing M2 concurrency test using a zero-cost product) — a real
regression, fixed, not a hypothetical.

---

## 8. Purchase returns

`Dr Purchase Clearing / Cr Inventory`, using the *same* `unit_cost` value
M3's `create_purchase_return` already uses for the `PURCHASE_RETURN`
inventory movement (the product's current WAC at return time) — the
journal never uses a different valuation than the operational movement
it accounts for. **M3's documented cost-basis limitation (no per-lot
receipt tracking; a return is valued at current WAC, not the original
receipt cost of the specific units) applies unchanged here** — this
journal is a faithful mirror of the operational movement, not an
independent, more-precise valuation.

---

## 9. Stock adjustment accounting

Positive `quantity_delta` (found more stock than recorded): `Dr Inventory
/ Cr Inventory Adjustment Gain`. Negative: `Dr Inventory Shrinkage
Expense / Cr Inventory`. Amount is `abs(quantity_delta) × unit_cost`,
using the *same* `product.current_cost` value already passed to
`record_movement` for the `STOCK_ADJUSTMENT_IN`/`_OUT` movement.

**Zero-cost product**: same treatment as §7 — no journal entry, the
adjustment still happens operationally (correcting the counted quantity
of a product that has always been zero-cost is a real scenario).

---

## 10. Sale returns and sale voids — explicitly out of scope

Per §1's discovered gap: no operational sale-return or sale-void/refund
workflow exists, so there is nothing for M4 to post accounting for.
`SALE_RETURN` is a supported `source_type` value, ready for the day that
workflow is built (a future `post_sale_return_journal` following the
exact same pattern as §8's purchase return — reverse the proportional
revenue/tax/discount, reverse COGS by restocking value, refund the
payment method's account).

---

## 11. Inventory GL reconciliation — the central invariant, proven exact

**Claim**: `Inventory` account GL balance (Σ debits − Σ credits, filtered
by store) always equals `Σ_products(current_qty_on_hand × current_cost)`
for that store, to the ledger quantum (`0.000001`).

**Why it's true, not just usually true**: every posting function derives
its Inventory-account amount from the *identical* `(quantity, unit_cost)`
pairs already passed to `inventory_service.record_movement()` for that
same operational event — never a second, independently-computed
valuation. Since Weighted Average Cost is *defined* such that
`current_qty_on_hand × current_cost` always equals the cumulative sum of
every movement's value (added at receipt cost, removed at the WAC in
effect at removal time — the entire point of a WAC system), and the
accounting journal's Inventory line is built from those same movement
values, the two are the same quantity computed two ways. The only
residual drift is the rounding boundary itself: WAC is stored at 6dp
(`Decimal("0.000001")`, `inventory/service.py`), and journal amounts use
the *same* 6dp quantum rather than the app's usual 2dp money precision —
so there is no second, incompatible rounding rule to drift against.

**Proven, not asserted**: `test_inventory_reconciliation_matches_after_receipt_and_sale`
and the API-level `test_inventory_reconciliation_report_endpoint` build
stock through a real receipt, sell some of it, and assert `discrepancy
== Decimal("0.000000")` — exact equality, not a fuzzy tolerance.

**One documented exception, found live**: reversing a *SALE's* journal
entry (§3) is an **accounting-only** correction — it does not undo the
operational sale or restock inventory (because no sale-void workflow
exists to call, per §10). Reversing a sale's journal without also voiding
the sale operationally therefore *does* produce a real, correctly-detected
reconciliation discrepancy — proven live against a running instance: a
$50 sale (COGS $20) was posted, reconciliation showed `discrepancy:
"0.000000"`; after reversing only its journal entry, reconciliation
correctly reported `discrepancy: "20.000000"` (the COGS/Inventory credit
that was reversed with no matching stock change). This is the
reconciliation report doing exactly its job — detecting a real
accounting/operational divergence — not a bug, and it is the direct
consequence of §10's deferred scope: reversal is a foundation for
financial correction, not (yet) a full operational undo.

**A pre-existing test-fixture caveat, also discovered**: a product whose
`current_qty_on_hand` is seeded directly (bypassing `record_movement`
entirely, as some older RBAC-only test fixtures do for convenience) has
no journal history behind that seeded quantity and will show a false
discrepancy — this is correct behavior (there genuinely was no
accounting event for that quantity), not a reconciliation bug. Tests that
assert exact reconciliation build stock through a real receipt instead.

---

## 12. COGS reconciliation

Same mechanism as §11, restated for the expense side: `Cost of Goods
Sold`'s debit total for a period always equals `Σ` of every `SaleItem`'s
`quantity × unit_cost_at_sale` in that period, because that is the exact
value used to build the journal line. §6's frozen-COGS test proves this
survives a later WAC change; nothing in the reporting layer (§14)
recomputes COGS from current WAC.

---

## 13. P&L implementation

`profit_and_loss()` derives every figure from `trial_balance()`, which
itself sums `journal_lines`/`journal_entries` only — never from `sales`/
`sale_items` directly (task Section 26's explicit requirement):

```
Net Sales           = Sales Revenue (net credit) − Sales Discounts (net debit)
Cost of Goods Sold  = COGS (net debit)
Gross Profit        = Net Sales − COGS
Other Income        = Inventory Adjustment Gain (net credit)
Operating Expenses  = Inventory Shrinkage Expense (net debit)
Net Income          = Gross Profit + Other Income − Operating Expenses
```

No fabricated expense lines — `Operating Expenses` is exactly the one
real expense account that exists beyond COGS. A future payroll/rent/
utilities milestone adds accounts and this formula's `Operating Expenses`
term grows to include them; nothing here hardcodes "the" expense list.

---

## 14. Store accounting model

Chosen: **B** — shared Chart of Accounts, store dimension on
`JournalEntry.store_id` (§2). Every report accepts an optional `store_id`
filter (omit for consolidated, cross-store); every list/detail endpoint
enforces the same store-scoping pattern as M2/M3
(`scoped_store_filter`/`enforce_store_access`, both at the route layer
and duplicated in the service layer for direct-call testability — the
established `_enforce_store_access` pattern). Verified both negative
(cross-store read → 404, cross-store reverse → 403, no leak/mutation) and
positive (an Admin's cross-store access still works) live and in tests.

---

## 15. RBAC

Four new permissions: `accounting.read`, `accounting.reverse`,
`accounting.post` (reserved — no endpoint uses it; see §17),
`accounting.admin` (reserved — no chart-of-accounts admin endpoint
exists in M4). Role grants: Admin gets all four (via its existing
"every permission" rule); Manager gets `read` + `reverse`; Auditor gets
`read` only; Cashier and Inventory Clerk get none — a cashier can never
see or touch the ledger. Seeded by a **new** migration
(`8df037a45976`), not by editing the M2 seed migration
(`e6180fca2ee0`), because that migration already ran against any
existing database — new permission codes after that point require a new
migration inserting only the new rows (documented in the migration
itself, including the `ON CONFLICT` handling needed because the M2 seed
migration reads `ALL_PERMISSIONS` *live* at migration-run time, so a
from-scratch replay of it already includes M4's new codes).

---

## 16. Accounting periods

**Not built in M4, deliberately** — the task explicitly warns against
over-engineering fiscal-year functionality not yet needed. `posting_date`
exists on every entry (distinct from `created_at`) and every report
accepts a `date_from`/`date_to` range, so period-style reporting already
works without a "closed period" concept. What's deferred: there is no
mechanism to *lock* a period against further posting, and no
period-closing workflow that zeroes Revenue/Expense into a Retained
Earnings equity account (which is also why no Retained Earnings account
is seeded — nothing would ever post to it). The extension point is
clean: adding a `closed_periods` table and a check at posting time
("is `posting_date` inside a closed period?") requires no change to the
posting functions' own logic, only one more validation at their entry
point.

---

## 17. Tax

Sales tax **is** modeled end-to-end and posted: `Tax Payable` (2100)
receives the exact `Sale.tax_total`, itself computed by `finalize_sale`
from the existing effective-dated `TaxRate` architecture (M2) — never a
hardcoded rate. Purchase-side tax is **not** modeled, matching M3's own
documented decision (`purchase_order_items` has no tax column) — M4 does
not invent one. `posting_date` uses the date part of `Sale.completed_at`/
`GoodsReceipt.received_date`/etc., all already UTC per M2 hardening's
established policy.

---

## 18. Payments

`PAYMENT_METHOD_ACCOUNT_CODE` distinguishes all five methods the system
already supports (CASH/CARD/MOBILE_MONEY/BANK_TRANSFER/OTHER) into five
separate asset accounts — never one generic "Cash" posting. Split tender
posts one debit line per `Payment` row. Cash overpayment (change) is a
separate credit line to Cash on Hand, proven to keep the entry balanced
algebraically (§6). Underpayment/invalid payment never reaches posting —
`finalize_sale` rejects `INSUFFICIENT_PAYMENT`/`OVERPAYMENT_NOT_ALLOWED`
before creating the `Sale` row at all (unchanged M2 behavior), so no
partial/invalid payment can ever produce a journal entry.

---

## 19. Atomicity

Covered fully in §4. Restated as the specific claim the task asked for:
there is no code path where a `Sale` exists without its journal, or a
journal exists without its `Sale` — they are written in the same
Python function call sequence, on the same session, and neither commits
independently. Verified by the general pattern already established for
`finalize_sale`/`receive_goods` (M2/M3): an exception anywhere before the
route handler's single `db.commit()` discards everything flushed so far,
because nothing was ever committed to discard *from*.

---

## 20. Concurrency (repeated 5x each, real PostgreSQL connections)

- **Two concurrent sales against the same product** each post their own
  distinct, individually-balanced journal entry — no lost entry, no
  merged/duplicate entry.
- **Two concurrent goods receipts against the same PO** each post their
  own distinct, balanced journal entry.
- **Two concurrent reversal requests against the same journal entry**
  serialize on the advisory lock (§5) and produce exactly one reversal
  entry — both requests return the same reversal ID.
- **The deferred balance trigger and the ledger-table privilege
  revocation**, verified with real (non-savepoint) commits against
  `erp_app` directly — see §3.

All in `tests/test_accounting_concurrency.py`, using independent
`SessionLocal()` connections per thread (the same real-commit pattern
`tests/test_concurrency.py` established for M2, since the `db` test
fixture's SAVEPOINT-based commit never actually fires a `DEFERRED`
constraint trigger — only a real `COMMIT` does).

---

## 21. Idempotency

Inherited for free from the operational layer for the common case: a
retried `finalize_sale`/`receive_goods` call returns its existing row via
its own `client_transaction_id` fast path *before* posting code is ever
reached a second time — proven by `test_retrying_a_sale_...`/
`test_retrying_a_goods_receipt_...`, asserting exactly one journal entry
and the correct (not doubled) resulting stock/COGS after two identical
requests.

**A second, independent backstop at the accounting layer itself**
(task Section 21's explicit ask: "if the accounting service receives the
same source transaction twice, the database must prevent duplicate
posting"): the partial unique index on `(source_type, source_id)` (§3).
`test_posting_twice_for_the_same_source_id_is_rejected_at_the_db_level`
calls `_post_journal` a second time for an already-journaled sale
directly (simulating a hypothetical future bug that bypasses the
operational fast path) and asserts it fails with `IntegrityError`, DB
constraint, not application logic — and that exactly one journal entry
survives afterward.

---

## 22. Reversal

Covered in §3/§5. `POST /accounting/journals/{id}/reverse` (gated by
`accounting.reverse`) creates a new `REVERSAL` entry with every line's
debit/credit swapped, referencing the original via `reversal_of_id`,
never mutating the original (structurally impossible — §3). Idempotent:
reversing an already-reversed entry returns the existing reversal.
Reversing a `REVERSAL` entry itself is rejected
(`CANNOT_REVERSE_REVERSAL`) — a reversal cannot cascade.

**No automated trigger calls this yet** (§10's gap — nothing voids a
sale or receipt operationally in M4), so in practice it is a manually-
triggered correction tool for now. The foundation is real and tested
end-to-end (service function, endpoint, RBAC, store-scoping,
concurrency), matching the task's explicit "the architecture must still
support them cleanly" instruction even with full operational
reversal/void workflows deferred.

---

## 23. Audit

`JOURNAL_ENTRY_REVERSED` is logged (with the reversal's entry ID and
reason) through the existing `audit_service.log_event` — same
append-only table, same DB-level immutability (`UPDATE`/`DELETE` revoked
from `erp_app` since M1, unchanged by M4). No new code path writes to or
deletes from `audit_logs`. Account/journal creation itself is not
separately audited beyond the journal row's own `created_by`/
`created_at` columns and its immutability — the journal *is* the audit
trail for a posting event, matching how `inventory_movements` already
serves double duty in M1/M2.

---

## 24. Known limitations

- No sale-return or sale-void/refund operational workflow (§1, §10) —
  `SALE_RETURN` is schema-ready, not wired up.
- Reversal is accounting-only; it does not undo the operational
  transaction it corrects, which can produce a genuine, correctly-
  detected inventory-reconciliation discrepancy if used against a sale
  without a matching (currently nonexistent) operational void (§11).
- No manual journal-posting endpoint (`accounting.post` is reserved,
  unused) — by design, not an oversight (task Section 27).
- No chart-of-accounts admin endpoint (`accounting.admin` is reserved,
  unused) — the eleven accounts are fixed, code-seeded reference data.
- No accounting-period closing/locking mechanism (§16) — deferred,
  extension point documented.
- Purchase-side tax remains unmodeled, matching M3.
- Purchase-return cost basis remains current-WAC-at-return-time, not
  per-lot original receipt cost — an M3 limitation this milestone
  inherits unchanged (§8).

## 25. Deferred functionality

Sale-return/void accounting integration (once the operational workflow
exists), accounting-period closing to Retained Earnings, per-supplier
Accounts Payable subledger, purchase-side tax, manual journal posting UI,
Chart of Accounts admin UI, payroll/operating-expense account expansion.

---

## 26. Testing strategy

220 backend tests pass (169 pre-M4 + 51 new): chart-of-accounts seeding,
every posting function's balance and expected-account assertions
(including the zero-value skip cases found via a real pre-existing test
failure), COGS immutability under a later WAC change, reversal
(including idempotent re-reversal and the cannot-reverse-a-reversal
rule), trial balance/P&L/inventory-reconciliation correctness, RBAC and
multi-store isolation (positive and negative) via real HTTP requests,
concurrency (5x each, real connections), and idempotency (both inherited
and the accounting layer's own independent backstop). 22 frontend tests
pass (19 pre-M4 + 3 new) covering the journal list/detail/reversal flow
and the trial-balance view. A full live HTTP smoke test against a running
instance (login → product → supplier → PO → receive → sell → verify
trial balance balances, P&L is correct, reconciliation is exact →
reverse → verify the resulting discrepancy is correctly detected)
confirmed every figure by hand-calculation, not just automated
assertions.
