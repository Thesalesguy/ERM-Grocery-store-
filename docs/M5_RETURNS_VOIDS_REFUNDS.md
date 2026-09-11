# M5 — Operational Returns, Voids, Refunds & Accounting Reversal Integration

This document records the M5 milestone's design decisions and explains
the sale-return/void workflow (`backend/app/modules/sales/service.py`'s
`create_sale_return`/`void_sale`, `app/modules/accounting/service.py`'s
`post_sale_return_journal`) and its integration into the M1–M4 foundation.
Read alongside `docs/M1_DATABASE_DESIGN.md`, `docs/M2_HARDENING_AUDIT.md`,
`docs/M3_PURCHASING_RECEIVING_WAC.md`, `docs/M4_ACCOUNTING_CORE.md`, and
`docs/M4_HARDENING_AUDIT.md`, which remain authoritative for anything this
document doesn't override.

M5's objective: close the exact gap M4's own pre-implementation review
flagged and deliberately deferred (`docs/M4_ACCOUNTING_CORE.md` §1) —
`SaleReturn`/`SaleReturnItem` existed as ORM models since M1 with no
service, no endpoint, and no accounting integration; `Sale.status`'s
`VOIDED`/`REFUNDED`/`PARTIALLY_REFUNDED` values existed with nothing ever
transitioning into them. M4's hardening audit separately found and fixed
a CRITICAL issue that this gap made possible: a Manager could reverse a
`SALE`-sourced journal entry directly, correcting the books without
touching inventory, quantity, or payment — silently diverging the
operational and accounting ledgers. M5 is that missing operational
workflow, built atomically with its own accounting integration so the gap
closes for good rather than shifting to "coming in M5" indefinitely.

---

## 1. Pre-implementation review

Traced the actual code before writing anything:

- `Sale`/`SaleItem`/`Payment` (M1/M2): a sale is created and moves to
  `COMPLETED` atomically inside `finalize_sale` — inventory decrement,
  WAC read, revenue/tax/discount computation, and payment rows all
  happen in one transaction. **There is no intermediate "open cart"
  state that persists to the database** — a sale either doesn't exist
  yet, or exists already `COMPLETED` with everything already applied.
- `SaleReturn`/`SaleReturnItem` (M1 models, unused until now): already
  had `quantity`, `unit_price_refunded`, `unit_cost_refunded`, `restock`,
  `reason`, `refund_method`, `refund_amount` — the shape M5 needed was
  already right, just never wired to a service.
- `inventory_service.compute_new_wac` (M1/M3): a pure function of
  `(existing_qty, existing_wac, received_qty, received_unit_cost)` — no
  assumption baked in that "received" means "purchased." A return of
  previously-owned inventory fits the same shape exactly.
- `PurchaseOrderItem.quantity_received` (M3): the exact prior-art pattern
  for a maintained running-total cache column, incremented only under a
  lock on the parent row, backstopped by a DB `CHECK`.
- `accounting/models.py AUTOMATED_SOURCE_TYPES` (M4, proactively): already
  included `"SALE_RETURN"` — M4 added it "schema-ready" specifically so
  this milestone's automated-source reversal block would already cover
  it without a migration.

Conclusion: build the return/void workflow as a **new, additive layer**
over the existing sale/inventory/accounting machinery — reuse
`compute_new_wac` unchanged, mirror M3's quantity-tracking-column pattern
exactly, and post through a new `SALE_RETURN` journal function that
follows M4's existing posting-function shape. Nothing in M0–M4 was
redesigned.

---

## 2. Return vs. void — one workflow, not two

**Decision: void is a thin wrapper over return, not a parallel
implementation** (the task's own explicit fallback for when the two are
not semantically distinct in this system).

Proof: since `finalize_sale` never leaves a sale in a non-`COMPLETED`,
inventory-not-yet-applied state, there is no scenario where "void" could
mean anything economically different from "return 100% of every
remaining unit, restocked." `void_sale()`:

1. Locks the `Sale` row.
2. Computes the remaining (unreturned) quantity of every line.
3. Raises `ConflictError(error_code="NOTHING_TO_VOID")` if nothing
   remains (the sale is already fully refunded).
4. Delegates to `create_sale_return(..., _is_void=True, restock=True for
   every line)` — `_is_void` only changes the audit action logged
   (`VOID_COMPLETED` vs. `SALE_RETURN_COMPLETED`); every other rule
   (locking, idempotency, accounting, RBAC) is identical.

This means a "void" and a "100%-quantity full return" are byte-for-byte
the same operation under the hood, sharing one `client_transaction_id`
uniqueness space on `sale_returns`.

---

## 3. Historical-snapshot-only pricing

A return never reads the product catalog's current price, current tax
rate, or current WAC. Every dollar figure a return computes comes from
the **original `SaleItem`'s already-frozen columns**:

| Return figure | Source (frozen at sale time) |
|---|---|
| `unit_price_refunded` | `sale_item.unit_price_at_sale` |
| `unit_cost_refunded` (COGS/WAC input) | `sale_item.unit_cost_at_sale` |
| `discount_refunded` (proportional share) | `sale_item.discount_amount` |
| `tax_refunded` (proportional share) | `sale_item.tax_amount` |

`SaleReturnLineCreate`/`SaleReturnCreate`/`VoidSaleCreate` (the API
request schemas) deliberately carry **no price, discount, tax, cost, or
refund_amount field at all** — only `sale_item_id`, `quantity`, and
`restock`. A client cannot submit a dollar figure for a return even if it
tried; the schema structurally excludes it. This closes the "tampered
price/discount/tax/cost" adversarial category by construction rather
than by runtime validation (see §22 of `docs/M5_HARDENING_AUDIT.md`).

Proven historically-correct under both a moved tax rate
(`test_tax_reversal_uses_historical_tax_not_current_rate`) and a moved
WAC (`test_return_cogs_reversal_uses_frozen_cost_not_current_wac`,
`test_return_blends_into_current_wac_using_original_sale_cost`).

---

## 4. Partial-return telescoping (discount/tax rounding)

A line-level aggregate (`discount_amount`, `tax_amount`) must be
distributed across possibly many separate partial returns of the same
line without ever exceeding the original total, and while summing to
exactly the original total once the line is fully returned — regardless
of how rounding falls on any individual return.

**`_proportional_share()`** computes this as the *difference between two
rounded cumulative entitlements*, not a fresh `total * this_qty /
original_qty` each time:

```python
entitled_after  = round(total * (already_returned_qty + this_return_qty) / original_qty)
entitled_before = round(total * already_returned_qty / original_qty)
share = entitled_after - entitled_before
```

Because `entitled_after` for return *N* becomes `entitled_before` for
return *N+1*, the partial shares telescope exactly:
`Σ share_i = entitled_after(last) = round(total * original_qty /
original_qty) = total`. Proven with a 3-unit line carrying a $3.00
discount split across three separate 1-unit returns
(`test_discount_telescopes_correctly_across_partial_returns`), which sums
to exactly $3.00 despite $3.00/3 not landing on a clean per-unit amount
at every intermediate step.

---

## 5. Inventory / WAC treatment — reusing `compute_new_wac`, not inventing a new formula

A restocked return is treated as a **receipt of previously-owned
inventory, at its own historical (frozen) cost**:

```python
new_wac = inventory_service.compute_new_wac(
    existing_qty=product.current_qty_on_hand,
    existing_wac=product.current_cost,
    received_qty=requested_qty,
    received_unit_cost=sale_item.unit_cost_at_sale,  # frozen, never current
)
inventory_service.record_movement(
    ..., movement_type="SALE_RETURN", quantity_delta=+requested_qty,
    unit_cost_at_movement=sale_item.unit_cost_at_sale, new_product_cost=new_wac,
)
```

Two proven cases:

- **Nothing else moved WAC since the sale** — averaging a cost with
  itself returns the pre-sale WAC exactly
  (`test_return_blends_into_current_wac_using_original_sale_cost`'s
  simple case).
- **WAC moved since the sale** (a receipt at a different cost happened in
  between) — the return correctly blends the returned units' *own*
  historical cost into the *current* average, not the current WAC applied
  to the returned units. Hand-verified: sell 2 units at WAC 4.0, receive
  10 @ 40.0 (moving WAC to 24.0), then return the 2 units → final WAC =
  `(18×24 + 2×4)/20 = 22.0`, never `24.0`
  (`test_return_blends_into_current_wac_using_original_sale_cost`'s full
  case; extended end-to-end in
  `test_sales_returns_reconciliation.py`).

`SaleReturnItem.restock: bool` (pre-existing M1 field) gates this
entirely: when `False` (a write-off — damaged, expired, non-resellable),
inventory quantity/WAC and the `Dr Inventory / Cr COGS` accounting pair
are both skipped. The COGS already recognized at sale time simply stays
expensed — no re-recognition, no new write-off account, no inventory
movement. The refund's revenue/tax/discount/cash lines alone still
balance (proven in
`test_non_restock_return_posts_no_inventory_or_cogs_entry`).

---

## 6. Quantity tracking and DB-level enforcement

**`SaleItem.quantity_returned`**: a maintained running-total cache column
(mirrors M3's `PurchaseOrderItem.quantity_received` exactly), incremented
only inside `create_sale_return` under a lock on the parent `Sale` row
acquired *first* — the same "lock the parent to serialize children
writes" pattern M3 established for PO/receiving concurrency.

Backstopped by a DB `CHECK (quantity_returned >= 0 AND quantity_returned
<= quantity)` — proven load-bearing, not decorative, by a mutation test
(§ Mutation testing below) that disabled the application-layer
over-return guard and confirmed the database itself rejected the
resulting write with a `CheckViolation`, not silent corruption.

---

## 7. Idempotency — including conflicting-payload rejection

Extends the M2/M3/M4 `client_transaction_id` pattern with a requirement
new to M5: a **matching key with a *different* payload** (a client bug
reusing a UUID for an unrelated return) is rejected with
`IDEMPOTENCY_KEY_CONFLICT` rather than silently returning an unrelated
result or silently creating a second one. The payload signature compared
is `(sale_id, refund_method, sorted[(sale_item_id, quantity, restock)])`.

**Checked twice**, not once — a genuine concurrency finding from this
milestone's Session D testing (see §11):

1. **Before any lock** (fast path for the common sequential-retry case —
   avoids wasted lock contention for a request that will short-circuit
   anyway).
2. **Immediately after the `Sale` row lock is acquired**, before the
   status/quantity guards run. Postgres holds that row's `FOR UPDATE`
   lock until commit, so by the time a racing second caller acquires it,
   a genuinely concurrent identical request has either not started or
   has already committed and is visible to this re-check. Without the
   second check, a caller that loses the lock race can see the sale's
   status already advanced to `REFUNDED` by the winner and get a
   confusing `SALE_NOT_RETURNABLE` instead of transparently receiving the
   winner's result — a real bug this milestone's own concurrency testing
   found and fixed (see §11).

The DB-level backstop (a genuinely concurrent flush racing against the
`sale_returns.client_transaction_id` UNIQUE constraint) still exists as a
final layer and is what `test_c_two_concurrent_requests_with_the_same_idempotency_key_create_exactly_one_return`
exercises against real, independent connections.

---

## 8. Refund / payment semantics — honestly scoped

`SaleReturn.refund_method` is a single field, matching the *existing*
schema exactly — the operator's choice of how the refund was actually
handed back, not an automatic per-original-payment-method split across a
split-tender sale. **This system does not integrate with, call, or
simulate any external payment processor or card network.** "Processing a
CARD refund" here means: the operator confirms they refunded the card
through their processor's own terminal/dashboard, and records that fact
by choosing `CARD` as `refund_method` — the same honesty boundary
finalize_sale's own `Payment.reference` field already implies for the
original sale. No claim is made anywhere in code, schema, or UI that this
system settles money with a bank or processor.

---

## 9. Accounting integration

**`SALE_RETURN`** was already present in M4's `AUTOMATED_SOURCE_TYPES`
tuple (added proactively, schema-ready) — no model or migration change
was needed in `accounting/models.py`. `post_sale_return_journal`
(`accounting/service.py`) is a new posting function following the exact
shape of M4's existing ones (`post_sale_journal`,
`post_goods_receipt_journal`).

For each line, `sales/service.py` computes a frozen `SaleReturnLineEffect`
(refund price, discount/tax refunded, restock flag, quantity, historical
unit cost) — the accounting module never recomputes these from current
data, only sums what it's handed:

```
Debits  = Sales Revenue (Σ refund_price)      + Tax Payable (Σ tax_refunded)
Credits = Sales Discounts (Σ discount_refunded) + <refund-method account> (Σ refund_amount)
```

This balances algebraically by construction, since
`refund_amount = refund_price − discount_refunded + tax_refunded` for
every line. Separately, restocked lines produce a self-balancing
`Dr Inventory / Cr COGS` pair using the *same* computed value
(`quantity × unit_cost_refunded`) on both sides — proven in
`test_return_journal_debits_revenue_and_tax_credits_discount_and_cash`
and the end-to-end reconciliation scenario (§12).

Posted **inline, in the same uncommitted transaction** as the operational
return (inventory movement, `quantity_returned` update, `Sale.status`
advance) — not a separate call, not eventually-consistent. A forced
failure at the accounting-posting step rolls back the entire attempt,
including inventory/WAC changes already applied earlier in the same
function call (§13, failure injection).

**The automated-source reversal block is exercised, not just assumed
compatible**: `reverse_journal_entry` refuses to reverse a
`SALE_RETURN`-sourced entry with `OPERATIONAL_REVERSAL_REQUIRED`, proven
live (not just by the tuple containing the string) in
`tests/test_sales_returns_mutation.py::test_mutation_removing_sale_return_from_automated_sources_allows_bare_journal_reversal`.

---

## 10. RBAC

Three new, minimal permissions — not a reuse of the broad
`accounting.reverse` — reflecting the real POS convention that a cashier
can process an ordinary return but a full void needs manager override:

| Permission | Admin | Manager | Cashier | Auditor |
|---|---|---|---|---|
| `sales.return.read` | ✓ | ✓ | ✓ | ✓ |
| `sales.return.write` | ✓ | ✓ | ✓ | |
| `sales.void` | ✓ | ✓ | | |

Seeded by the M5 migration using the same `ON CONFLICT DO
UPDATE`/`DO NOTHING` idempotent-seed pattern M4's hardening migration
established (needed because the M2 seed migration reads `ALL_PERMISSIONS`
live at migration-run time — a fresh database already includes M5's
codes on replay, while an existing database needs them inserted).

---

## 11. Concurrency

Three scenarios, each run 5× against real, independent PostgreSQL
connections with `threading.Barrier` (the `db` fixture's savepoint
isolation cannot exercise real cross-connection locking — see
`tests/test_sales_returns_concurrency.py`'s module docstring):

- **A — race to return the same single remaining unit**: exactly one of
  two concurrent callers wins; the loser is correctly rejected (either
  `EXCESSIVE_RETURN_QUANTITY` or `SALE_NOT_RETURNABLE`, depending on
  which layered guard it hits — both are correct outcomes of the same
  serialized validation, never a silent double-return).
- **B — two concurrent partial returns summing exactly to the sold
  quantity**: both succeed; the line lands at exactly fully returned,
  never over.
- **C — two concurrent requests with the same idempotency key**: exactly
  one `SaleReturn` row is created; both callers observe the identical
  return id.

**A genuine bug was found and fixed during this testing** (not merely
confirmed absent): the idempotency check originally ran only once, before
the `Sale` lock. Two racing identical requests could both pass it before
either committed; the loser would then reach the post-lock status guard
and see the sale already `REFUNDED` by the winner, surfacing
`SALE_NOT_RETURNABLE` instead of transparently returning the winner's
result — a real idempotency-under-concurrency defect, not a hypothetical
one. Fixed by adding the identical idempotency check again immediately
after the `Sale` lock is acquired (§7) — proven both as a live
concurrency scenario (C, above) and as a standalone, permanent mutation
test (`test_mutation_removing_idempotency_check_breaks_retry_transparency`).

---

## 12. End-to-end reconciliation

`tests/test_sales_returns_reconciliation.py` runs a composite scenario
extending M4's hardening-audit comprehensive-scenario style:

**Sale A → Return (1 of 3) → New Sale B → WAC-changing receipt → Another
Return (1 of 2, from Sale B, after WAC moved)** — hand-derived and
independently verified: GL Inventory balance `170.000000` vs. operational
valuation `170.000004`, a `-0.000004` discrepancy — within the same
documented, bounded WAC-rounding tolerance (`≤ 0.00001`) M4's own
hardening audit established, not a new or widened tolerance. Trial
balance balances exactly; `net_sales=75.00`, `cogs=30.00`,
`gross_profit=45.00` all confirmed against independently hand-computed
expected values before the test was ever run (it passed on the first
run).

---

## 13. Failure injection / atomicity

`tests/test_sales_returns_failure_injection.py` forces a `RuntimeError`
at four different points and proves a complete rollback each time — no
`SaleReturn` row, no inventory movement, no `quantity_returned` change,
no `Sale.status` change, no journal entry:

- Inside `inventory_service.record_movement` (after the `SaleReturn`/
  `SaleReturnItem` rows are already flushed).
- Inside `accounting_service.post_sale_return_journal` (after inventory/
  WAC/`quantity_returned` are all already applied).
- Inside `audit_service.log_event`'s second call within the function.
- Through `void_sale`'s wrapper (proving the wrapper doesn't weaken
  atomicity relative to calling `create_sale_return` directly).

---

## 14. Store isolation

Checked in two independent layers (defense-in-depth), proven independent
by a mutation test rather than assumed:

1. **Route layer**: `enforce_store_access(current_user, payload.store_id)`
   in the endpoint, before the service is even called.
2. **Service layer**: a lightweight pre-lock scalar query against the
   sale's *actual* store (fail-fast, before any lock contention — mirrors
   `receive_goods`'s pattern), plus a post-lock `sale.store_id !=
   store_id` check (`STORE_MISMATCH`).

`test_store_isolation_mutation_test_removing_enforce_store_access`
monkeypatches out the route-layer call and confirms the request **still**
gets `403` — proving the service-layer check is a real, independent
second line of defense, not dead code shadowed by the route check.

---

## 15. Known limitations / deferred functionality

- **No external payment/refund processor integration** (§8) — by design,
  not an oversight; matches the existing `Payment` model's own scope.
- **`refund_method` is one field per return**, not an automatic
  per-original-tender split for a split-tender sale — matches the
  existing schema; a future milestone could add itemized refund-tender
  splitting if a real requirement emerges.
- **WAC-rounding drift** in reconciliation is the same documented,
  bounded (`≤ 0.00001`), pre-existing limitation from M4 — not
  reintroduced or worsened by returns; it appears because a return is
  itself a WAC-recompute event, same as any receipt.
- **No period-closing / retained-earnings workflow** — unchanged from M4;
  out of scope here.
- **The frontend return UI is intentionally minimal** (`SalesPage.tsx`):
  find a sale by ID, inspect eligible lines, select quantities/restock,
  submit. No barcode/search-based sale lookup, no receipt-printing
  integration — matches the "minimal POS return UI" scope. The server
  remains the sole source of financial truth; the UI never computes or
  displays an estimated refund before the server confirms it, only the
  server's own authoritative response after submission.

See `docs/M5_HARDENING_AUDIT.md` for the full adversarial/mutation-testing
evidence and final verdict.
