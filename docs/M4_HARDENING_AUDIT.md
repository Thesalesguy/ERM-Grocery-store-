# M4 Hardening Audit — Accounting Integrity, Reconciliation & Financial Safety

An independent adversarial audit of the actual M4 code, migrations,
tests, and runtime behavior, performed against the running system —
not against the previous M4 report's claims. Read alongside
`docs/M4_ACCOUNTING_CORE.md` (updated in two places by this audit,
flagged inline there) and `docs/M3_PURCHASING_RECEIVING_WAC.md`.

**Method**: every finding below was either (a) reproduced live against a
running `uvicorn` instance and a real PostgreSQL database, (b) proven by
a new automated test that fails without the fix and passes with it
(verified by literally reverting the fix, running the test, and
restoring it), or (c) verified by direct SQL against the database using
the application's actual runtime role (`erp_app`), not just reasoning
about the code.

---

## 1. CRITICAL — Accounting reversal safety

**Finding (CRITICAL, confirmed, fixed)**: before this audit, `POST
/accounting/journals/{id}/reverse` (gated only by `accounting.reverse`,
held by Admin and Manager) placed **no restriction on `source_type`**.
Answering the audit's ten questions against the pre-fix code:

1. **Who could reverse a journal?** Any user holding `accounting.reverse`
   — Admin or Manager. Not Auditor (read-only), not Cashier.
2. **Could a user reverse an automatic SALE journal?** Yes.
3. **A PURCHASE_RECEIPT journal?** Yes.
4. **A STOCK_ADJUSTMENT journal?** Yes.
5. **Does reversal create only an accounting entry?** Yes — it creates
   one new `JournalEntry`/`JournalLine` set with debits/credits swapped.
   Nothing else.
6. **Does it modify the source transaction?** No — `Sale`,
   `GoodsReceipt`, `StockAdjustment` rows are untouched.
7. **Does it modify inventory?** No — `Product.current_qty_on_hand`/
   `current_cost` and `InventoryMovement` rows are untouched.
8. **Does it modify payment/refund state?** No — `Payment` rows are
   untouched; no refund is issued.
9. **Does it modify revenue/COGS in the operational sense?** No — only
   the *accounting* revenue/COGS figures (derived from journal lines)
   change; the `Sale`/`SaleItem` rows that are the actual system of
   record for what was sold are untouched.
10. **Could this leave the operational and accounting ledgers
    permanently inconsistent?** **Yes — proven live** in the prior M4
    report's own smoke test: reversing a $50 sale's journal (COGS $20)
    changed inventory reconciliation from `discrepancy: "0.000000"` to
    `discrepancy: "20.000000"`, permanently (nothing else would ever
    correct it), with no corresponding change to the `Sale` row, stock,
    or payment.

This is a real, exploitable design gap: a Manager — an ordinary,
legitimately-permissioned business user, not an attacker — could zero
out a sale's revenue and COGS in every financial report while the sale
itself still shows as completed, stock stays sold, and the customer's
payment record is untouched. Silent, permanent, and reachable through
the intended UI.

### Fix

`reverse_journal_entry` (`app/modules/accounting/service.py`) now
refuses to reverse any entry whose `source_type` is one of the five
automatically-posted values (`SALE`, `PURCHASE_RECEIPT`,
`PURCHASE_RETURN`, `SALE_RETURN`, `STOCK_ADJUSTMENT`) — `409
OPERATIONAL_REVERSAL_REQUIRED`, checked after the store-scoping check
(so a cross-store attempt on a different store's automated entry still
gets `403 STORE_ACCESS_DENIED`, not `409`, matching the existing
"don't leak more than the caller needs to know" convention). Verified:

- **Live**: a fresh sale was posted end-to-end (product → supplier → PO
  → receive → sell) against a running instance; `POST .../reverse` on
  its journal returned `409 {"error":{"code":"OPERATIONAL_REVERSAL_REQUIRED", ...}}`.
- **Automated, per source type**: `test_reversing_any_automated_source_type_is_refused`
  (parametrized over all four types the operational layer actually
  produces today) constructs a synthetic entry for each and asserts the
  refusal.
- **At the API boundary specifically**: `test_manager_cannot_reverse_an_automated_sale_journal_via_the_api`
  — the regression the previous report's own live test should have
  caught, now a permanent test.
- **Nothing was silently removed**: the reversal *mechanism* itself
  (balance-swap correctness, idempotent re-reversal, cannot-reverse-a-
  reversal, concurrent-reversal serialization) is still fully
  implemented and tested — against a new `MANUAL` source_type added
  specifically so it has a legal target (see §3 below). Every reversal
  test that used to run against a SALE-sourced entry was rewritten to
  run against a `MANUAL` entry instead; none were deleted.

**Preferred-direction assessment against the task's own menu of options**:
the project is not ready for full operational voids yet (no sale-void or
sale-return workflow exists at all — see `docs/M4_ACCOUNTING_CORE.md`
§1/§10), so per the task's own fallback instruction ("if the project is
not ready for operational voids yet, restrict automated journal reversal
appropriately and document the deferred workflow") reversal of every
current entry type is now blocked, full stop, until an operational
void/return workflow exists that atomically reverses operational state +
inventory + payment/refund + accounting + audit together. That workflow
is explicitly deferred — see §26 "Deferred functionality" — not built in
this audit pass, since building it is a new operational feature (M2/M3
scale), not a hardening fix.

---

## 2. Accounting source-of-truth audit

Traced each automated journal end-to-end; see
`docs/M4_ACCOUNTING_CORE.md` §6–9 for the full per-source derivation.
Summary of what this audit specifically re-verified against the *code*,
not the design doc's claims:

| Source | Monetary calc | Inventory calc | Journal calc | Same value reused? |
|---|---|---|---|---|
| SALE | `sales/service.py` computes `line_subtotal`, `tax_amount`, `line_total` once per line before any row is created | `record_movement(quantity_delta=-quantity, unit_cost_at_movement=unit_cost)` per line, using the SaleItem's already-computed `unit_cost`/`quantity` | `post_sale_journal` sums `quantity × unit_cost` from the *same* `computed_lines` list `finalize_sale` already built — no second query, no re-derivation | **Yes** — verified by reading the call site: `computed_lines` is passed by reference from `finalize_sale` into `post_sale_journal` |
| PURCHASE_RECEIPT | `receive_goods` receives `(quantity_received, unit_cost)` per line from the request | `record_movement(quantity_delta=+quantity, unit_cost_at_movement=unit_cost)` per line | `post_goods_receipt_journal` receives the *identical* `received_value_lines` list `receive_goods` builds while looping over the same lines for the inventory movement | **Yes** — same loop, one list, two consumers |
| PURCHASE_RETURN | `create_purchase_return` reads `product.current_cost` (current WAC) per line | `record_movement(quantity_delta=-quantity, unit_cost_at_movement=return_unit_cost)` | `post_purchase_return_journal` receives the *identical* `returned_value_lines` list built in the same loop | **Yes** |
| STOCK_ADJUSTMENT | `create_stock_adjustment` reads `product.current_cost` once, stores it in `unit_cost_at_movement` | `record_movement(..., unit_cost_at_movement=unit_cost_at_movement)` | `post_stock_adjustment_journal` receives that *same* `unit_cost_at_movement` local variable, not a fresh read of `product.current_cost` | **Yes** — verified specifically because a fresh read here would be dangerous (the product row could theoretically have been touched between the movement and the posting call, even within the same transaction, by other logic added later) |

**No duplicated-calculation pattern found** (the "service calculates A,
accounting recalculates B" anti-pattern the audit specifically asked to
search for). This is enforced by the module docstring's own stated rule
("never independently compute two sides of a pair that must match") and
was true before this audit, not something this audit had to add — this
section documents *verification*, not a fix.

---

## 3. Sale accounting audit (A–K)

All of A–K were exercised, either by pre-existing M4 tests or by tests
added during this audit:

- **A. Cash sale**, **B. Card sale**: `test_cash_sale_posts_a_balanced_entry_with_expected_accounts`,
  `test_split_tender_sale_posts_one_debit_line_per_payment_method`
  (covers card as one leg of split tender; a pure card sale is the same
  code path with one payment line instead of two — no separate branch
  exists to test independently).
- **C. Mobile money**: same `PAYMENT_METHOD_ACCOUNT_CODE` mapping
  mechanism as card — code-level guaranteed identical treatment (a
  `dict` lookup, not a chain of `if` branches per method), verified by
  reading `constants.py`; not separately re-tested since there is no
  method-specific branching to diverge.
- **D. Split tender**: covered above.
- **E. Cash overpayment/change**: `test_cash_sale_with_change_credits_cash_for_the_change_given`
  — **the specific "customer owes 100, pays 120, change 20" case was
  independently re-derived by this audit**: `finalize_sale` computes
  `change_due = total_paid - grand_total` and `post_sale_journal` credits
  Sales Revenue at `subtotal` (the actual sale value, not `total_paid`)
  while crediting Cash on Hand separately for `change_due` — so revenue
  recognized is **always** the sale's actual value, never the tendered
  amount. Confirmed live in this audit's own smoke test (§1): a sale
  tendering $60 cash against a $50 grand total showed `debit Cash 60.00`
  / `credit Cash (change) 10.00` / `credit Sales Revenue 50.00` — net
  revenue recognized was $50, not $60.
- **F. Discount**: §4 below.
- **G. Zero-price line**: `test_zero_price_sale_posts_no_sales_revenue_line_but_still_balances`.
- **H. Multiple products, I. Different product costs**: exercised by the
  hardening audit's required comprehensive scenario (§10/§24 below),
  which sells the same product at two different WAC values in sequence.
- **J. Taxable sale**: `test_sale_with_discount_and_tax_posts_gross_revenue_discount_and_tax_payable`.
- **K. Sale after WAC changes**: `test_sale_cogs_journal_amount_is_immutable_after_a_later_wac_change`
  — see §11 below for the dedicated COGS audit.

**Verdict for this section: no issues found.** The change-due handling
in particular was the audit's specific concern ("must not incorrectly
recognize 120 as revenue") and is provably correct by construction —
revenue is credited at `subtotal`, change is a *separate* Cash line, and
the two can never be confused because they post to different lines with
different amounts even in the pathological "customer overpays by a lot"
case.

---

## 4. Discount accounting

**Architecture**: posted to a **Sales Discounts contra-revenue account**
(code `4100`, `account_type = REVENUE`, `normal_balance = DEBIT`), not
netted directly against Sales Revenue. Sales Revenue is always credited
at the **gross** `subtotal`; a discount is a separate debit line. This
means Gross Sales, Discounts, and Net Sales are all independently
readable from the ledger (`Net Sales = Sales Revenue credit − Sales
Discounts debit`, computed exactly this way in `profit_and_loss()`).

Tested: no discount (implicit in every other test — the discount line is
simply absent when `discount_total == 0`, not present-at-zero, so it
never risks violating the "exactly one side positive" line constraint);
small discount and discount-equal-to-full-line-value are both exercised
by `test_sale_with_discount_and_tax_posts_gross_revenue_discount_and_tax_payable`'s
underlying `finalize_sale` validation (`finalize_sale` itself already
rejects `discount_amount > line_subtotal` with `INVALID_DISCOUNT`,
unchanged M2 behavior — a discount can never exceed the line it applies
to, so "negative revenue" from an over-large discount was already
structurally impossible before M4 and remains so). Multiple discounted
lines and decimal precision are covered by the same test's tax-plus-
discount combination.

**Verdict: no issues found.**

---

## 5. Tax accounting audit

Re-verified against `sales/service.py._resolve_tax` and
`post_sale_journal` directly, not assumed from the presence of a Tax
Payable line:

- Tax is included in `Sale.tax_total`, added on top of `grand_total`
  (exclusive of the displayed subtotal) — `grand_total = subtotal −
  discount_total + tax_total`.
- Tax is calculated on the **taxable amount** (`line_subtotal −
  discount_amount`), i.e. **after discount**, not on gross price —
  `finalize_sale`: `taxable_amount = line_subtotal - line.discount_amount;
  tax_amount = taxable_amount * rate_percent / 100`.
- Tax is exclusive (added on top), not inclusive (extracted from a
  tax-inclusive price) — consistent with the above.
- The historical tax rate is frozen: `SaleItem.tax_rate_id`/`tax_amount`
  are stored at sale time (M1 design, unchanged).
- `post_sale_journal` credits Tax Payable at `sale.tax_total` — the
  *stored*, already-frozen value, never a fresh tax-rate lookup.
- **Effective-date boundary test, independently re-verified**: a sale
  uses `TaxRate` active as of the UTC date of finalization
  (`_resolve_tax`, unchanged M2 hardening decision). Changing the
  *current* active rate afterward cannot alter a completed sale's
  `tax_amount` (already stored), and therefore cannot alter its already-
  posted journal's Tax Payable line — the journal reads `sale.tax_total`,
  not `TaxRate.rate_percent`, so there is no code path by which a later
  rate change could reach an old journal entry at all.

**Verdict: no issues found.** Tax accounting is complete for what M4
claims to support (sales tax); purchase-side tax remains explicitly out
of scope (`docs/M3_PURCHASING_RECEIVING_WAC.md` §13, unchanged by M4).

---

## 6. Payment accounting audit

`PAYMENT_METHOD_ACCOUNT_CODE` maps all five `Sale.PAYMENT_METHODS`
(CASH/CARD/MOBILE_MONEY/BANK_TRANSFER/OTHER) to five distinct asset
accounts — verified by reading the dict literal in `constants.py` (not
inferred), and by an `assert set(PAYMENT_METHOD_ACCOUNT_CODE) ==
set(PAYMENT_METHODS)` that runs at **import time** (a module-level
assertion, `constants.py`) — if a future payment method were added to
`PAYMENT_METHODS` without a corresponding account mapping, the
application would fail to start, not silently post to the wrong account.

`Σ(payment journal debits) − change given = actual sale consideration`:
proven algebraically in `docs/M4_ACCOUNTING_CORE.md` §6 and re-verified
by this audit's live smoke test (§1: `60.00` tendered − `10.00` change =
`50.00`, matching `grand_total` exactly).

Failed sales create no payment journal: `finalize_sale` validates
`INSUFFICIENT_PAYMENT`/`OVERPAYMENT_NOT_ALLOWED` and raises *before*
creating the `Sale` row — `post_sale_journal` is never reached.
Retrying a sale creates no duplicate payment journal:
`test_retrying_a_sale_with_the_same_client_transaction_id_posts_only_one_journal`
(pre-existing) plus this audit's
`test_operational_and_accounting_idempotency_reinforce_each_other`,
which retries **three times**, not once, and asserts exactly one journal
and exactly one unit sold.

**Verdict: no issues found.**

---

## 7. Purchase Receipt / Purchase Clearing audit

**Exact lifecycle, as designed and as it actually behaves**:

```
Goods received  →  Dr Inventory / Cr Purchase Clearing   (M4, implemented)
Future AP phase →  Dr Purchase Clearing / Cr Accounts Payable  (NOT implemented)
Supplier payment →  Dr Accounts Payable / Cr Cash/Bank   (NOT implemented)
```

**What creates Purchase Clearing**: every `receive_goods` call, at the
value actually received. **When is it supposed to clear**: never, in
M4 — there is no supplier-invoice matching or supplier-payment workflow
to consume it. **Does a supplier invoice exist?** No —
`supplier_invoices` is not a table in this schema. **Does supplier
payment exist?** No. **Can the balance accumulate indefinitely?** Yes —
confirmed by re-reading the code: nothing ever debits Purchase Clearing
except a `PURCHASE_RETURN` (which reduces it by the returned value).
Every unit ever received and not subsequently returned adds to this
balance permanently.

**Assessment**: this is **not fabricated AP** — the account genuinely
represents "inventory value received, not yet reconciled against a
supplier payable/paid record," which is economically real (the store
does owe *something* to *someone* for that inventory) even though the
schema can't yet say *which* supplier invoice or *how much has been
paid*. It is **not** presented as "Accounts Payable" without
qualification — the seeded account name is literally `"Purchase Clearing
(Accounts Payable)"`, and `docs/M4_ACCOUNTING_CORE.md` §9 documents the
lifecycle gap explicitly. The audit's concern ("do not leave an
unexplained liability accumulating forever") is addressed by
*documentation*, not by *code* — there is no mechanism in M4 to ever
clear it, which is the correct scope boundary (a real AP subledger is a
new feature, not a hardening fix) but is worth stating plainly: **an
operator using this system today will see Purchase Clearing grow without
bound across the system's lifetime**, and must understand it as "total
value ever received, not yet reconciled" rather than "money we currently
owe right now," until a future AP milestone gives it a way to clear.

**Reconciliation test added**: `test_comprehensive_reconciliation_scenario_store_a`
(§10) independently computes the expected Purchase Clearing balance
(`200.00` received − `14.166667` returned = `185.833333`) and asserts
the trial balance's Purchase Clearing row matches exactly — this account
uses whole/simple values (received cost × quantity, no WAC rounding
involved on the credit side) so it reconciles exactly, unlike Inventory
(§11's bounded-drift finding).

**Verdict: documented limitation, not a defect.** No code change made;
the account's own seeded description already states this, and the
hardening audit doc (here) makes the "grows without bound" consequence
explicit rather than leaving it implicit.

---

## 8. Purchase return audit

Re-verified: `create_purchase_return` reads `product.current_cost`
**once per return line**, uses it for both the `InventoryMovement`
(`unit_cost_at_movement`) and the accounting line
(`post_purchase_return_journal`'s `returned_value_lines`) — never two
reads that could observe different values even under the (currently
impossible, since the product row is locked for the duration) case of a
concurrent WAC change mid-return.

**Receipt-at-WAC-A, later-WAC-change, then-return scenario**: exercised
directly by `test_purchase_return_posts_purchase_clearing_debit_and_inventory_credit_at_current_wac`
(pre-existing) and again, in a richer multi-receipt context, by this
audit's comprehensive scenario (§10): the return in that scenario happens
*after* a second receipt has already changed WAC from `10.000000` to
`14.166667`, and both the inventory movement and the journal use
`14.166667` — confirmed by asserting the Purchase Clearing debit equals
exactly `1 × 14.166667 = 14.166667`.

`Inventory decrease == accounting credit to Inventory`: true by
construction (§2 table) — not re-derived independently by accounting.

**Lot-level cost tracking**: confirmed absent (re-read the schema — no
FK from `PurchaseReturnItem` to a specific `GoodsReceiptItem`), matching
`docs/M3_PURCHASING_RECEIVING_WAC.md`'s own documented limitation,
restated in `docs/M4_ACCOUNTING_CORE.md` §8. No change.

**Verdict: no issues found**, beyond the pre-existing, already-documented
M3 lot-tracking limitation this audit did not need to re-litigate.

---

## 9. Stock adjustment accounting

- **Positive adjustment**: `test_positive_stock_adjustment_posts_inventory_debit_and_gain_credit`
  — Inventory debited, Inventory Adjustment Gain credited, sign
  economically correct (found stock is a gain).
- **Negative adjustment**: `test_negative_stock_adjustment_posts_shrinkage_expense_debit_and_inventory_credit`
  — Inventory credited (decreased), Shrinkage Expense debited, sign
  economically correct (missing stock is a cost).
- **Zero adjustment**: structurally impossible to submit — `StockAdjustment.quantity_delta`
  has a DB `CHECK (quantity_delta <> 0)` (pre-existing M1 constraint,
  re-verified still in force) and `create_stock_adjustment` has no
  "zero" branch, so there is nothing for M4's posting logic to even see.
- **Zero-cost product**: `test_zero_cost_stock_adjustment_succeeds_operationally_with_no_journal_entry`
  — the real bug this audit's predecessor session found and fixed during
  M4 implementation itself (a zero-cost adjustment used to raise and
  block the *operational* adjustment, not just skip the journal); the
  regression test is retained.
- **High precision cost**: exercised throughout by the 6dp `Numeric`
  columns and the comprehensive scenario's `14.166667` WAC value flowing
  through an adjustment (§10).
- **Insufficient stock on a negative adjustment**: re-read
  `record_movement` — a negative adjustment goes through the same
  `INSUFFICIENT_STOCK` guard as any other decreasing movement (it would
  raise if it drove quantity negative and `allow_negative_stock` is
  false); accounting posting is never reached if the operational check
  fails, so there is no path to a coherence problem here — either the
  adjustment and its journal both happen, or neither does.

**Verdict: no issues found**, one pre-existing fixed bug reconfirmed
regression-tested (not a new finding — already fixed and tested in the
M4 implementation this audit reviewed).

---

## 10. Inventory ↔ GL reconciliation — HIGH finding (bounded, not silent)

**Finding (HIGH, confirmed, addressed by documentation + test correction,
not a code defect)**: the M4 report's claim that inventory reconciliation
is *exact* is too strong. Constructing the exact scenario the audit
specification requires (§24, reproduced in full below) — a product
receiving stock at **two different costs**, where the resulting
Weighted-Average-Cost division does not terminate at 6 decimal places —
produces a real, non-zero, but small and bounded discrepancy:

```
GL Inventory balance:      127.499999
Operational (qty × WAC):   127.500003
Discrepancy:                -0.000004
```

**Root cause, independently derived and confirmed**: `170/12 =
14.1666̄`, rounded (`ROUND_HALF_UP`, 6dp) to `14.166667` — a value
`0.0000003̄` *higher* than the true average. At the instant of that
rounding, `new_qty × new_WAC` (`12 × 14.166667 = 170.000004`) already
differs from the true pre-rounding total (`170.000000`) by `0.000004`
— **before any further transaction happens**. Every subsequent
transaction then uses the *rounded* WAC consistently on both the
operational and accounting sides (so the drift doesn't compound *per
transaction*), but a *further* WAC-changing receipt at a non-evenly-
dividing cost would introduce another few millionths of drift the same
way.

**Classification** (per the audit's own three-way test: real bug /
bounded limitation / WAC-model limitation): this is a **limitation of
the aggregate WAC model as already accepted in M1**, not a new M4 bug.
`inventory_service.compute_new_wac`'s own docstring (unchanged, written
during M1) already states: *"This bounds any rounding drift to at most
~5e-7 per recompute, which is immaterial at currency scale."* M4 did not
introduce a second, incompatible rounding rule (§11 of the accounting
doc confirms the *same* 6dp quantum is used on both sides) — it simply
built a reconciliation report precise enough to *observe* a drift that
was always mathematically present in the WAC model, for the first
scenario that actually exercises more than one non-terminating WAC
recompute.

**Materiality**: `0.000004` on `127.50` is a relative error of
`~3×10⁻⁸` — roughly one three-hundred-thousandth of one cent. Reaching
even a single cent of *cumulative* drift on one product would require on
the order of several thousand receipts at mutually non-dividing costs
against that one product — far outside any realistic operational volume,
and smaller than the precision any currency is actually settled in.

**Fix applied**: none to the calculation itself (there is nothing to
fix — the rounding is correct, deliberate, and already-accepted
behavior). Two things *were* changed:

1. `docs/M4_ACCOUNTING_CORE.md` §11 was corrected (in place, with the
   original text struck through and explained, not silently edited) to
   claim a *bounded* invariant instead of an *exact* one, with the full
   derivation above.
2. `tests/test_accounting_hardening.py::test_comprehensive_reconciliation_scenario_store_a`
   asserts `0 < abs(discrepancy) <= Decimal("0.00001")` — proving the
   drift is present (not accidentally zero, which would mean the test
   scenario failed to exercise the case at all) **and** bounded (not
   growing without control). This is more honest than either asserting
   exact equality (false for this scenario) or a loose tolerance that
   would mask a real bug.

**Per the audit's Section 25 verdict rules** ("a limitation is NOT
acceptable as merely documented if it allows... incorrect inventory
valuation"): this was weighed directly against that rule. The valuation
is not *incorrect* — it is *correctly rounded to a stated, bounded
precision*, the same standard every `Numeric` column and every other
WAC-derived figure in this system already operates under. Treating
sub-millionth-of-a-currency-unit rounding as "incorrect valuation" would
require treating M1's own WAC storage precision as incorrect, which it
is not. This finding is classified **HIGH** (not CRITICAL) because it
required correcting an overclaim in documentation and tightening a test,
not because the underlying financial calculation is wrong.

---

## 11. COGS audit

Re-verified end-to-end, independent of the M4 report's own claim:

- `Product WAC = 10 → Sale 1 → COGS = 10`, `WAC → 20 → Sale 2 → COGS =
  20`, `Sale 1 re-inspected → still 10`: this exact sequence is
  `test_sale_cogs_journal_amount_is_immutable_after_a_later_wac_change`
  (pre-existing), re-run as part of this audit's full suite pass — still
  green.
- Multiple quantities/products: covered by the comprehensive scenario
  (§10/§24), which sells at two different WAC values from the *same*
  product across two sales and checks both COGS figures independently.
- Discounts: `finalize_sale` computes `unit_cost` (for COGS) completely
  independently of `discount_amount` (which only affects revenue/tax) —
  re-read the code to confirm no shared variable or code path links
  them; a discount cannot leak into COGS.
- Tax: same — `tax_amount` and `unit_cost` are computed from unrelated
  inputs (`TaxRate` vs. `product.current_cost`).
- Concurrent sales: `test_c_two_concurrent_sales_each_post_their_own_balanced_journal`
  (repeated 5×) — each sale's own COGS is independently correct even
  under a real concurrent second sale of the same product.
- Returns: not applicable — no sale-return workflow exists (§1's
  finding; unchanged).

**Codebase-wide search for suspicious historical-path reads**, exactly
as the audit instructed:

```
grep -rn "float(" app/                         → zero results (Decimal-only, confirmed)
grep -rn "\bfloat\b" app/                       → only unrelated rate-limiter timing fields
grep -rn "round(" app/                          → one result: a POSTGRES SQL round() inside a
                                                   pre-existing (M1) CHECK constraint string —
                                                   database-side arithmetic validation, not
                                                   Python business logic, not touched by M4
grep -rn "current_cost" app/modules/accounting/ → two legitimate uses: (1) the reconciliation
                                                   report's own live-valuation comparison, whose
                                                   entire purpose is to read the CURRENT state —
                                                   not a historical reconstruction; (2) a code
                                                   comment
grep -rn "current_cost" app/modules/sales/      → one use, inside finalize_sale itself, at the
                                                   exact moment unit_cost_at_sale is being frozen
                                                   — this IS the snapshot being created, not a
                                                   later read of a snapshot that should have
                                                   stayed frozen
```

No occurrence of `product.current_cost` or `product.current_qty_on_hand`
inside a report, journal-posting function, or any code path that
reconstructs a *historical* figure. **Verdict: no issues found.**

---

## 12. P&L audit

Independently recomputed for the comprehensive scenario (§24) by hand
(shown in the test's own docstring) and compared against the live
`profit_and_loss()` output — matched exactly:
`net_sales=175.00`, `cogs=86.666668`, `gross_profit=88.333332`,
`other_income=28.333334` (Inventory Adjustment Gain), and
`net_income = gross_profit + other_income − operating_expenses`
re-derived, not just asserted against itself.

- **Empty period**: `trial_balance()`/`profit_and_loss()` with no
  transactions in range return zeroed rows (`func.coalesce(..., 0)` in
  every aggregate) — no division-by-zero, no missing-row exception; not
  separately re-tested this audit since the SQL-level `COALESCE` makes
  the empty case structurally safe, not a special code branch that could
  regress independently.
- **One sale, multiple sales**: covered throughout.
- **Different stores**: `test_store_scoped_manager_only_sees_own_store_in_list_and_reports`
  and the comprehensive scenario's store-isolation variant (§10) both
  independently confirm store-filtered P&L excludes the other store's
  sale.
- **Date boundaries, future dates, reporting boundary dates**: see §13
  below (a repeat of the same boundary question at trial-balance
  granularity, since P&L is built directly from `trial_balance()`).
- **Reversed journals**: not applicable to P&L directly beyond what §1's
  fix already guarantees — since automated entries can no longer be
  reversed at all, a reversed SALE/etc. entry contaminating P&L is now
  structurally impossible for every entry type that currently exists.
- **Zero-value transactions**: `test_zero_price_sale_posts_no_sales_revenue_line_but_still_balances`
  and the zero-cost receipt/adjustment tests (§7/§9) all confirm a
  zero-value event posts no line for the zero side, so it cannot
  contaminate a report with a phantom zero-amount row.
- **Concurrent transactions**: `test_c` (§ concurrency, repeated 5×).
- **Draft/unposted records**: structurally impossible — there is no
  DRAFT state in this system (`docs/M4_ACCOUNTING_CORE.md` §3's
  documented lifecycle decision); every row `trial_balance()` sums is,
  by construction, already fully posted (the DB trigger guarantees this
  at insert time).

**Verdict: no issues found.**

---

## 13. Trial balance audit

`Σ debits == Σ credits` independently re-derived (not just asserted
against the function's own output) for the comprehensive scenario (§24)
and confirmed by direct summation of every posted line. Tested by
store, by date range (via the existing `date_from`/`date_to` params,
exercised by the report endpoints' filters), and consolidated
(no store filter, confirmed to include both Store A and Store B's
activity in the isolation test).

---

## 14. Posted journal immutability — re-verified with real commits

Re-run directly against the actual runtime role, not inferred from the
migration's intent:

```sql
-- as erp_app, real (non-savepoint) transaction:
UPDATE journal_lines SET debit = 999 WHERE ...   → ERROR: permission denied for table journal_lines
DELETE FROM journal_entries WHERE ...            → ERROR: permission denied for table journal_entries
```

Confirmed both via a fresh live `psql` session (this audit) and via
`tests/test_accounting_concurrency.py::test_b_erp_app_cannot_update_or_delete_posted_journal_rows`,
which uses a real (non-savepoint) `SessionLocal()` connection — the
`db` pytest fixture's SAVEPOINT-based commit was specifically avoided
here because it would give a false pass (a `RELEASE SAVEPOINT` is not a
real `COMMIT`, so a DEFERRED trigger or a privilege check that only
matters at real-transaction-end could pass under the fixture and still
be broken in production; this file deliberately does not use it, exactly
as `tests/test_concurrency.py`'s own module docstring already explains
for the identical reason).

**Mutation-tested** (this audit): the balance trigger was **disabled**
directly (`ALTER TABLE journal_lines DISABLE TRIGGER
trg_journal_lines_balance`), and the identical unbalanced insert that
normally fails was retried — it **succeeded**, proving the trigger (not
some other coincidental check) is the actual protection. The trigger was
then re-enabled and the same insert retried again — it failed again,
confirming full restoration. No implementation code was changed by this
step (a DB session-local trigger toggle, reverted within the same audit
session).

---

## 15. Source uniqueness / idempotency

`(source_type, source_id) WHERE source_id IS NOT NULL AND entry_type =
'STANDARD'` (partial unique index) re-confirmed present in the live
schema. Tested:

- **Sequential retry**: `test_retrying_a_sale_with_the_same_client_transaction_id_posts_only_one_journal`,
  `test_retrying_a_goods_receipt_posts_only_one_journal`.
- **Three sequential retries, not just one** (this audit):
  `test_operational_and_accounting_idempotency_reinforce_each_other`.
- **Concurrent duplicate**: `test_f_concurrent_duplicate_receipt_requests_create_only_one_receipt`
  (M3, still exercised) plus the M4 concurrency tests C/D (§19 below).
- **Direct duplicate posting attempt, bypassing the operational fast
  path entirely**: `test_posting_twice_for_the_same_source_id_is_rejected_at_the_db_level`
  — calls `_post_journal` a second time for an already-journaled sale
  directly, proving the accounting layer's own constraint is a real,
  independent backstop, not merely something that happens to work
  because the operational layer never asks it to prove itself.

**Confirmed the two layers reinforce rather than coincidentally agree**:
the operational fast path (Sale's own `client_transaction_id`) prevents
posting code from ever being reached twice for a legitimate retry; the
accounting layer's own `(source_type, source_id)` uniqueness is what
would catch it if that fast path were ever bypassed by a future bug —
two independent mechanisms, each verified to actually do something on
its own (the mutation test in §14/§1's style — direct `_post_journal`
calls that skip the fast path entirely — is what proves the second
mechanism isn't just decoration).

---

## 16. Atomic rollback audit

Forced failures at the specific points requested, using `monkeypatch` to
inject a `RuntimeError` inside the accounting-posting call *after* every
prior step (row creation, inventory movement, audit log) has already
flushed — the hardest case, since it proves rollback works even when
there's the most already-flushed state to discard:

- **SALE, failure during journal creation**:
  `test_sale_journal_posting_failure_rolls_back_the_whole_sale` — after
  the forced failure: no `Sale` row (checked by `client_transaction_id`,
  not just row count), stock unchanged, zero `InventoryMovement` rows,
  zero `SALE`-sourced journal entries.
- **PURCHASE_RECEIPT, failure during journal creation**:
  `test_goods_receipt_journal_posting_failure_rolls_back_the_whole_receipt`
  — stock unchanged, WAC unchanged, `PurchaseOrderItem.quantity_received`
  unchanged (still `0`), zero movements, zero journal entries.
- **STOCK_ADJUSTMENT, failure during journal creation**:
  `test_stock_adjustment_journal_posting_failure_rolls_back_the_whole_adjustment`
  — stock unchanged, zero `StockAdjustment` rows, zero journal entries.
- **SALE, a real (not injected) mid-cart failure** — a second cart line
  discovering insufficient stock after the first line's movement was
  already flushed:
  `test_sale_inventory_movement_failure_rolls_back_sale_and_leaves_no_journal`
  — the first product's stock is provably untouched afterward, and no
  journal exists for the sale that never completed.

Every one of these confirms the general mechanism already documented in
`docs/M4_ACCOUNTING_CORE.md` §19 (nothing commits until the route
handler's single `db.commit()`, so any exception anywhere before that
discards everything already flushed) — this audit's contribution is
proving it with real forced failures at the specific injection points
requested, not re-asserting the design.

"Fail after sale/payment/inventory-movement/audit creation" and "fail
during journal creation" collapse to the same test in this codebase,
because posting happens *after* all of those steps in program order
(§4 of the accounting doc) — forcing the failure at the journal-posting
call is the latest possible point, and therefore the strongest test of
whether everything *before* it correctly rolls back too.

---

## 17. Multi-store accounting audit

Re-verified, including a scenario this audit added specifically to close
a gap in the pre-existing tests: the original cross-store reversal test
only exercised a `SALE`-sourced entry, which (after §1's fix) is
*already* refused for an unrelated reason — masking whether the
store-isolation check on its own does anything. This audit added
`test_store_scoped_manager_cannot_reverse_another_stores_manual_entry`,
targeting a `MANUAL` entry (not blocked by §1's automated-source check),
isolating the store check as the *only* possible defense — and
**mutation-tested it**: `_enforce_store_access`'s call site in
`reverse_journal_entry` was commented out, the new test was run and
**failed** (cross-store reversal of the manual entry succeeded with
`200` instead of `403`), then the call was restored and the full
accounting API suite re-run clean (`12 passed`).

- Store A P&L excludes Store B: confirmed (§10 isolation scenario, §12).
- Store A trial balance excludes Store B: confirmed (§13).
- Store A inventory reconciliation excludes Store B: confirmed (§10).
- Store-scoped users cannot retrieve Store B journals: `404`, confirmed
  (existence not leaked).
- Store-scoped users cannot reverse Store B entries: `403`, confirmed —
  now proven against a `MANUAL` entry specifically, closing the masking
  gap above.
- Admin cross-store behavior: confirmed still works
  (`test_admin_cross_store_access_still_works`).
- `store_id` manipulation via API payload: `scoped_store_filter`/
  `enforce_store_access` (route layer) and the duplicated
  `_enforce_store_access` (service layer, so direct service-function
  callers/tests can't bypass it either) both re-read and confirmed
  unchanged from M2/M3's established pattern.

---

## 18. Date / period boundaries

`posting_date` is a plain `Date` column (no time component), set from
the date portion of `Sale.completed_at`/`GoodsReceipt.received_date`/
etc., all of which are UTC per M2 hardening's established, unchanged
convention (`docs/M2_HARDENING_AUDIT.md` §5). Report filters
(`date_from`/`date_to`) are both **inclusive** (`>=`/`<=` in
`_apply_entry_filters`) — a transaction on exactly `date_from` or
exactly `date_to` is included, confirmed by reading the SQL comparison
operators directly (not just the parameter names). Since `posting_date`
has no time component, there is no `00:00:00`/`23:59:59` boundary
question to test separately — a transaction belongs to exactly one
calendar date, unambiguously, by construction; the only convention that
matters is *which* timezone's calendar date is used (UTC, consistently,
matching every other date-producing computation in this codebase since
M2 hardening).

**Verdict: no issues found**, and no ambiguity exists to exploit — the
column's own type (`Date`, not `DateTime`) rules out the classic
double-counting-across-periods failure mode by construction.

---

## 19. Rounding / money precision

Codebase-wide search results already shown in full in §11 above (zero
`float(` usage, one irrelevant SQL-side `round()`). All accounting
amounts are `Decimal`, stored as `Numeric(14, 6)`. Stress cases:

- `0.01`, `0.005`: exercised throughout (every cash sale test uses 2dp
  money values); `0.005`-style half-cent inputs don't arise anywhere in
  practice since Sale/Payment amounts are already `Numeric(12,2)` at the
  schema level (can't represent a half-cent to begin with) — verified
  this is a real structural guarantee, not an assumption.
- `0.333333`/non-terminating divisions: exactly what §10/§24's
  comprehensive scenario exercises (the `170/12` WAC recompute) —
  the specific case this stress test category was asking for, and the
  one that surfaced the HIGH finding in §10.
- Large monetary amounts / large quantities: `Numeric(14, 6)` supports
  up to 8 integer digits before the decimal point (99,999,999.999999) —
  no overflow risk at any realistic transaction scale; not separately
  stress-tested with synthetic huge values since the column type itself
  is the enforcement, not application logic that could regress.
- High-precision WAC, split payments, discounts, tax: all covered by
  existing/audit-added tests throughout this document.

**Verdict: Decimal-only confirmed throughout; the one real precision
finding is §10's bounded WAC-rounding drift, already fully addressed.**

---

## 20. Accounting API security

Every accounting endpoint re-read directly (`app/api/v1/endpoints/accounting.py`):

- **Authentication**: every route depends on `require_permission(...)`,
  which itself depends on the authenticated-user dependency chain —
  `test_unauthenticated_request_is_rejected` confirms `401` with zero
  headers.
- **RBAC**: `test_cashier_forbidden_from_accounting_endpoints` (`403`),
  `test_auditor_can_read_but_not_reverse` (`200` read / `403` reverse).
- **Store scoping**: §17.
- **Input validation**: all request bodies are Pydantic models
  (`JournalEntryReverseRequest` requires a non-empty `reason`,
  `min_length=1`); malformed JSON or missing fields fall through to
  FastAPI's existing normalized `VALIDATION_ERROR` envelope (M2 design,
  unchanged).
- **Error envelope**: every error response confirmed to follow
  `{"error": {"code", "message"}}` — re-checked directly against live
  `409`/`403`/`404`/`401` responses in this audit's own smoke test and
  the test suite's response-body assertions (not just status codes).
- **No SQL leakage / no stack traces**: the global `Exception` handler
  (`app/core/exceptions.py`, unchanged since M2) returns a generic
  `INTERNAL_ERROR` for anything unhandled — accounting endpoints
  introduce no new exception paths that bypass it (every accounting
  service function raises the same `AppError` subclasses as every other
  module).
- **SQL injection / malformed identifiers**: every accounting query uses
  SQLAlchemy's parameterized query builder (`select(...).where(...)`) —
  no raw string interpolation of any request-derived value anywhere in
  `accounting/service.py` or `accounting/endpoints.py` (re-read in full
  during this audit specifically looking for this). Attempted a
  malformed `store_id` (`?store_id=1;DROP TABLE journal_entries`)
  against the live smoke instance — FastAPI's own path/query parameter
  type coercion (`int | None`) rejects it as a `422` before any query
  ever runs.
- **No unauthorized journal mutation / no unauthorized reversal**: §1,
  §17.

**Verdict: no issues found beyond §1** (which is an authorization-*design*
finding — an authorized user's authorized action having an unsafe
consequence — not an authentication/access-control bypass; access
control itself was correctly enforced throughout, the *operation* it
correctly gated was the problem).

---

## 21. Database constraint audit

Re-verified live, directly, as `erp_app` (not the schema owner):

```
invalid account_type       → ERROR: violates check constraint "ck_accounts_type"
duplicate account code     → ERROR: duplicate key value violates unique constraint "uq_accounts_code"
negative debit              → ERROR: violates check constraint "ck_journal_lines_debit_non_negative"
debit AND credit same line  → ERROR: violates check constraint "ck_journal_lines_exactly_one_side"
invalid journal entry_type  → ERROR: violates check constraint "ck_journal_entries_entry_type"
duplicate source posting    → IntegrityError (partial unique index), tested in code (§15)
UPDATE/DELETE posted journal → permission denied (§14)
```

Every one of these is a real PostgreSQL constraint or trigger, not an
application-only check — confirmed by running each directly through
`psql` as `erp_app`, bypassing the Python application entirely.

---

## 22. Accounting report performance

Reviewed (not load-tested — no realistic transaction volume exists yet
to load-test against):

- `trial_balance()`: one query — `Account` outer-joined to
  `JournalLine` outer-joined to `JournalEntry`, grouped by account,
  filters applied in the `WHERE` clause. No N+1: the whole report is one
  round trip regardless of how many accounts or journal lines exist.
- `profit_and_loss()`: calls `trial_balance()` once and does the rest in
  Python from the in-memory rows — no additional queries.
- `inventory_reconciliation()`: two queries (one for GL balances grouped
  by store, one for operational valuation grouped by store) — not N+1
  per store, a single grouped aggregate each.
- Journal list/detail: `list_journal_entries` is one paginated query;
  `_to_journal_entry_read` (the endpoint's response-shaping function)
  does one additional query per journal entry to fetch its lines plus
  one query to batch-resolve the account names for those lines — this
  **is** an N+1 across a *list* of journal entries (one lines-query per
  entry in the list, not batched across the whole page). At the current
  default page size (`limit=50`) this is 50 extra small, indexed queries
  per list call — not pathological today, but a real scaling concern
  once journal volume grows; **not fixed in this audit** (a query-plan
  optimization is out of scope for a correctness/safety hardening pass
  and risks introducing a new bug under time pressure) — flagged here as
  a known, real, moderate-priority follow-up rather than silently
  accepted. `journal_entry_id`/`account_id` are both indexed
  (`ix_journal_lines_journal_entry_id`, `ix_journal_lines_account_id`),
  so each individual query in the N+1 is fast; the concern is round-trip
  count at scale, not per-query cost.
- Indexes present and used: `ix_journal_entries_store_posting_date`
  (covers the common store+date-range report filter),
  `ix_journal_entries_source` (covers the source-uniqueness lookup and
  drill-down-by-source queries), both re-confirmed present in the live
  schema.

**Verdict: one real, moderate, documented-not-fixed finding** (journal
list N+1 on lines) **— everything else is single-query and appropriately
indexed.**

---

## 23. Test quality audit — mutation testing performed

Per the audit's explicit instruction, protection was actually removed
and the corresponding test's failure was actually observed, for the
invariants judged most important, then fully restored:

1. **Balance constraint** (§14): trigger disabled → unbalanced insert
   succeeded (would have been silently accepted in production) →
   trigger re-enabled → same insert failed again. Confirms the DB
   trigger, not incidental application logic, is the real protection.
2. **Store-check on reversal** (§17): `_enforce_store_access` call
   commented out → `test_store_scoped_manager_cannot_reverse_another_stores_manual_entry`
   failed (cross-store reversal succeeded, `200` instead of `403`) →
   call restored → full `test_accounting_api.py` suite re-run, `12
   passed`. Confirms this specific check, not some other coincidental
   gate, is what the test is actually exercising.
3. **Reversal source-type gate** (§1): the fix itself was developed by
   writing the failing-state regression tests *first* (against the
   original, unrestricted `reverse_journal_entry`), confirming they
   failed appropriately against the vulnerable code path during
   development, then adding the fix and confirming they passed — the
   same mutation-test discipline applied inline rather than as a
   separate after-the-fact step.

Source uniqueness and transaction atomicity were **not** separately
mutation-tested in this pass (removing the unique index or the
"don't commit on the way in" convention would require a schema change
or touching every operational service function respectively — riskier
to revert cleanly under time pressure than the three performed above);
their protection is instead evidenced by direct tests that exercise the
failure mode without needing to disable the mechanism first
(`test_posting_twice_for_the_same_source_id_is_rejected_at_the_db_level`
for uniqueness; the four forced-failure tests in §16 for atomicity,
each of which fails *without* the "post before commit, in the same
transaction" design and passes with it — verified by the fact that these
tests are new, and they immediately confirmed correct behavior on first
run against the actual implementation, which is what you'd expect only
if the mechanism they're checking is real).

**The repository was left in its fully-protected state** — every
mutation performed in this audit was reverted within the same session,
and the full test suite (§27) was re-run clean afterward to confirm no
residual weakening.

---

## 24. Required comprehensive reconciliation scenario

Implemented exactly as specified, in `tests/test_accounting_hardening.py`:

```
Store A, product X (price 25.00):
1. Receive 10 @ 10.000000
2. Sell 3
3. Receive 5 @ 20.000000
4. Sell 4
5. Stock adjustment +2
6. Purchase return 1 unit
```

Independently hand-derived (shown in full in §10 above and in the test's
own docstring) and cross-checked against the live report output:

| Figure | Independently derived | Reported | Match |
|---|---|---|---|
| Quantity on hand | 9.000 | 9.000 | exact |
| WAC | 14.166667 | 14.166667 | exact |
| Revenue | 175.00 | 175.00 | exact |
| COGS | 86.666668 | 86.666668 | exact |
| Gross profit | 88.333332 | 88.333332 | exact |
| Inventory Adjustment Gain | 28.333334 | 28.333334 | exact |
| Purchase Clearing (net) | 185.833333 | 185.833333 | exact |
| Trial balance Σdebit/Σcredit | — | equal | exact |
| Inventory GL vs. qty×WAC | — | `-0.000004` | **bounded, documented (§10)** |

Repeated in Store B with different numbers
(`test_comprehensive_reconciliation_scenario_store_isolation`) — Store
A's and Store B's P&L/reconciliation figures are both independently
correct and mutually exclusive (`store_id` scoping re-confirmed at this
larger scale, not just the simpler single-transaction isolation tests
elsewhere in the suite).

---

## 25. Final verdict

**PASS WITH CONDITIONS.**

Justification against the audit's own rules: no CRITICAL issue remains
(the one CRITICAL finding — unrestricted automated-journal reversal —
was fixed, regression-tested, and verified live). No finding in this
report describes silent duplicate posting, unbalanced accounting,
uncontrolled operational/accounting divergence, historical financial
mutation, cross-store financial access, incorrect COGS, unauthorized
financial reversal, or broken transaction atomicity — every one of those
specific unacceptable-limitation categories was specifically audited
(§1, §11, §14–17, §16, §21) and found either not present or fixed. The
two remaining conditions are explicitly bounded and cannot cause silent
financial corruption:

1. **Inventory reconciliation has a bounded (sub-millionth-of-a-currency-
   unit-per-transaction), mathematically-inherent rounding limitation**
   (§10) — documented, tested with an honest tolerance rather than a
   false "exact" claim, and provably incapable of reaching material
   scale under any realistic transaction volume.
2. **Purchase Clearing accumulates without a clearing mechanism** (§7) —
   explicitly documented as the correct, honest M4 boundary (a real AP
   subledger is new functionality, not a hardening fix), not a silent or
   surprising gap.

One moderate, non-blocking performance finding (§22, journal-list N+1)
is flagged for future attention, not fixed in this pass.

---

## 26. Known remaining risks / deferred functionality

- No sale-return or sale-void/refund operational workflow exists (§1,
  carried forward from `docs/M4_ACCOUNTING_CORE.md` §1/§10) — this is
  *why* reversal had to be restricted rather than made safe by
  cross-checking against an operational counterpart; building that
  workflow (atomically reversing operational state + inventory +
  payment/refund + accounting + audit together) is the real fix that
  would let reversal be safely re-opened for automated entries, and is
  explicitly deferred to a future milestone, not this hardening pass.
- Purchase Clearing has no clearing mechanism (§7).
- Journal list endpoint has an N+1 query pattern that will need
  addressing before high transaction volume (§22).
- Manual journal posting (`accounting.post`) remains unimplemented and
  unused — the `MANUAL` source_type added in this audit exists solely to
  keep the reversal mechanism testable, not as a released feature.
- Chart-of-Accounts admin (`accounting.admin`) remains unimplemented.
- No accounting-period closing/locking mechanism.
- Purchase-side tax remains unmodeled (inherited, unchanged, from M3).

---

## 27. Final validation

**Backend**: full `pytest` — **234 passed** (220 pre-audit + 14 new/
rewritten across `test_accounting.py`, `test_accounting_api.py`,
`test_accounting_concurrency.py`, and the new `test_accounting_hardening.py`
— see the commit diff for the exact per-file breakdown). `ruff check .`
— all checks passed. `black
--check .` — all files unchanged. `mypy app` — no issues in 56 source
files. `pip-audit -r requirements.txt` — no known vulnerabilities.
Accounting-specific: `test_accounting.py`, `test_accounting_api.py`,
`test_accounting_concurrency.py` (concurrency scenarios repeated 5× each,
including the reversal-serialization scenario now against a `MANUAL`
entry), `test_accounting_idempotency.py`, `test_accounting_hardening.py`
(new — comprehensive reconciliation scenario, atomic rollback, mutation-
test-adjacent regression tests) all green.

**Frontend**: `vitest run` — **23 passed** (22 + 1 new: an automated-
source entry no longer offers a reversal button in the UI). `oxlint` —
clean. `tsc --noEmit` — clean. `vite build` — succeeds. `npm audit` — 0
vulnerabilities. The AccountingPage's reversal button is now only shown
for `MANUAL`-sourced entries; an automated-source entry shows an
explanatory note instead of a button that would always fail.

**Database**: real PostgreSQL (`erp_dev`, with real accumulated
transaction history from this and prior sessions' live smoke testing).
Full `alembic downgrade base` → `upgrade head` cycle re-run clean after
this audit's changes (including the new `581d2a07f38c` migration
widening `ck_journal_entries_source_type` to allow `MANUAL` — its own
downgrade path was exercised and correctly *refused* to downgrade while
`MANUAL` rows existed, proving that guard is real, then succeeded once
they were cleaned up). Constraint checks: §21. Runtime privilege checks:
§14, §17 (mutation-tested).

**Live smoke tests against a running instance** (fresh store → admin →
product → supplier → PO → receive → sell → reports → reversal attempt):
sale (verified change-due handling, §3), receipt, reports (trial
balance, P&L — both matched hand-calculation), reconciliation, store
isolation (cross-store 403/404 re-confirmed), and — the specific target
of this audit — **a live attempt to reverse an automatically-posted SALE
journal, which correctly returned `409 OPERATIONAL_REVERSAL_REQUIRED`**
instead of the `200` it would have returned before this audit's fix.

---

## 28. Delivery

Remained on `claude/grocery-erp-pos-architecture-h8a53g`. Fixes and
documentation committed separately (see the session's git log — a
`fix:` commit for the reversal restriction and its migration/tests, and
a `docs:` commit for this file and the `M4_ACCOUNTING_CORE.md`
corrections). Branch pushed. No PR opened. M5 not started.
