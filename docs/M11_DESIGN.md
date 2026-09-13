# M11 — Management Analytics & Reporting: Design

Companion to `docs/M10_HARDENING_AUDIT.md` (M10 closed PASS WITH
CONDITIONS; those conditions are documented, do not block M11, and are
not reopened here). This document is written and approved before any
M11 application code exists.

## 0. What already exists (read before designing anything)

A source-of-truth trace across the whole codebase, done before writing
a line of this document, found that a meaningful slice of "M11" already
exists:

- `GET /accounting/reports/trial-balance`, `GET
  /accounting/reports/profit-loss`, `GET
  /accounting/reports/inventory-reconciliation`
  (`app/api/v1/endpoints/accounting.py` →
  `accounting/service.py::trial_balance/profit_and_loss/inventory_reconciliation`).
- `GET /ap/aging`, `GET /ap/reports/ap-reconciliation`, `GET
  /ap/reports/purchase-clearing-reconciliation`, `GET
  /ap/suppliers/{id}/summary`, `GET /ap/suppliers/{id}/transactions`,
  `GET /ap/suppliers/{id}/statement` (`app/api/v1/endpoints/ap.py` →
  `ap/service.py`).
- `reports.read` permission (seeded in M2, granted to Manager and
  Auditor) with no module consuming it yet.
- `ReportsPage.tsx` and `DashboardPage.tsx` on the frontend, both
  explicit placeholders ("Sales, inventory, and P&L summary widgets
  will appear here in later milestones").

M11's job is therefore **not** to invent financial reporting from
scratch. It is to (a) fill the genuine gaps — sales/profitability,
inventory turnover/shrinkage/slow-moving/transfers, purchasing
spend/delivery performance, labor/payroll analytics, a KPI dashboard,
drill-down, and multi-store/company-wide aggregation — and (b) make the
existing GL reports reachable at those same store/company scopes,
**without reimplementing their arithmetic**. Every new report in this
milestone either calls an existing service function directly or
aggregates directly from the same authoritative tables those functions
already read — never a second, parallel computation of a number that
already has one authoritative source.

## 1. Analytical architecture

**No second accounting truth, no data warehouse.** M11 adds one new
backend module, `app/modules/reports/`, containing read-only query
functions and, for exactly one report (Section 11), a documented
materialized view. Every other report is a live SQL aggregation over
the existing authoritative tables, executed at request time:

- **Financial reports** (Trial Balance, P&L, Revenue/COGS/Expense
  summary, Balance Sheet summary, AP aging/reconciliation, Purchase
  Clearing reconciliation) call the **existing** `accounting/service.py`
  and `ap/service.py` functions. Where those functions only accept one
  `store_id: int | None`, they gain one new, additive, optional
  parameter, `store_ids: Sequence[int] | None`, with `store_id`
  preserved unchanged and every existing call site/test untouched (see
  Section 2 for exactly why this is additive, not a redesign).
- **Everything else** (sales/profitability, inventory analytics,
  purchasing/supplier analytics beyond AP, labor/payroll analytics) is
  new SQL aggregation in `app/modules/reports/service.py`, reading
  directly from `Sale`/`SaleItem`/`SaleReturn`/`Payment`,
  `InventoryMovement`/`Product`, `PurchaseOrder`/`PurchaseOrderItem`/
  `GoodsReceipt`/`GoodsReceiptItem`, `InterStoreTransfer*`,
  `StockCount*`/`StockAdjustment`, and `PayrollPeriod`/
  `PayrollEmployeeResult`/`PayrollEarningLine`/`PayrollDeductionLine`/
  `AttendanceRecord`/`EmploymentAssignment`. All aggregation
  (`SUM`/`COUNT`/`GROUP BY`) happens in PostgreSQL via SQLAlchemy Core
  `select(func.sum(...))...group_by(...)` — never by loading rows into
  Python and summing in application code.

**Why not a data warehouse / star schema for M11**: every domain this
milestone reports on already has a normalized, indexed, authoritative
table with a `store_id` and a date/timestamp column. A star schema
would duplicate that data, introduce a refresh pipeline, and create the
exact "can it silently diverge" risk the task instructions warn
against — with no query the live tables can't already answer at the
data volumes a single-company grocery ERP produces (see Section 9 for
the one report, and only that one, where a snapshot is justified).

## 2. Why the `accounting`/`ap` service extension is additive, not a redesign of M0-M10

`trial_balance`, `profit_and_loss`, `inventory_reconciliation`
(`accounting/service.py`) and `ap_aging`, `ap_reconciliation`,
`purchase_clearing_reconciliation` (`ap/service.py`) currently accept
`store_id: int | None` (`None` = every store). Company-wide reporting
for an **unrestricted** user already works today by passing `None` —
no change needed there. The one genuine gap is an **explicit subset**
of stores (e.g., "these 3 of our 10 stores"), which the current single
`store_id` parameter cannot express.

The fix is to add `store_ids: Sequence[int] | None = None` alongside
the existing parameter in each function's signature, applied as
`JournalEntry.store_id.in_(store_ids)` (or the domain-appropriate
equivalent) when provided, leaving every existing code path — every
existing test, every existing endpoint call with only `store_id` — byte
-for-byte unchanged. This is the same category of change M9 and M10
themselves made repeatedly to earlier milestones' modules (M9 added
columns to `products`/`purchase_orders`; M10 added two new posting
functions to `accounting/service.py` with zero deletions) and which the
M10 closure gate explicitly confirmed as acceptable, additive extension
rather than "reopening" a milestone. Nothing about the accounting
engine's *arithmetic*, its immutability guarantees, or its existing
single-store/all-store behavior changes.

## 3. Store and company authorization scoping (the core new primitive)

The current RBAC model has **exactly two** store-authorization levels,
confirmed by re-reading `app/modules/auth/service.py` and every
existing report endpoint:

- **Store-scoped**: `CurrentUser.store_id` is a specific store. Can
  only ever see that one store's data.
- **Unrestricted**: `CurrentUser.store_id is None` (Admin, or any role
  hired without a store). Can see every store.

There is **no existing concept of a user assigned to an explicit
subset of stores** (e.g., a regional manager over 3 of 10 stores).
Introducing that would mean changing the `User`/auth model — reopening
M0-M2, which this milestone must not do. M11 does not invent it either.
Instead, M11 introduces the query-and-authorization *machinery* for an
explicit store subset, correct today for both ends of the existing
spectrum, and ready for a future subset-assignment feature without
requiring one:

```python
def resolve_authorized_store_ids(
    current_user: CurrentUser, requested_store_ids: list[int] | None
) -> list[int] | None:
    """None returned = no restriction (company-wide, every store).
    A list returned = exactly these stores, every one of them already
    authorized. Never returns a list containing a store the caller
    cannot access — fails closed with ForbiddenError instead."""
    if current_user.store_id is None:
        return requested_store_ids  # None -> all stores; a list -> that exact subset
    if requested_store_ids is None:
        return [current_user.store_id]
    if any(sid != current_user.store_id for sid in requested_store_ids):
        raise ForbiddenError(..., error_code="STORE_ACCESS_DENIED")
    return [current_user.store_id]
```

This lives in `app/modules/reports/service.py` (report-specific; it is
not a general-purpose auth primitive other modules need, so it does not
belong in `auth/service.py`) and is the **only** place a report
resolves "which stores." Every report function in this module takes
the *resolved* list (or `None`), never a raw client-supplied
`store_id`/`store_ids` — mirroring the existing
`scoped_store_filter`/`enforce_store_access` split exactly. A
store-scoped user who explicitly requests another store's ID gets a
403, not a silent drop — matching this codebase's established
fail-closed convention (`enforce_store_access` raises; it does not
quietly filter).

**Known limitation, stated plainly**: because no subset-assignment role
exists today, "selected multiple stores, not all" is only meaningfully
exercised by an unrestricted user narrowing their own view — there is
no role today that is *authorized for* a proper subset and *denied* the
rest. Section 15 Session G tests both ends of what the current model
actually supports (single-store denial of another store; unrestricted
arbitrary-subset and all-stores) and documents this gap rather than
fabricating a role to paper over it.

## 4. Dimensions

| Dimension | Source | Notes |
|---|---|---|
| Store | `store_id` on every authoritative table | Every report is filterable by the resolved store list |
| Date/time | Domain-appropriate event timestamp (below) | Always the event's own timestamp column, never `created_at` where a more meaningful business date exists |
| Product | `Product.id`/`sku`/`name` | Via `SaleItem.product_id`, `InventoryMovement.product_id`, `PurchaseOrderItem`/`GoodsReceiptItem` |
| Category | `Product.category_id` → `ProductCategory` | Nullable on `Product`; uncategorized products form their own "Uncategorized" bucket, never silently dropped |
| Payment method | `Payment.payment_method` (sales) / `SupplierPayment.payment_method` (AP) | Two distinct enums, never conflated (mirrors `accounting/constants.py`'s own two separate mapping dicts) |
| Supplier | `Supplier.id`/`name` | Via `PurchaseOrder`/`PurchaseInvoice`/`SupplierPayment`/`SupplierCreditNote` |
| Employee / Department / Position | `hr` module effective-dated tables | "Current" = row with `effective_to IS NULL` at the report's `as_of` date, never a cached flag |
| Account | `Account.code`/`name`/`account_type` | Chart of accounts, Section 4 of `M4_ACCOUNTING_CORE.md` |

## 5. Measures and their exact definitions

### 5.1 Sales & profitability (Phase 3)

All measures below are computed from `Sale`/`SaleItem`/`SaleReturn`/
`SaleReturnItem`/`Payment` directly (never from the GL — the GL's
`ACCOUNT_SALES_REVENUE` credit total is the reconciliation *check*
against these, per Section 8, not itself the source).

The date dimension for a sale is `Sale.completed_at` (the moment it was
finalized — an `OPEN` sale has no `completed_at` and is excluded from
every measure below by construction). The date dimension for a return
is `SaleReturn.created_at` (no `completed_at` field exists on
`SaleReturn`; a return is created already final — there is no
"OPEN return" state, confirmed by `SaleReturn`'s model docstring).

**Verified against the actual running code, not the schema's aspirational
CHECK constraint**: `app.modules.sales.service.void_sale` calls
`create_sale_return` with `_is_void=True`, but that flag **only**
changes the audit-log action string (`"VOID_COMPLETED"` vs.
`"SALE_RETURN_COMPLETED"`) — it never sets `Sale.status` to `'VOIDED'`.
A 100%-voided sale ends at `status='REFUNDED'`, **identical** to an
ordinary full customer return, and `Sale.voided_by`/`voided_reason` are
never populated by any current code path. `'VOIDED'` is a schema value
with **no producer anywhere in this codebase today** — a pre-existing
M1-M5 characteristic (confirmed by grep and by a failing test written
against the originally-assumed behavior, then corrected), not something
M11 may "fix," since that would mean changing M1-M5's own service code.

This does not create a financial-safety gap: a void nets to **exactly
zero** revenue through the same returns-netting math as an ordinary
full return, so nothing is double-counted either way — the property
that actually matters ("do not double-count returned or voided
transactions") holds regardless of the unused status value. What it
does mean is that `Sale.status` alone cannot distinguish "the cashier
voided this same-day" from "the customer returned it three weeks
later." The **only** reliable signal for that distinction is the audit
trail `create_sale_return` itself writes
(`audit_logs.action='VOID_COMPLETED'`, `entity_type='sale_return'`,
`entity_id=SaleReturn.id` — indexed, append-only) — offered as a
supplementary, clearly-labeled breakdown of the returns total (`void_count`/
`void_amount`), never as a second, additive bucket (a void is a return,
not returns-plus-something).

| Metric | Formula | Treatment of edge cases |
|---|---|---|
| Gross sales | `SUM(SaleItem.line_total + SaleItem.discount_amount - SaleItem.tax_amount)` for `Sale.status IN ('COMPLETED','REFUNDED','PARTIALLY_REFUNDED')` | Every non-`OPEN` sale that was ever finalized counts in gross, including one later fully voided/returned — the reversal is netted out below, never by excluding the sale itself (there is no reliable `Sale`-level signal to exclude a void by, per the note above) |
| Discounts | `SUM(SaleItem.discount_amount)` over the same status filter | Header-level `Sale.discount_total` is the maintained rollup and must equal this; a reconciliation check (Section 8) proves it |
| Returns | `SUM(SaleReturnItem.unit_price_refunded * quantity - discount_refunded + tax_refunded)` joined through `SaleReturn.sale_id` to `Sale`, **filtered to `Sale.status != 'VOIDED'`** (defensive — see below) | The `!= 'VOIDED'` filter is dead code against today's data (nothing ever sets that status) but is kept as a forward-compatible guard, cheaper to keep than to argue should be removed; the mutation test in Section 17 documents it as "not currently load-bearing" rather than claiming it protects something it does not |
| Void breakdown | `void_count`/`void_amount`: same returns query, additionally joined to `audit_logs` on `action='VOID_COMPLETED' AND entity_type='sale_return' AND entity_id=SaleReturn.id` | Informational subset of Returns, not summed into any other total — reported so "how many of today's returns were actually voids" is answerable, honestly labeled as audit-trail-derived rather than a structured transactional field |
| Net sales | Gross sales − Returns | By construction, a fully-refunded-or-voided sale nets to exactly zero, never negative from this alone |
| Tax | `SUM(SaleItem.tax_amount)` (gross sales period) minus `SUM(SaleReturnItem.tax_refunded)` (non-void returns) | Tax is never included in "net sales" — sales tax is a collected-on-behalf-of liability, not revenue (matches `ACCOUNT_TAX_PAYABLE`'s own treatment) |
| COGS | `SUM(SaleItem.quantity * SaleItem.unit_cost_at_sale)` for the same non-void statuses, minus `SUM(SaleReturnItem.quantity * SaleReturnItem.unit_cost_refunded)` for **`restock = true`** returns only | A non-restocked (damaged) return's inventory never came back, so its COGS is never reversed — this exactly matches the accounting posting rule in `sales/service.py::create_sale_return`, so operational COGS and GL COGS reconcile (Section 8) |
| Gross profit | Net sales − COGS | |
| Gross margin % | Gross profit / Net sales, **0 when Net sales = 0** | Never divide by zero (Section 16 item 16) |
| Transaction count | `COUNT(DISTINCT Sale.id)` for non-void, non-`OPEN` sales | A sale with a partial return is still one transaction |
| Units sold | `SUM(SaleItem.quantity)` (non-void statuses) minus `SUM(SaleReturnItem.quantity)` (non-void-sale returns) | Net units, matching net sales' treatment |
| Average transaction value | Net sales / Transaction count, **0 when count = 0** | |
| By store / product / category / payment method / day | Same formulas, `GROUP BY` the dimension | Payment-method breakdown sums `Payment.amount` per method per sale in scope — a split-tender sale contributes to multiple methods, summing back to `Sale.grand_total` |

**Zero-price sales** (a `SaleItem` with `unit_price_at_sale = 0`, e.g. a
promotional give-away) are **included** in units sold and transaction
count, contribute $0 to gross/net sales, and their (non-zero) COGS
still reduces gross profit — this is deliberate: a $0 sale is a real
inventory-reducing, margin-reducing event, not a non-event, and hiding
it would understate shrinkage-via-giveaway.

**Reversed accounting entries**: a `JournalEntry` reversal
(`reverse_journal_entry`, `MANUAL`/`ACCOUNTING_REVERSE`-gated) never
touches `Sale`/`SaleItem` — the operational sales dashboard is entirely
independent of the GL and is never affected by a manual journal
reversal. Manual reversals are visible only in the financial reports
(Section 5.2), which read `JournalLine` directly and therefore see
whatever a reversal's own balanced pair contributes (net zero for the
reversed entry, by the accounting engine's own invariant).

### 5.2 Financial reporting (Phase 4)

- **Trial Balance / P&L**: unchanged output of the existing
  `accounting/service.py` functions, reachable at store-list scope
  (Section 2).
- **Revenue / COGS / Expense summary**: new thin wrappers around
  `trial_balance()`'s own rows, grouped by `account_type` — no new
  arithmetic, a presentation slice of the same trial balance rows.
- **Balance sheet summary**: **Asset and Liability sections only.**
  This system has no Equity/Retained-Earnings account (deliberately not
  seeded — confirmed in `accounting/constants.py`'s own module
  docstring: "nothing in M4 produces a transaction that would ever post
  to them"). A true balance sheet cannot exist without an equity side
  that balances Assets − Liabilities. M11 reports Assets and
  Liabilities from the trial balance's own account-type grouping,
  explicitly labeled "Balance Sheet Summary (Assets & Liabilities —
  no Equity account exists in this system)" — not "Balance Sheet," to
  never imply a completeness this system's chart of accounts does not
  have. This is a known limitation (Section "Known limitations"), not
  silently glossed over.
- **Cash/payment-method summary**: new — sums `Payment.amount` by
  `payment_method` (operational) side by side with each method's own
  GL clearing-account balance from `trial_balance()` (financial) —
  Section 8 defines the reconciliation between them.
- **Store-level and company-wide P&L**: `profit_and_loss()` called once
  per resolved store id and once with the full resolved list (or
  `None`) for the company total — never independently re-derived.
- **Comparative period**: the report endpoint accepts a second,
  optional `(date_from, date_to)` pair and calls the same underlying
  function twice, returning both periods' figures and the delta —
  never a "trend" computed by a different, parallel method.
- **Posted vs. non-posted, made explicit everywhere**: every financial
  report is, by construction, built only from `JournalEntry`/
  `JournalLine` rows — which exist **only** once something is posted
  (there is no "draft journal entry" concept anywhere in this system).
  So a financial report can never include unposted data by accident.
  The risk runs the other way: an operational event that **hasn't
  posted yet** (e.g., a `CALCULATED` but not yet `POSTED` payroll
  period, a `DRAFT` purchase invoice) is real and true operationally
  but invisible to every financial report. Every financial report
  response therefore carries an explicit `as_of`/`generated_at`
  timestamp and, where the underlying domain has a meaningful
  "pending" bucket (payroll, AP), the corresponding operational report
  (Section 5.4/5.6) surfaces that pending state side by side, labeled
  as **not yet reflected in the financial reports above** — never
  silently merged into one number.

### 5.3 Inventory analytics (Phase 5)

- **Inventory value / stock quantity**, by store/category/product:
  `SUM(Product.current_qty_on_hand * Product.current_cost)` /
  `SUM(Product.current_qty_on_hand)`, `GROUP BY` the dimension. This
  cache is itself reconciled against the movement ledger (Section 8) —
  M11 reads the cache for speed but the reconciliation check proves it
  hasn't drifted, exactly mirroring how `inventory_reconciliation()`
  already treats it as reconciled-not-assumed.
- **Inventory movement summary**: `SUM(quantity_delta)`/`COUNT(*)`
  grouped by `movement_type`, `store_id`, date (from `created_at`,
  since `InventoryMovement` has no separate business-date column — it
  is itself the immutable, timestamped ledger entry).
- **Stock adjustment / shrinkage**: `StockAdjustment` filtered to
  `reason_code IN ('DAMAGE','THEFT','EXPIRY','STOCKTAKE_CORRECTION','OTHER')`,
  summed by reason and store. "Shrinkage" specifically = adjustments
  with a **negative** `quantity_delta` (stock found *missing*) — a
  positive adjustment is a *gain*, never counted as shrinkage (matches
  `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` vs
  `ACCOUNT_INVENTORY_ADJUSTMENT_GAIN`'s own separation).
- **Inventory turnover** = COGS (Section 5.1, for the period) /
  average inventory value (opening + closing value from
  `InventoryMovement.resulting_quantity_on_hand` and cost at those two
  dates, ÷ 2). **Undefined (`null`, not `0` or `∞`) when average
  inventory is zero** — a product with zero average stock has no
  meaningful turnover, and reporting `0` would misleadingly imply "does
  not sell" rather than "was never stocked."
- **Days inventory on-hand** = 365 / turnover, same zero/undefined
  guard, and additionally capped/labeled "N/A" when turnover itself is
  `null`.
- **Slow-moving**: products with `current_qty_on_hand > 0` and zero (or
  below a configurable threshold, default 1 unit) `SALE`-type
  `InventoryMovement` rows in the trailing N days (parameterized,
  default 90) — a deterministic, explainable rule, not a scored
  estimate.
- **Stockouts**: products with `current_qty_on_hand = 0` (or
  `allow_negative_stock` and a negative value — see next item) at
  report time, restricted to `is_active = true` products (a
  discontinued product at zero stock is not a stockout).
- **Negative-stock detection**: products with `current_qty_on_hand < 0`
  — only possible where `Product.allow_negative_stock = true` (a DB
  `CHECK` constraint already forbids negative stock otherwise,
  confirmed in `products/models.py`) — reported as its own explicit
  exception list, since a negative on-hand quantity is inherently an
  operational anomaly worth surfacing even where explicitly allowed.
- **Transfer activity / in-transit**: `InterStoreTransferLine.shipped_quantity
  - received_quantity` (per line, summed per store-pair/product) is the
  in-transit quantity; valued at `unit_cost_at_shipment` (frozen at
  ship time), matching `ACCOUNT_INVENTORY_IN_TRANSIT`'s own valuation
  exactly (Section 8 reconciles the two). A transfer's source-store
  quantity is reduced at ship time and the destination's is increased
  only at receipt — in-transit stock is **never** counted as on-hand at
  either store, and never double-counted at both (Section 16 item 10).
- **Stock-count variance**: `StockCountLine.counted_quantity -
  expected_quantity` for `StockCount.status = 'POSTED'` only (a
  `DRAFT`/`OPEN`/`COUNTED`/`REVIEWED` count has not yet produced a real
  adjustment and must not appear as if it has); `counted_quantity IS
  NULL` (not yet counted) is excluded from variance, never treated as a
  variance of `-expected_quantity`.

### 5.4 Purchasing & supplier analytics (Phase 6)

- **Purchase spend**: `SUM(GoodsReceiptItem.quantity_received *
  GoodsReceiptItem.unit_cost)` — the actual received cost, not the PO's
  estimated `unit_cost` (which may differ) — by supplier/store/product,
  date = `GoodsReceipt.received_date`.
- **Received / outstanding PO quantities**:
  `PurchaseOrderItem.quantity_received` vs. `quantity_ordered -
  quantity_received`, excluded entirely for `PurchaseOrder.status =
  'CANCELLED'`.
- **Supplier delivery performance**: days between `PurchaseOrder.order_date`
  and each `GoodsReceipt.received_date` for that order, averaged per
  supplier — an explainable, directly-traceable metric, not a scored
  rating.
- **Purchase price variance report**: a new grouped view over the
  already-posted `ACCOUNT_PURCHASE_PRICE_VARIANCE` journal lines
  (created by `ap/service.py` at invoice-posting time) — reuses the
  existing GL posting as its source, never independently recomputing
  "invoiced price vs. receipt cost" outside of what accounting already
  posted.
- **AP outstanding / aging / Purchase Clearing balance / supplier
  statement**: the existing `ap/service.py` functions, reachable at
  store-list scope (Section 2) — not reimplemented.

### 5.5 Labor & payroll analytics (Phase 7)

Source: `PayrollPeriod`/`PayrollEmployeeResult`/`PayrollEarningLine`/
`PayrollDeductionLine` for **`PayrollPeriod.status = 'POSTED'`
periods only** — a `DRAFT`/`OPEN`/`CALCULATED`/`APPROVED` period's
numbers are not yet a final, authoritative payroll outcome (they can
still be recalculated/changed) and are excluded from every payroll
*financial* metric. A separate, clearly labeled "pending payroll"
count (periods not yet posted, by status) is offered alongside, never
merged into the posted totals.

- **Headcount / active employees / by store**: `COUNT` of
  `EmploymentStatusPeriod` rows with `status = 'ACTIVE'` and
  `effective_to IS NULL` as of the report date, joined to the
  employee's current `EmploymentAssignment.store_id` (same "current
  row" convention as Section 4).
- **Attendance / overtime hours**: `PayrollEmployeeResult.regular_hours`/
  `overtime_hours` for posted periods (already the authoritative,
  calculated figure — not re-summed from raw `AttendanceRecord` rows,
  which would risk disagreeing with what was actually calculated and
  posted).
- **Payroll gross / deductions / net / cost by store**:
  `PayrollPeriod.total_gross`/`total_deductions`/`total_net_pay`
  (store-level, since `PayrollPeriod` is itself per-store — M10
  decision #1) summed across the resolved store list for a company
  total.
- **Employer/labor cost**: gross pay **plus**
  `PayrollDeductionLine.amount` where `is_employer_contribution = true`
  — this is exactly and only what the existing payroll engine
  represents (Wage & Salary Expense + Employer Contribution Expense);
  reported as "Labor Cost (per existing payroll engine — excludes any
  statutory employer obligation not modeled in this system)" so it is
  never mistaken for a complete, jurisdiction-correct total employer
  cost.
- **Payroll cost as % of sales**: payroll cost (above) / net sales
  (Section 5.1) for the same store(s) and date range — **the payroll
  period's own `period_start`/`period_end` is the date range used**,
  not `posted_at`, so the percentage compares like-for-like operating
  periods.
- **Explicit statutory disclaimer, on every payroll report response**:
  a fixed, non-configurable label — *"Deductions shown are the
  configurable amounts recorded in this system's payroll engine. No
  statutory tax, social-security, or other jurisdiction-specific
  formula is calculated or implied."* Every column derived from
  `DeductionType`/`DeductionRate` additionally states that
  configuration's own effective-date range in the response (per the
  operational-risk note in this milestone's brief) so a report is never
  read as if the deduction rule was permanent or store-specific when it
  is in fact global and effective-dated.

## 6. Time semantics

- Every date/timestamp used above is the domain's own authoritative
  event column (Section 5), never `created_at` where a more meaningful
  business date exists (`completed_at` for sales, `received_date` for
  receipts, `posting_date` for journal entries, `work_date` for
  attendance, `period_start`/`period_end` for payroll).
- All stored timestamps are UTC (`DateTime(timezone=True)`), per this
  project's established convention. **M11 does not introduce any new
  local-date conversion.** A `date_from`/`date_to` filter is compared
  directly against the stored UTC date/timestamp column, exactly as
  every existing report (`trial_balance`, `ap_aging`, etc.) already
  does — no store-timezone shifting is applied to sales, purchasing, or
  financial date filters, matching the existing, documented gap noted
  in `M2_HARDENING_AUDIT.md` ("`stores.timezone` is not yet wired into
  ... date comparisons") rather than silently inventing a fix for a
  different milestone's known limitation. The one exception is
  attendance/labor reporting, which reuses `AttendanceRecord.work_date`
  exactly as HR computed it (already store-timezone-correct via
  `resolve_work_date`) — M11 never recomputes a work date itself.
- An empty date range (`date_from == date_to`) is a valid one-day
  report, not an error. `date_from > date_to` is rejected with a 422
  validation error before any query runs. An unbounded range (both
  omitted) is allowed and means "all time" — Section 12 addresses its
  performance cost.

## 7. Store/company aggregation

Covered in full in Section 3. Summary: every report accepts an
optional store filter; the resolved store list (Section 3's
`resolve_authorized_store_ids`) is what every query actually filters
on; "company-wide" for an unrestricted caller means the resolved list
is `None` (no filter) or the literal list of every store, and the two
must produce identical totals (tested in Session G).

## 8. Financial reconciliation rules

| Reconciliation | Compares | Tolerance |
|---|---|---|
| Sales revenue | Operational gross sales (5.1) vs. `ACCOUNT_SALES_REVENUE` net credit for the same store/date range | Exact (`Decimal` equality) — both derive from the same frozen `SaleItem` values `post_sale_journal` already used |
| Discounts | Operational discounts (5.1) vs. `ACCOUNT_SALES_DISCOUNTS` net debit | Exact |
| Tax | Operational tax (5.1) vs. `ACCOUNT_TAX_PAYABLE` net credit | Exact |
| COGS | Operational COGS (5.1) vs. `ACCOUNT_COGS` net debit | Exact |
| Inventory value | `Σ(current_qty_on_hand × current_cost)` vs. `ACCOUNT_INVENTORY` GL balance | Reuses the **existing** `inventory_reconciliation()` — exact to the ledger quantum `Decimal('0.000001')`, exposed, never silently corrected (existing behavior, unchanged) |
| In-transit | `Σ(shipped_quantity − received_quantity) × unit_cost_at_shipment` vs. `ACCOUNT_INVENTORY_IN_TRANSIT` GL balance | **Correction found during implementation**: this reconciliation already existed — `transfers.service.inventory_in_transit_reconciliation` (M8). M11 wraps it verbatim rather than recomputing it (a duplicate was written first, caught by checking the OpenAPI schema for an already-registered `/transfers/reports/...` path, and replaced). Deliberately company-wide only, no store filter — matching M8's own documented reasoning that this account represents value *between* stores, not within one |
| AP | Operational open-invoice balance vs. `ACCOUNT_ACCOUNTS_PAYABLE` | Reuses the **existing** `ap_reconciliation()` |
| Purchase Clearing | Reuses the **existing** `purchase_clearing_reconciliation()` | Existing tolerance, unchanged |
| Payroll | `PayrollPeriod.total_net_pay` (posted) vs. `ACCOUNT_PAYROLL_PAYABLE` net credit | Exact, new — mirrors the inventory/AP pattern exactly |

**Rule, stated once and applied everywhere**: a discrepancy is
**reported**, in a dedicated exception/reconciliation view, never
silently adjusted by the reporting layer. If a real discrepancy is
ever found in this codebase's actual data, that is a genuine defect to
fix at the source (the posting function), not something M11 papers
over with a "corrected" number.

## 9. Inventory reconciliation rules

Covered fully in Sections 5.3 and 8. The one additional rule not
already stated: **a stock count in any status other than `POSTED` never
appears in any valuation or reconciliation figure** — only a posted
count has produced a real `StockAdjustment`/`InventoryMovement`/GL
effect; a `COUNTED`/`REVIEWED` count is a draft under review and must
not be mistaken for a settled position.

## 10. Labor-cost rules

Covered fully in Section 5.5. Restated once, plainly, because it is
the single most important rule in this document given M10's own
disclosed risk: **every payroll analytics response is computed only
from `POSTED` `PayrollPeriod` rows, and states the effective-date scope
of any global `DeductionRate`/`OvertimePolicy` row that contributed to
those figures.** M11 does not change `DeductionRate`/`OvertimePolicy`'s
approved global scope — it only reports on the consequences of that
scope transparently, per the explicit instruction in this milestone's
brief.

## 11. Query performance strategy

- Every report is a single (or small, fixed number of) grouped
  aggregation queries — no N+1 (a report over 50 stores issues one
  query with `GROUP BY store_id`, not 50 queries).
- New indexes added where a report's filter/group-by isn't already
  covered by an existing index (audited in Section "Migration
  strategy" below — most needed indexes already exist, e.g.
  `ix_sales_store_created`).
- **One materialized view, justified**: `daily_sales_summary`
  (`store_id, sale_date, gross_sales, discounts, returns, net_sales,
  tax, cogs, gross_profit, transaction_count, units_sold`), grouped by
  store and calendar date. Justification: the sales trend-by-day report
  (Phase 3) and the KPI dashboard (Phase 8) both need day-granularity
  history over potentially long ranges (a year or more) on every page
  load, and recomputing that from raw `SaleItem`/`SaleReturnItem` rows
  for a long range on every dashboard view is the one place this
  milestone's own performance testing (Session J) is expected to show a
  real cost. It is a **derived, reproducible, read-only artifact**:
  - Refreshed by `REFRESH MATERIALIZED VIEW CONCURRENTLY
    daily_sales_summary`, triggered by a lightweight FastAPI
    `BackgroundTasks` call **after** `finalize_sale`/`create_sale_return`
    commit (never before — never inside the same transaction as the
    operational write, so a refresh failure can never roll back a real
    sale), refreshing only the **affected `(store_id, sale_date)` row(s)**
    via a scoped `DELETE ... ; INSERT ... SELECT` inside the view's
    refresh function rather than a full-view rebuild, keeping the
    refresh cost proportional to one day's data, not the whole
    history.
  - **Cannot silently diverge**: every trend/KPI endpoint that reads
    this view also exposes a `reconcile=true` query parameter that
    instead computes the identical live aggregation directly from
    `SaleItem`/`SaleReturnItem` for the requested range and returns
    both figures plus their delta — the exact reconciliation-check
    pattern already established for `inventory_reconciliation()`. A
    scheduled or manually-triggered full `REFRESH MATERIALIZED VIEW
    daily_sales_summary` (non-concurrent, full rebuild) is also
    provided as an operator escape hatch if the incremental refresh
    is ever suspected of drifting — never a reason to trust the view
    blindly.
  - No other report in this milestone uses a materialized view or any
    other snapshot/cache table — every other report is Section 1's
    live aggregation, because none of them showed the same
    "long-range, every-page-load" access pattern that justifies the
    added complexity here.

## 12. Authorization / store isolation

Section 3 is the authorization design. Enforcement rule, restated: the
route layer resolves the authorized store list via
`resolve_authorized_store_ids` **before** calling any service function;
every `reports/service.py` function takes only the already-resolved
list, never a raw client parameter — so even a bug in one report
function's own filtering can't leak past an already-correct
resolution, and (mirroring the M10 mutation-testing precedent) each
report's authorization is tested at **both** the route layer and the
service layer independently, per Phase 10's explicit requirement.

## 13. Handling of reversed/voided/returned transactions

Fully specified per-domain in Section 5. Single cross-cutting rule:
**every "excluded" state (`VOIDED` sale, `CANCELLED` PO/transfer/stock
count, `DRAFT`/non-posted invoice or payroll period) is excluded by an
explicit `status`/`state` filter in the query itself, never by relying
on the absence of a row** — because in this schema, a voided/cancelled
record's row and its line items still exist (nothing is deleted), so a
query that forgot the filter would silently double-count rather than
silently under-count, which is the more dangerous failure mode and
exactly what Section 17's mutation tests are built to catch.

## 14. Handling of POSTED versus non-posted accounting

Fully specified in Section 5.2 (financial reports) and repeated for
payroll in Section 5.5. Single rule, stated once for all domains: a
financial report is built exclusively from `JournalLine`/`JournalEntry`
rows, which exist only for posted transactions — so "non-posted" data
can only ever be *missing* from a financial report, never wrongly
included. Every operational report that has a meaningful non-posted
state (payroll, AP invoices, stock counts) surfaces that pending state
as an explicitly separate, clearly labeled figure alongside the
financial one, never blended into it.

## 15. Indexing strategy

Audited against existing indexes before adding anything new:

| Table | Existing index | New index needed for M11 |
|---|---|---|
| `sales` | `ix_sales_store_created (store_id, created_at)` | Add `ix_sales_store_completed (store_id, completed_at)` — the sales dashboard filters/groups by `completed_at`, not `created_at` |
| `sale_items` | `ix_sale_items_sale_id`, `ix_sale_items_product_id` | None — product/category rollups join through these |
| `sale_returns` | `ix_sale_returns_sale_id`, `ix_sale_returns_store_id` | None |
| `inventory_movements` | (existing, per M1) indexed on `store_id`/`product_id` | None found missing |
| `purchase_order_items`, `goods_receipt_items` | existing FKs indexed | None found missing |
| `payroll_employee_results` | existing FK indexes | Add `ix_payroll_periods_store_status (store_id, status)` on `payroll_periods` — every payroll analytics query filters on both |
| `attendance_records` | existing | None — HR already indexes what M11 needs |
| `journal_entries` | `ix_journal_entries_store_posting_date`, `ix_journal_entries_source` | None — the exact index every financial report already relies on |

Every new index is added via a normal Alembic migration (Section 16),
never as an ad hoc `CREATE INDEX` outside migration history.

## 16. Migration strategy

M11 requires one migration:

1. Two new indexes (above).
2. The `daily_sales_summary` materialized view (Section 11) plus its
   scoped refresh function.
3. **No changes to any existing table's columns, constraints, or
   privileges** — no new columns on `sales`/`products`/etc., and
   critically, **no change to any existing append-only privilege
   grant**. The materialized view itself is populated by a
   `SECURITY DEFINER`-free plain SQL function running as the migration
   owner/schema role at refresh time (not `erp_app` directly avoiding
   any need to grant new write privileges to the restricted runtime
   role beyond what a normal `REFRESH MATERIALIZED VIEW` already
   requires, which PostgreSQL grants to the view's owner regardless of
   `erp_app`'s table-level restrictions — never bypassing or weakening
   the existing `journal_entries`/`inventory_movements`/etc. immutability
   grants).
4. Downgrade: drops the view, its refresh function, and the two new
   indexes — **fully non-destructive to any existing table's data**,
   since the view is entirely derived and the indexes carry no data of
   their own. No downgrade guard is needed (nothing irreplaceable is
   ever created by this migration), unlike M8/M9/M10's guards, which
   exist because those milestones' downgrades would otherwise drop
   tables holding irreplaceable transactional data. Verified per
   Phase 14's exact test list (fresh, M10→M11, M11→M10, populated
   upgrade/downgrade/re-upgrade).

## 17. Testing strategy

Follows this project's own established discipline exactly (mirrors the
M9/M10 hardening-pass structure): targeted unit tests per metric
definition, dedicated reconciliation tests proving the exact-match
claims in Section 8, RBAC/mutation tests proving every authorization
check in Section 3/12 is load-bearing (not just correct-today),
concurrency tests using real independent connections (never the `db`
fixture's savepoint isolation) for read-during-write scenarios, failure
injection proving a forced failure never mutates transactional data
(reports are read-only, so this also proves a broken report can never
corrupt anything, not only that it fails safely), and a migration
round-trip test. Full session breakdown in this milestone's hardening
audit (`docs/M11_HARDENING_AUDIT.md`, produced after implementation).

## 18. Known limitations (stated up front, not discovered late)

1. **No statutory payroll compliance** — by design, per M10 decision
   #8 and repeated here: nothing in M11 computes or implies a
   jurisdiction-specific tax/social-security formula.
2. **No true balance sheet** — no Equity/Retained-Earnings account
   exists in this system's chart of accounts; M11's "balance sheet
   summary" is Assets and Liabilities only, explicitly labeled as such.
3. **No native multi-store-subset RBAC role** — Section 3's
   authorization machinery is correct and tested for both ends of the
   *current* two-level model; a genuine "authorized for stores 3 and 7
   only" role does not exist today and is not created by M11.
4. **Store-timezone-naive date filtering outside HR** — matches the
   pre-existing, documented M2 gap; M11 does not fix or paper over it.
5. **`daily_sales_summary` is the only report backed by a
   snapshot/materialized artifact**; every other report is always
   computed live, so it can never be "stale" in the way a cached report
   can — the tradeoff is query cost at very large date ranges, which
   Section 12/Phase 11's performance testing measures and documents
   rather than papering over with an untested cache.
6. **Purchase price variance reporting only covers invoices that have
   actually posted** — an outstanding, unposted invoice's eventual
   variance is not predictable from this report.
7. **Supplier delivery performance is a simple average lead time**, not
   a weighted or statistically adjusted score — deliberately, per this
   milestone's instruction against "AI-like" unexplained scores.

---

This design was reviewed against every M0-M10 design/hardening
document before being written and found no architectural contradiction
requiring a change to M0-M10 — Section 3's authorization gap and
Section 5.2's missing-equity limitation are both real, but both are
resolved by building correctly-scoped-for-today machinery and stating
the limitation, not by reopening any prior milestone. Implementation
proceeds from this document without a stop.
