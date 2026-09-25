# M22 Discovery: Accounting Integrity and Financial-Period Control

**Status:** Discovery complete. Starting HEAD: `953a33f`. No production code changed by
this document — implementation follows only where this discovery establishes a
business rule (Phase 10).

**Scope guardrail (per the M22 brief):** M22 is not a rewrite of M21_DISCOVERY.md's
findings F2 ("P&L excludes labor"), F3 ("transfer loss/cancellation gap"), and F4
("no period-close"). It is a narrow, evidence-first slice of the accounting domain.
Each of the 5 KNOWN FINDINGS in the brief is evaluated independently below; nothing
here assumes they belong in one implementation.

---

## 0. Business-Policy Questions — resolved before any design work

### Question A: does payroll/labor belong in the official P&L?

**Resolved: YES, by an explicit, pre-existing repository rule.** Not invented here.

Evidence — `docs/M4_ACCOUNTING_CORE.md` Section 13 ("P&L implementation"):

> "No fabricated expense lines — Operating Expenses is exactly the one real expense
> account that exists beyond COGS. A future payroll/rent/utilities milestone adds
> accounts and this formula's Operating Expenses term grows to include them; nothing
> here hardcodes 'the' expense list."

This is M4's own documented design intent, written before payroll (M10) existed: the
`operating_expenses` term was always meant to be "every EXPENSE account beyond COGS,"
computed dynamically, and M4 shipped a one-account version only because M4 shipped
before any other EXPENSE accounts existed. M10 (payroll) and M6/M15 (purchase
variance/discounts, cash over/short) each added real EXPENSE accounts
(`app/modules/accounting/service.py::profit_and_loss` currently only picks up
`5900 Inventory Shrinkage Expense`) without anyone going back to widen the formula —
that is the bug, not an open policy question. **No STOP is required for Question A.**
See Phase 4/Phase 1 below for the exact fix and full account inventory.

### Question B: transfer authorization (F7)

**Not evaluated further — out of scope by the brief's own instruction.** F7
(M21_DISCOVERY.md: whether transfer *creation* requires anything beyond
`inventory.transfer.write`) is a business-policy question the M21 implementation
already searched for an authoritative rule on and found none (see
`docs/M21_TESTING_SESSIONS.md`, "Limitations and intentionally unresolved items").
M22 introduces no new evidence on this question and changes nothing about transfer
authorization. It remains: **BUSINESS DECISION REQUIRED: TRANSFER CREATION
AUTHORIZATION SCOPE** (unchanged from M21).

---

## Phase 1: Accounting architecture audit — every posting path

All ~16 forward-posting functions and all 3 reversal-posting functions in
`app/modules/accounting/service.py` route through exactly **one** low-level
function, `_post_journal(db, *, store_id, posting_date, source_type, source_id,
memo, created_by, lines, entry_type, reversal_of_id)` (lines 188–244), **except**
`reverse_journal_entry` (the generic MANUAL-entry reversal, lines ~1120–1220), which
builds its `JournalEntry` directly as a second, separate code path. This means a
period-lock check added at exactly these **two** call sites covers literally every
way a JournalEntry can be created in this system today. This is the single most
important architectural fact this discovery rests on.

| # | Function | Source type | Operational trigger | `posting_date` source | Backdatable? | Store attribution | Correction path | Audit trail |
|---|----------|-------------|---------------------|------------------------|--------------|--------------------|------------------|-------------|
| 1 | `post_sale_journal` | SALE | `sales.service.finalize_sale` | `sale.completed_at.date()` | No (server-set at finalize time) | `sale.store_id` | Reversal blocked here (AUTOMATED_SOURCE_TYPES) — must go through `create_sale_return`/void, which posts its own new-dated entry | `JournalEntry.source_id` → Sale |
| 2 | `post_sale_return_journal` | SALE_RETURN | `sales.service.create_sale_return` | `return_date` (caller-supplied, defaults today) | Yes | inherited from sale's store | New entry per return; no reversal-of-reversal path needed | source_id → SaleReturn |
| 3 | `post_goods_receipt_journal` | PURCHASE_RECEIPT | `purchasing.service.receive_goods` | `goods_receipt.received_date` | Yes (operational date field) | receipt's store | Reversal blocked (AUTOMATED) — correct via `create_purchase_return` | source_id → GoodsReceipt |
| 4 | `post_purchase_return_journal` | PURCHASE_RETURN | `purchasing.service.create_purchase_return` | `purchase_return.return_date` | Yes | inherited | New entry per return | source_id → PurchaseReturn |
| 5 | `post_purchase_invoice_journal` | PURCHASE_INVOICE | `ap.service` invoice posting | `purchase_invoice.invoice_date` | Yes | invoice's store | `post_purchase_invoice_void_journal` (below) | source_id → PurchaseInvoice |
| 6 | `post_purchase_invoice_void_journal` | PURCHASE_INVOICE_VOID | `ap.service` invoice void | `purchase_invoice.invoice_date` (same date as original — **not "today"**) | Yes | inherited | terminal (void is itself the correction) | source_id → PurchaseInvoice |
| 7 | `post_supplier_payment_journal` | SUPPLIER_PAYMENT | `ap.service` record payment | `supplier_payment.payment_date` | Yes | inherited | `post_supplier_payment_reversal_journal` | source_id → SupplierPayment |
| 8 | `post_supplier_credit_note_journal` | SUPPLIER_CREDIT_NOTE | `ap.service` credit note | `credit_note.credit_date` | Yes | inherited | `post_supplier_credit_note_reversal_journal` | source_id → SupplierCreditNote |
| 9 | `post_supplier_payment_reversal_journal` | SUPPLIER_PAYMENT_REVERSAL | `ap.service` reversal | `datetime.now(UTC).date()` — **always today** | **No** | inherited | terminal | source_id → SupplierPayment |
| 10 | `post_supplier_credit_note_reversal_journal` | SUPPLIER_CREDIT_NOTE_REVERSAL | `ap.service` reversal | `datetime.now(UTC).date()` — **always today** | **No** | inherited | terminal | source_id → SupplierCreditNote |
| 11 | `post_stock_adjustment_journal` | STOCK_ADJUSTMENT | `inventory.service.create_stock_adjustment` | caller-supplied (see line 818, a conditional expression) | Yes | adjustment's store | Reversal blocked (AUTOMATED) — correct via a new adjustment | source_id → StockAdjustment |
| 12 | `post_transfer_shipment_journal` | INTER_STORE_TRANSFER_SHIP | transfer ship | `transfer.shipped_at.date()` or today | Yes | source store | no dedicated reversal (F3, unchanged) | source_id → Transfer |
| 13 | `post_transfer_receipt_journal` | INTER_STORE_TRANSFER_RECEIVE | transfer receive | `transfer_receipt.received_date` | Yes | destination store | no dedicated reversal (F3, unchanged) | source_id → TransferReceipt |
| 14 | `post_payroll_journal` | PAYROLL_POSTING | `payroll.service` post period | `payroll_period.pay_date` | Yes (period's own field, `pay_date >= period_end` DB-constrained, but period itself can be backdated) | period's store | `post_payroll_reversal_journal` | source_id → PayrollPeriod |
| 15 | `post_payroll_reversal_journal` | PAYROLL_REVERSAL | `payroll.service.reverse_payroll_period` | `datetime.now(UTC).date()` — **always today** | **No** | inherited | terminal | source_id → PayrollPeriod |
| 16 | `post_cash_shift_variance_journal` | CASH_SHIFT_VARIANCE | shift close | `shift.closed_at.date()` or today | Yes-ish (tied to real close time) | shift's store | no dedicated reversal | source_id → CashierShift |
| 17 | `reverse_journal_entry` (separate code path, not via `_post_journal`) | MANUAL only | `accounting.reverse` endpoint | `date.today()` — **always today** | **No** | inherited | terminal; refuses to reverse anything in `AUTOMATED_SOURCE_TYPES` | reversal_of_id → original entry |

**The pattern that matters for period control:** every *forward* posting function (1–8,
11–14, 16) uses an operational record's own date field, which is either directly
caller-editable (return/payment/credit-note/invoice dates) or indirectly so (a
backdated `Sale`/`GoodsReceipt`/`PayrollPeriod` whose own date field the operational
workflow allows). Every *reversal/correction* posting function (9, 10, 15, 17) —
without exception — already posts at `date.today()`, never at the original
transaction's date. **Reversals are already period-safe by construction; only
forward postings are the real risk vector.** This was independently confirmed by
`docs/M4_ACCOUNTING_CORE.md` Section 16 (quoted in Phase 2 below), which anticipated
exactly this split when M4 shipped.

One exception worth flagging, not fixing: `post_purchase_invoice_void_journal` (#6)
reuses the *original* invoice's date rather than today's. A void is conceptually a
correction, but it is implemented as a same-shape reversal of the original entry's
lines rather than a `_post_journal`-external reversal — it is already covered by the
same `_post_journal` gate as every forward function, so no special-casing is needed;
noted here only because it breaks the "corrections always post today" generalization
above. It requires no design decision, only correct universal gating.

---

## Phase 2: Accounting-period model design

**Authoritative precedent found, not invented:** `docs/M4_ACCOUNTING_CORE.md`
Section 16, "Accounting periods":

> "Not built in M4, deliberately... What's deferred: there is no mechanism to *lock*
> a period against further posting, and no period-closing workflow that zeroes
> Revenue/Expense into a Retained Earnings equity account (which is also why no
> Retained Earnings account is seeded — nothing would ever post to it). The
> extension point is clean: adding a `closed_periods` table and a check at posting
> time ('is posting_date inside a closed period?') requires no change to the posting
> functions' own logic, only one more validation at their entry point."

This is decisive for scope: M4's own design explicitly separates **(a)** a posting
lock (in scope for M22, unblocked) from **(b)** zeroing Revenue/Expense into Retained
Earnings (blocked on the Balance-Sheet/Equity question — see Phase 5, out of scope
for M22). M22 builds (a) only.

### Model: `AccountingPeriod`

A **closed-period is a denylist entry**, not a full lifecycle state machine. The
absence of a covering row means a date is open — this is deliberately smaller than
`PayrollPeriod`'s multi-state model (DRAFT→OPEN→CALCULATED→APPROVED→POSTED), because
nothing here needs a draft/calculation/approval workflow: closing a period is a
single administrative act, not a multi-person business process.

```python
class AccountingPeriod(TimestampMixin, Base):
    """One CLOSED date range for one store. A posting_date not covered by
    any row here is open by default -- there is no separate "OPEN" state
    to store. See docs/M22_DISCOVERY.md Phase 2."""

    __tablename__ = "accounting_periods"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_accounting_periods_valid_range"),
        # Mirrors EmploymentStatusPeriod's non-overlap pattern (M10): two
        # closed ranges for the same store must never overlap, or "is this
        # date closed" becomes ambiguous.
        ExcludeConstraint(
            (Column("store_id"), "="),
            (func.daterange(Column("period_start"), Column("period_end"), "[]"), "&&"),
            name="ck_accounting_periods_no_overlap",
        ),
        Index("ix_accounting_periods_store_id", "store_id"),
        Index("ix_accounting_periods_range", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    closed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
```

**Design decisions and why each is the smallest defensible choice:**

- **Per-store, not company-wide.** Every existing posting function is already
  store-scoped (`store_id` is a required parameter of `_post_journal`); a
  company-wide period would require a cross-store concept this codebase has never
  needed, and closing one store's books does not imply another's are ready.
- **No reopen.** The brief's own Phase 9 lists "reopen semantics... undefined" as an
  explicit STOP condition. Rather than guess at reopen rules (who can reopen, what
  happens to anything that theoretically could have posted while reopened, whether a
  reopen needs its own approval step), M22 simply does not build reopen — the model
  and endpoint expose *create* only. This is not a business-policy gap needing a
  STOP: it is a scope decision the "smallest coherent slice" instruction directly
  supports, and it costs nothing later (`AccountingPeriod` rows are never mutated, so
  adding a reopen/delete endpoint afterward is additive, not a migration or a
  behavior change to existing rows).
- **No status column.** Every row that exists is closed; there is nothing else to
  encode. This is the literal `closed_periods` table the M4 design doc names.
- **`reason` required, not optional.** Every other financial correction action in
  this codebase (`reverse_journal_entry`, `post_supplier_payment_reversal_journal`,
  etc.) requires a reason string for audit purposes; closing a period is at least as
  consequential and gets the same treatment.
- **Gating permission: `accounting.admin`.** Already defined
  (`app/modules/auth/permissions.py:41`), already documented as "reserved for future
  chart-of-accounts admin," currently granted to **Admin only**
  (`ROLE_PERMISSIONS[ADMIN] = list(ALL_PERMISSIONS)`; no other role holds it — verified
  by reading the full role matrix, not assumed). Reusing it needs no new permission
  code and no role-grant change, and its current Admin-only scope matches
  `fiscal.config.write`'s own precedent for "configuration-level, turns on a new
  constraint for the whole store" actions (see that permission's docstring).

### Enforcement: exactly two call sites

A single helper, `_enforce_period_open(db, *, store_id, posting_date)`, queries for
an `AccountingPeriod` row covering `posting_date` for that store and raises a new
`PeriodClosedError` (subclass of the existing app exception hierarchy, mapped to
**409 Conflict** like every other `ConflictError` in this module — e.g.
`ACCOUNTS_NOT_SEEDED`, `CANNOT_REVERSE_REVERSAL`) if one exists. It is called from:

1. The top of `_post_journal` (line ~200, before `_resolve_accounts`) — covers all 16
   forward-posting functions and 2 of the 3 reversal functions that route through it.
2. Inside `reverse_journal_entry`, immediately before constructing its `JournalEntry`
   directly (line ~1189) — the one code path that bypasses `_post_journal`.

No other call sites exist anywhere in the codebase that create a `JournalEntry` row
(verified in Phase 1's exhaustive trace) — two call sites is complete coverage, not
a starting subset.

---

## Phase 3: Post-close financial integrity

Given the Phase 1 finding that every reversal/correction function already posts at
`date.today()`, the interaction between period-close and corrections falls out
directly, with no new mutation-permitting behavior required:

- **Late-arriving transactions, corrections, returns, AP corrections, supplier
  credits, cash corrections, payroll corrections, inventory adjustments:** each of
  these is either (a) itself a forward posting subject to the same period-open check
  as everything else (a late invoice dated inside a closed period is correctly
  refused — that is the entire point of closing a period), or (b) a
  reversal/void/correction posting that always dates itself "today," which by
  definition cannot fall inside a period that was closed for a *past* range. A
  closed period therefore blocks new backdated postings into it while never blocking
  a same-day compensating entry that corrects a mistake discovered after the fact —
  exactly the "prefer compensating entries over historical mutation" principle the
  brief asks for, and it requires zero new code beyond the two gate call sites: the
  existing reversal architecture already behaves this way.
- **The one sharp edge:** closing *today's* date for a store blocks *all* posting to
  that store for the rest of today, reversals included (since reversals post to
  today too). This is correct, not a bug: a period boundary that didn't also stop
  same-day corrections wouldn't be a boundary. It does mean an admin should not close
  a period that includes today while operations are still running — a documented
  operational caution, not a code gap.
- **No historical mutation path is added or needed.** M22 does not touch
  `sale.completed_at`, `purchase_invoice.invoice_date`, or any other operational
  date field's mutability; those are unchanged by this milestone.

---

## Phase 4: P&L / net-income reconciliation (Question A is resolved — in scope)

Current code (`profit_and_loss`, `app/modules/accounting/service.py:1397`):

```python
operating_expenses = net_debit(ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE)
```

Full EXPENSE-type account inventory (every EXPENSE account ever seeded, across all
migrations — traced directly, not assumed):

| Code | Name | Milestone | Currently in `operating_expenses`? |
|------|------|-----------|--------------------------------------|
| 5000 | Cost of Goods Sold | M4 | No — handled separately in `gross_profit`, correctly excluded here |
| 5100 | Purchase Price Variance | M6 | **No — bug** |
| 5150 | Purchase Discounts (contra) | M6 | **No — bug** |
| 5200 | Purchase Tax Expense (Non-Recoverable) | M6 | **No — bug** |
| 5900 | Inventory Shrinkage Expense | M4 | Yes |
| 5910 | Cash Over/Short | M15 | **No — bug** |
| 6000 | Wage & Salary Expense | M10 | **No — this is Finding F2/Question A** |
| 6100 | Employer Contribution Expense | M10 | **No — bug, same root cause as F2** |

This confirms F2 is a symptom of a more general bug: `operating_expenses` was never
widened past its one M4-era hardcoded account, for *any* of the four milestones that
added EXPENSE accounts since, not only payroll. The fix generalizes rather than
special-cases labor, which is also the least speculative option since it is exactly
what M4 Section 13 already said the term should do:

```python
operating_expenses = sum(
    (
        net_debit(row.account_code)
        for row in rows.values()
        if row.account_type == "EXPENSE" and row.account_code != ACCOUNT_COGS
    ),
    start=Decimal("0"),
)
```

`TrialBalanceRow` already carries `account_type` (line 1303), so this is derived
purely from the same `trial_balance()` rows the function already computes — no
independent calculation, consistent with this file's own "never independently
compute two sides of a pair that must match" rule (module docstring). `ACCOUNT_COGS`
stays excluded because `gross_profit` already subtracts it once; including it again
in `operating_expenses` would double-count it.

**Net-income impact:** this is a real change to a reported number (net income will
decrease once labor and the other four previously-omitted expense accounts are
included), and it is retroactive for any historical `date_from`/`date_to` range
passed to the endpoint — the fix is not itself period-gated, it is a query-time
correction to how existing GL data is summarized. No new JournalEntry rows are
created, no data is mutated; every store that has ever posted payroll, a purchase
variance/discount/tax line, or a cash-over/short line will see its historical P&L
change the next time it queries this endpoint. This is the correct outcome (the old
number was wrong), but it is called out explicitly as a **user-visible reporting
change**, not a silent one, in the Testing Sessions and the final report.

---

## Phase 5: Balance Sheet / Equity — BUSINESS DECISION REQUIRED (unchanged, not built)

`docs/M11_DESIGN.md` (lines 260–268) already documents this as a deliberate,
unresolved gap:

> "Balance sheet summary: Asset and Liability sections only. This system has no
> Equity/Retained-Earnings account... A true balance sheet cannot exist without an
> equity side... explicitly labeled 'Balance Sheet Summary (Assets & Liabilities —
> no Equity account exists in this system)' — not 'Balance Sheet'."

`app/modules/accounting/constants.py` independently confirms Owner Equity/Retained
Earnings was never seeded because "nothing in M4 produces a transaction that would
ever post to them." Nothing found in M6/M7/M10/M15/M20 changes this — no milestone
since has produced an equity-posting transaction either.

Per the brief's explicit instruction ("Do not simply add an Equity account because
the discovery mentioned it"): **M22 does not add an Equity account, does not build a
Balance Sheet, and does not build the Revenue/Expense-zeroing period-closing
workflow** (which requires Equity to post into — see Phase 2's quoted M4 passage).
This is documented as:

> **BUSINESS DECISION REQUIRED: EQUITY / RETAINED-EARNINGS TREATMENT.** No rule
> exists for how Owner's Equity should be modeled (single owner draw account? capital
> contributions? multi-store equity is it consolidated or per-store?), so no period
> "close to Retained Earnings" workflow can be built without inventing one. The
> period-**lock** mechanism in Phase 2 does not require this decision and is built
> independently; the period-**close-to-equity** workflow does, and is not attempted.

---

## Phase 6: Correction/reversal audit matrix

| Operation | Original-period mutation possible? | Reversal/correction available? | Audit trail? | Behavior once period-lock ships | Gap |
|---|---|---|---|---|---|
| Sale (SALE) | No (finalize is terminal) | Via `create_sale_return` (new dated entry) or void | Yes, `source_id` | Return/void still allowed after close if dated today; a *new sale* dated into a closed period is blocked | None |
| Sale return (SALE_RETURN) | No | New return posts fresh; no reversal-of-return | Yes | Same as above | None new |
| Goods receipt (PURCHASE_RECEIPT) | No | `create_purchase_return` | Yes | Blocked if receipt dated into closed period | None |
| Purchase invoice (PURCHASE_INVOICE) | No (once posted) | `post_purchase_invoice_void_journal` (reuses **original** date, not today — see Phase 1 exception) | Yes | **Gap, pre-existing, not created by M22:** voiding a POSTED invoice whose original date now falls in a closed period will itself be blocked (it posts at the original date), with no code path to void it except by reopening the period. Documented, not fixed in M22 — reopen is out of scope (Phase 2), and this is no worse than today's behavior (today there is no period lock at all, so this gap is latent, not new). |
| Supplier payment (SUPPLIER_PAYMENT) | No | `post_supplier_payment_reversal_journal` (today's date) | Yes | Reversal always succeeds regardless of when payment was dated | None |
| Supplier credit note | No | `post_supplier_credit_note_reversal_journal` (today's date) | Yes | Same | None |
| Stock adjustment | No | New adjustment (AUTOMATED, reversal blocked) | Yes | Blocked if new adjustment dated into closed period | None new |
| Transfer ship/receive | No | None dedicated (F3, pre-existing, unchanged by M22) | Yes | Blocked if dated into closed period | F3 unchanged |
| Payroll posting | No | `reverse_payroll_period` → `post_payroll_reversal_journal` (today's date) | Yes | Reversal always succeeds | None |
| Manual entry (rare, no creation endpoint yet) | No | `reverse_journal_entry` (today's date) | Yes | Reversal always succeeds | None |

**The one real gap this matrix surfaces** is the `PURCHASE_INVOICE_VOID` exception
already flagged in Phase 1: it is the single correction-type posting that does *not*
use today's date. It is documented here rather than special-cased, per the brief's
"prefer compensating entries... document the decision required" instruction: fixing
it (making void always post at `date.today()` like every other correction) would be
a behavior change to `ap.service`'s void semantics outside M22's
period-control/P&L slice, and no business rule requires M22 to touch it. It is
flagged as a **remaining risk** in the final report, not silently left as an
undocumented surprise.

---

## Phase 7: Financial invariants (defined before implementation; each will be demonstrated by a named test in M22_TESTING_SESSIONS.md, not merely asserted)

1. **INV-1 (single gate):** Every `JournalEntry` row created anywhere in the system
   passes through `_enforce_period_open` before it is written — either via
   `_post_journal` or via `reverse_journal_entry`'s own call.
2. **INV-2 (no silent bypass):** A posting attempt whose `posting_date` falls inside
   a closed `AccountingPeriod` for its store raises `PeriodClosedError` (409) and
   creates **zero** rows — no partial `JournalEntry` with no lines, no orphaned
   `JournalLine`.
3. **INV-3 (store isolation):** Closing store A's period never blocks posting to
   store B for the same date range (mirrors M21's cross-store isolation discipline).
4. **INV-4 (non-overlap):** Two `AccountingPeriod` rows for the same store can never
   have overlapping date ranges (DB-enforced via `ExcludeConstraint`, not
   application-only).
5. **INV-5 (correction survivability):** A reversal/void whose own posting date is
   `date.today()` succeeds even when the *original* entry's date falls inside a
   closed period (the compensating-entry principle from Phase 3), **except** the one
   documented `PURCHASE_INVOICE_VOID` exception (Phase 6), which is expected to fail
   and is asserted to fail for the documented reason, not silently skipped.
6. **INV-6 (P&L completeness):** `profit_and_loss().operating_expenses` equals the
   sum of `net_debit` over every `EXPENSE`-type account in the chart of accounts
   except `ACCOUNT_COGS` — verified by asserting the computed value against a
   manually-summed trial balance in a test fixture that posts to multiple EXPENSE
   accounts at once (payroll + purchase variance + cash-over/short together).
7. **INV-7 (P&L balances, no double-count):** `gross_profit - operating_expenses +
   other_income == net_income` continues to hold exactly (no rounding drift
   introduced by summing more accounts) after the Phase 4 fix.
8. **INV-8 (no COGS double-count):** Widening `operating_expenses` never includes
   `ACCOUNT_COGS`'s own balance a second time.
9. **INV-9 (period-close audit):** Every `AccountingPeriod` row records a non-null
   `closed_by`, `closed_at`, and `reason` — there is no way to close a period
   anonymously or without a stated reason (DB `NOT NULL`, not just schema validation).
10. **INV-10 (authorization):** Only a caller holding `accounting.admin` can create an
    `AccountingPeriod` row; every other role, including Manager (who holds
    `accounting.reverse` but not `accounting.admin`), is refused.

---

## Phase 8: Testing sessions planned (executed post-implementation; recorded in `docs/M22_TESTING_SESSIONS.md`)

- **A — Period lifecycle:** create a closed period; verify it appears in a list;
  verify non-overlap is DB-enforced.
- **B — Closed-period protection:** attempt each of the 16 forward-posting functions
  (Phase 1 table) with a `posting_date` inside a closed period; assert `409
  PERIOD_CLOSED` and zero rows created (INV-1/2).
- **C — Correction/reversal:** reverse a payment/credit-note/payroll period/manual
  entry whose *original* posting predates a closed period; assert success (INV-5).
  Separately assert `post_purchase_invoice_void_journal` against a closed-period
  invoice *does* fail, and that this is the documented exception, not a regression.
- **D — Accounting reconciliation:** trial balance still balances (`total_debit ==
  total_credit`) with periods closed; period-close creates no journal entries of its
  own (it is a control-plane row, not a GL posting).
- **E — P&L (Question A resolved, in scope):** post payroll + purchase
  variance/discount/tax + cash-over/short in the same date range; assert
  `operating_expenses`/`net_income` reflect all of them (INV-6/7/8).
- **F — Balance Sheet:** explicitly skipped/out of scope (Phase 5) — documented in
  the testing sessions doc, not silently omitted.
- **G — Multi-store accounting:** store isolation for period-close (INV-3), reusing
  M21's established isolation-test patterns.
- **H — Authorization:** `accounting.admin` required for period-close; every other
  role refused (INV-10); direct-service-layer call bypass attempt, mirroring M21
  Session C's "service-layer bypass" methodology.
- **I — Concurrency:** two concurrent postings racing a period-close (does a
  postin-flight transaction at the moment of close ever land inside the just-closed
  range?) — evaluated for applicability once the exact transaction boundaries of the
  new close endpoint are implemented; if not materially different from M21's own
  Session E judgment (no new concurrency surface beyond what `pg_advisory_xact_lock`
  and existing row-level locking already cover), documented as such rather than
  padded with a no-op test.
- **J — Failure injection:** DB error mid-`_enforce_period_open` query leaves no
  partial state (relies on the same uncommitted-transaction-until-route-commit
  pattern already documented in this file's module docstring).
- **K — Migration safety:** new table only, no backfill, no existing-row rewrite —
  additive migration, single new head off `9c4c5a209aa9`.
- **L — Adversarial:** attempt to close a period covering a date that already has
  postings (should succeed — closing is about the *future*, not retroactively
  invalidating what already posted); attempt to close an already-closed overlapping
  range (should fail on the DB constraint); attempt period-close with a missing
  `reason` (should fail on `NOT NULL`).
- **M — Mutation testing:** required targets to be named in
  `docs/M22_TESTING_SESSIONS.md` once implementation is complete (the M21 mutation
  methodology — break the invariant, confirm RED, revert, diff-verify — will be
  reused exactly).

---

## Phase 9: STOP-condition check

Per the brief, M22 must STOP and document (not guess) if any of: P&L labor policy
undefined, period scope undefined, reopen semantics undefined, closed-period
correction behavior undefined, or equity semantics undefined.

| Condition | Status |
|---|---|
| P&L labor policy | **Resolved** (Question A, Phase 0/4) — no STOP |
| Period scope (per-store vs. company-wide, what "a period" means) | **Resolved** (Phase 2, per-store, denylist model) — no STOP |
| Reopen semantics | **Deliberately not built**, not "undefined-and-guessed" — treated as an explicit scope exclusion per "smallest coherent slice," not a business decision requiring a stakeholder answer. No STOP. |
| Closed-period correction behavior | **Resolved** (Phase 3/6 — corrections post today, already period-safe by construction; the one exception is documented, not silently patched over) — no STOP |
| Equity semantics | **STOP.** Documented above (Phase 5) as BUSINESS DECISION REQUIRED. Balance Sheet and period-close-to-equity are not implemented in M22. |
| Transfer authorization (F7, Question B) | **STOP** (carried over from M21, unchanged, out of scope per the brief). |

---

## Phase 10 scope decision — what M22 implements

**In scope (business rules established, evidence-based):**
1. `AccountingPeriod` model + migration (single new head).
2. `_enforce_period_open` gate at the two call sites (`_post_journal`,
   `reverse_journal_entry`).
3. Period-close service function + endpoint, gated by `accounting.admin`.
4. `profit_and_loss()` fix: dynamic `operating_expenses` over all EXPENSE accounts
   except COGS (resolves F2/Question A and the same latent bug for purchase
   variance/discounts/tax and cash-over/short).

**Explicitly not implemented in M22 (no rule established, or out of scope by the brief):**
- Balance Sheet / Equity / Retained Earnings / period-close-to-equity workflow (Phase 5).
- Reopen-a-closed-period capability (Phase 2 — deliberate scope exclusion, not a stop).
- Transfer authorization (F7 / Question B — unchanged from M21).
- The `PURCHASE_INVOICE_VOID` original-date exception (Phase 6 — documented risk,
  not fixed, no rule requires M22 to change AP void semantics).
- Any UI/frontend surface for period administration (a small admin action; whether it
  needs a dedicated screen versus an API-only administrative action is a UX decision
  outside "accounting integrity and financial-period control," and no report/page
  currently surfaces period state that the frontend would need to react to).

This is the smallest slice for which every included piece has an evidence-based rule
behind it, and it leaves every unresolved question (Equity, transfer authorization,
the invoice-void date exception) explicitly documented rather than guessed at or
silently dropped.
