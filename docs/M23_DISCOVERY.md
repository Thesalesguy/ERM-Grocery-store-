# M23 Discovery: Balance Sheet / Equity and Period-Closing Interaction

**Status:** Discovery only. No production code, migration, or test changed.
Starting HEAD: `631c5a5` (M22 final commit).

**Scope guardrail (per the M23 brief):** this document determines *whether, and
exactly how* to build Balance Sheet/Equity reporting and reassesses the M22
period-close model's interaction with it. It does not expand into logistics,
HR UX, analytics, vendor UX, or production infrastructure, and it does not
touch F7 (transfer authorization) or M22's P&L labor-cost decision.

---

## Phase 0: Source audit

Read in full: `docs/M4_ACCOUNTING_CORE.md`, `docs/M11_DESIGN.md` (Sections 5.2,
18), `docs/M11_HARDENING_AUDIT.md` (Section 12), `docs/M22_DISCOVERY.md`,
`docs/M22_TESTING_SESSIONS.md`, `backend/app/modules/accounting/{models,
service,constants}.py`, `backend/app/modules/reports/service.py`'s financial
section, `backend/app/api/v1/endpoints/{accounting,reports}.py`,
`backend/app/modules/auth/models.py::Store`, and every migration that seeds a
chart-of-accounts row (M4, M6, M10, M15).

Repo-wide search (`grep -ril`, case-insensitive where noted) for every term
listed in the brief:

| Term | Hits |
|---|---|
| `equity` | `accounting/constants.py`, `reports/service.py`, `test_reports_financial.py`, `docs/M21_DISCOVERY.md`, `docs/M22_DISCOVERY.md`, `docs/M22_TESTING_SESSIONS.md`, `docs/M11_HARDENING_AUDIT.md`, `docs/M11_DESIGN.md`, `docs/M4_ACCOUNTING_CORE.md` — every hit is a statement that no Equity account exists, never a requirement that one should |
| `retained earnings` | 0 hits outside the same "no Equity/Retained-Earnings account" sentences above — no standalone design discussion exists |
| `accumulated earnings` | 0 hits |
| `opening balance` | 1 hit, `docs/M7_ADVANCED_AP_SETTLEMENT.md` — a **supplier statement's** opening balance (subledger concept, unrelated to GL/equity opening balances) |
| `balance sheet` (any case) | `reports/service.py`, `test_reports_financial.py`, `docs/M21_DISCOVERY.md`, `docs/M7_ADVANCED_AP_SETTLEMENT.md` (unrelated AP-statement usage), `docs/M22_TESTING_SESSIONS.md`, `docs/M11_HARDENING_AUDIT.md`, `docs/M11_DESIGN.md`, `docs/M22_DISCOVERY.md`, `frontend/src/pages/ReportsPage.tsx` |
| `period close` | `docs/M21_DISCOVERY.md` only (the finding that led to M22) |
| `closing entry` | 0 hits |
| `year end` | 0 hits |
| `fiscal year` | 0 hits |
| `net income transfer` | 0 hits |
| `income summary` | 0 hits |
| `retained profit` | 0 hits |
| `owner equity` | 0 hits |
| `capital` | `accounting/constants.py` ("capitalized into Inventory" — WAC terminology, unrelated), `docs/M6_AP_VENDOR_ACCOUNTING.md` (same "capitalized" usage) — no owner's-capital-account discussion anywhere |

**Conclusion:** the repository contains zero design discussion, zero
requirement, and zero prior decision for Retained Earnings, accumulated
earnings, closing entries, fiscal-year boundaries, net-income transfer, an
income-summary account, or owner's capital. The only recurring statement,
repeated verbatim-in-spirit across M4, M11 (design + hardening audit), M21
discovery, and M22 discovery, is that **no Equity account exists and none of
M4–M22 ever produces a transaction that would post to one** — and this
absence is not merely undiscussed, it is a **tested, intentional invariant**:
`backend/tests/test_reports_financial.py::test_balance_sheet_has_no_equity_section_by_design`
asserts `not hasattr(bs, "equity")` outright.

A **partial** Balance Sheet already exists and ships today:
`reports/service.py::balance_sheet_summary` / `GET
/reports/financial/balance-sheet`, surfaced in the frontend under a
"Balance Sheet" sub-tab, computed live from `trial_balance()` (Assets and
Liabilities account types only), explicitly labeled "Balance Sheet Summary
(Assets & Liabilities — no Equity account exists in this system)" in its own
docstring — never presented as a complete balance sheet.

---

## Phase 1: Classification (with repository evidence)

| Item | Classification | Evidence |
|---|---|---|
| Balance Sheet (complete, A = L + E) | **PARTIAL** | An Assets+Liabilities-only version exists, shipped, tested, and honestly labeled (`reports/service.py:671-709`, `test_reports_financial.py:56-66`). Completing it is blocked purely on Equity, which is the next row. |
| Equity account | **UNRESOLVED BUSINESS DECISION** | `account_type` CHECK constraint already permits `'EQUITY'` as a value (seeded in the M4 migration: `"account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')"`) — the schema was built to allow one — but zero EQUITY rows have ever been seeded and no repo document states a business reason to add one now. Three independent milestones (M4, M11, M22) each considered it and each concluded "not yet," but none of them is a stakeholder decision that it is permanently unneeded — each says "nothing produces a transaction that would post to it," a technical observation, not a policy ruling. |
| Retained earnings / accumulated earnings | **UNRESOLVED BUSINESS DECISION** | Zero repository discussion (Phase 0 search). Mechanically dependent on the Equity decision above — cannot be designed before Equity's ownership/structure is decided (see Phase 4). |
| Opening balances | **AUTHORITATIVELY NOT SUPPORTED (implementation), UNRESOLVED (need)** | No migration, seed script, admin endpoint, or service function initializes a non-zero balance for any account. Every store starts at $0 everywhere and only accumulates via real operational transactions (confirmed by tracing every `post_*_journal` function — none is reachable except from a genuine sale/receipt/invoice/etc.). Whether this system will ever need to onboard a store with pre-existing stock/cash is not addressed by any design doc. |
| Period-end closing entries (zero P&L into Equity) | **AUTHORITATIVELY NOT BUILT, entry point deliberately reserved** | `docs/M4_ACCOUNTING_CORE.md` Section 16 (quoted in `docs/M22_DISCOVERY.md` Phase 2) explicitly separates a posting **lock** (built in M22) from a closing workflow that "zeroes Revenue/Expense into a Retained Earnings equity account" — named as future, out-of-M4-scope work, contingent on the still-absent Equity account. |
| Net-income transfer | **UNRESOLVED BUSINESS DECISION** | Same dependency as retained earnings — no mechanism, no design discussion, blocked on Equity. |
| Fiscal-year boundary | **AUTHORITATIVELY NOT DEFINED** | Zero hits anywhere in the repo for "fiscal year." `AccountingPeriod` (M22) has no concept of a year boundary — it is an arbitrary admin-chosen date range per store, not tied to any calendar/fiscal convention. |
| Permanent vs. temporary accounts | **PARTIAL, implicit not explicit** | The *behavior* already exists and is correct: `profit_and_loss()` is period-scoped (`date_from`+`date_to`, temporary-account semantics — see Phase 3) while `balance_sheet_summary()` is cumulative-to-date (`date_to` only, no `date_from` — permanent-account semantics). Nothing in the code or docs names this distinction explicitly; it is a byproduct of how each report was independently built, not a designed "permanent vs. temporary account" model. |
| Period reopening | **AUTHORITATIVELY NOT BUILT (M22's own explicit scope decision, not a gap)** | `docs/M22_DISCOVERY.md` Phase 2 documents this as a deliberate exclusion — "smallest coherent slice" plus the brief's own STOP condition on undefined reopen semantics — not an oversight. |
| Post-close corrections | **AUTHORITATIVELY DEFINED for every path except one** | Every reversal/void function *except* `post_purchase_invoice_void_journal` posts at `date.today()` and is therefore period-safe by construction (verified exactly in Phase 6 below). The one exception is analyzed in Phase 7 and is itself classified **UNRESOLVED BUSINESS/ACCOUNTING-POLICY QUESTION**. |

---

## Phase 2: Current account taxonomy and the accounting equation

Every account ever seeded (M4 + M6 + M10 + M15 — the complete set, traced
directly from `accounting/constants.py` and cross-checked against every
migration that inserts into `accounts`):

| account_type | Accounts (code — name) |
|---|---|
| ASSET | 1000 Cash on Hand, 1010 Card Clearing, 1020 Mobile Money Clearing, 1030 Bank Transfer Clearing, 1040 Other Payment Clearing, 1050 Bank Account, 1500 Inventory, 1520 Inventory In Transit |
| LIABILITY | 2000 Purchase Clearing (AP), 2010 Accounts Payable, 2100 Tax Payable, 2200 Payroll Payable, 2210 Statutory Withholding Payable, 2220 Benefit/Other Deduction Payable, 2230 Employer Contribution Payable |
| EQUITY | **none seeded** |
| REVENUE | 4000 Sales Revenue, 4100 Sales Discounts (contra), 4900 Inventory Adjustment Gain |
| EXPENSE | 5000 COGS, 5100 Purchase Price Variance, 5150 Purchase Discounts (contra), 5200 Purchase Tax Expense, 5900 Inventory Shrinkage Expense, 5910 Cash Over/Short, 6000 Wage & Salary Expense, 6100 Employer Contribution Expense |

25 accounts total, zero EQUITY rows.

**Does the schema structurally support Assets = Liabilities + Equity?**
**Partially, and asymmetrically.** The `account_type` CHECK constraint already
accepts `'EQUITY'` — adding an Equity account needs no schema/migration
change, only an `INSERT` (or an admin-managed create path, since
`accounting.admin` exists and Accounts are otherwise migration-seeded,
"admin"-only reference data per `constants.py`'s own docstring). What is
missing is entirely on the *application* side: no function ever computes an
amount to post *into* Equity, and `reports/service.py::balance_sheet_summary`
only ever queries `account_type == "ASSET"` / `"LIABILITY"` — an `"EQUITY"`
branch would need to be added there too. Today, `total_assets` and
`total_liabilities` are computed and returned side by side with **no
assertion or expectation that they are equal** — and they are not: every
dollar of historical net income accumulated since a store's first
transaction is embedded, unlabeled, inside the Asset balances (cash and
inventory are higher because the store has been profitable) with no matching
recorded line anywhere. This is not a bug in `balance_sheet_summary` — it
correctly reports what the GL contains — it is the direct, verifiable
consequence of Equity never being seeded. **Verification, not assumption:**
`test_balance_sheet_assets_equal_cash_plus_inventory_after_a_sale` proves
`total_assets` moves by exactly a sale's cash-plus-COGS effect, i.e. by net
income — the residual that a real Equity account would capture.

---

## Phase 3: P&L → Equity relationship

Traced directly in `accounting/service.py`:

- `profit_and_loss(db, *, date_from, date_to, ...)` computes `net_sales`,
  `cogs`, `gross_profit`, `other_income`, `operating_expenses`, `net_income`
  **freshly on every call**, filtered to the caller's `date_from`/`date_to`
  window via `trial_balance()`'s own date filters. It writes nothing. It is
  a pure query.
- `balance_sheet_summary(db, *, as_of, ...)` calls
  `trial_balance(store_ids=..., date_to=as_of)` — **no `date_from`** — i.e.
  cumulative from the beginning of that store's history through `as_of`.

**Direct answer to the brief's question** ("If January has net income of X
and February has net income of Y, where is January's earnings represented
in February's Balance Sheet, if anywhere?"): **Nowhere, as a labeled amount.**
January's net income is never "closed" or transferred anywhere — Revenue and
Expense accounts are never zeroed at any boundary; they simply keep
accumulating (correctly, since `profit_and_loss` re-filters by date range
every time it is asked for a period). But because
`balance_sheet_summary`(as_of = end of February) sums `JournalLine` amounts
for ASSET/LIABILITY accounts from the store's entire history through
February, **the economic effect** of January's net income (the extra cash
and/or inventory value it left behind) **is** present in February's asset
totals — it is simply commingled with every other asset movement and has no
corresponding, separately-identified Equity balance. This is exactly the
missing-Equity gap already known (Phase 1), now precisely located: the
Balance Sheet side already behaves correctly as a *permanent, cumulative*
report; the *Equity* side that would make the accumulation legible and the
equation balance is what does not exist. **No retained-earnings behavior is
invented here** — this is a description of current, verified behavior only.

---

## Phase 4: Store / entity ownership scope

No `Company`, `Organization`, or `LegalEntity` model exists anywhere in the
codebase (confirmed by grep across `app/`) — `Store`
(`app/modules/auth/models.py`) is the only organizational unit above a user.
Every financial construct in this system is already store-scoped, with no
company-wide aggregation *model* (only company-wide *reporting*, via
`store_ids: list[int] | None` parameters that are themselves lists of
individual stores' own data):

- `JournalEntry.store_id` (M4) — required, not nullable.
- `AccountingPeriod.store_id` (M22) — required, not nullable, per-store
  closed-date-range.
- `FiscalConfig`/fiscal submissions (M20) — per-store.
- `Store.legal_name` / `Store.tax_registration_number` (M20) — each store
  carries its **own** optional legal identity/tax-registration fields, not a
  shared parent company's.

**Evidence-based conclusion:** the repository's own data model consistently
treats each *store* as the natural financial/legal unit, not a
consolidated multi-store company — every other financial boundary in this
system (`JournalEntry`, `AccountingPeriod`, `FiscalConfig`, and now the
legal-identity fields themselves) is per-store, never company-wide. If Equity
is built, per-store is the only scope consistent with everything else in the
system today. **This is still not treated as a settled business decision
here** — the brief explicitly warns "do not assume that per-store accounting
periods imply per-store legal equity" — but the *evidence* uniformly points
one way, and no document anywhere describes an alternative (consolidated,
multi-store-company) structure. The gap this surfaces: the repository never
states whether stores are, in reality, legally separate businesses or
branches of one company — that fact is unknown, not merely undecided, and is
recorded here as a genuine informational gap rather than guessed at.

---

## Phase 5: Opening balances

Inspected: every migration that creates `accounts`/`journal_entries` (none
seeds a non-zero balance — the M4 seed only inserts `Account` rows, never a
`JournalEntry`), `accounting/service.py` (every `post_*_journal` function is
reachable only from a real operational event — sale, receipt, invoice,
payment, credit note, adjustment, transfer, payroll, shift close — none is a
generic "set a balance" function), the `MANUAL` source type (exists solely so
`reverse_journal_entry` has a legal reversal target — `accounting.post` is
reserved and **no endpoint creates a MANUAL entry today**), and every
`reports`/`accounting` admin workflow (none initializes a balance).

**Conclusion:** there is no opening-balance concept, workflow, or account
anywhere in this system, for any account type (cash, inventory, receivables,
payables, or — since none exist — fixed assets or equity). A brand-new store
starts at exactly $0 in every account and only ever accumulates through real,
individually-accounted operational transactions. This is arguably a coherent
design for a store that begins operating *inside* this system from day one
(nothing needs "initializing" because nothing existed before) — the gap only
matters if the business ever needs to **onboard** a store with pre-existing
inventory/cash/payables from outside the system, and no document anywhere
states whether that scenario is ever expected. Recorded as an unresolved
need, not implemented speculatively.

---

## Phase 6: Period-close reassessment against 8 scenarios

Re-derived directly from the actual `posting_date=` source of every
`post_*_journal` function (re-verified in this milestone, one correction to
M22's own table noted below):

| Function | `posting_date` source | Backdatable by a caller? |
|---|---|---|
| `post_sale_journal` | `sale.completed_at` (server timestamp) | No |
| `post_sale_return_journal` | `return_date` (request-body field) | **Yes** |
| `post_goods_receipt_journal` | `received_date` (request-body field) | **Yes** |
| `post_purchase_return_journal` | `return_date` (request-body field) | **Yes** |
| `post_purchase_invoice_journal` | `invoice_date` (request-body field) | **Yes** |
| `post_purchase_invoice_void_journal` | `purchase_invoice.invoice_date` (the **original** invoice's stored date) | Inherits whatever date the original invoice had |
| `post_supplier_payment_journal` | `payment_date` (request-body field) | **Yes** |
| `post_supplier_credit_note_journal` | `credit_date` (request-body field) | **Yes** |
| `post_supplier_payment_reversal_journal` | `datetime.now(UTC).date()` | No |
| `post_supplier_credit_note_reversal_journal` | `datetime.now(UTC).date()` | No |
| `post_stock_adjustment_journal` | `stock_adjustment.created_at` (server timestamp) | **No — correction to M22_DISCOVERY.md's Phase 1 table, which listed this as backdatable; it is not.** `create_stock_adjustment` has no caller-supplied date parameter at all. |
| `post_transfer_shipment_journal` | `transfer.shipped_at`, set via `datetime.now(UTC)` in `transfers/service.py` | No |
| `post_transfer_receipt_journal` | `received_date` (request-body field) | **Yes** |
| `post_payroll_journal` | `payroll_period.pay_date` (request-body field, DB-constrained `>= period_end`) | **Yes** |
| `post_payroll_reversal_journal` | `datetime.now(UTC).date()` | No |
| `post_cash_shift_variance_journal` | `shift.closed_at`, set via `datetime.now(UTC)` in `shifts/service.py` | No |
| `reverse_journal_entry` (generic) | `date.today()` | No |

This correction does not change any M22 conclusion or invariant — the
period-lock gate (`_enforce_period_open`) applies uniformly regardless of
which functions happen to be backdatable — it only makes the "why" more
precise: functions are safe against closed-period corruption either because
they always post today (a genuine architectural pattern for
reversals/corrections) or, in stock adjustment's and transfer-shipment's
case, because they simply have no caller-facing date field to backdate with
in the first place.

**A–H, answered from this table plus M22's existing `_enforce_period_open`
gate (unchanged, not modified in this milestone):**

- **A. Closing today's period:** blocks all posting to that store for the
  rest of today, including same-day reversals/corrections (they also post at
  `date.today()`). Documented sharp edge, unchanged from M22.
- **B. Closing a historical period:** the primary intended use — blocks any
  *new* backdated posting into it; already-posted entries are untouched
  (no historical mutation exists anywhere in this codebase).
- **C. Corrections after close:** succeed, because every correction-type
  function (payment/credit-note/payroll reversal, generic manual reversal)
  posts at `date.today()`, which cannot fall inside a *historical* closed
  range.
- **D. Reversals after close:** identical to C — same functions.
- **E. AP invoice voids whose original date is closed:** **blocked** — the
  one real exception, analyzed in full in Phase 7.
- **F. Inventory corrections after close:** **never at risk** — stock
  adjustments always post at their own creation timestamp (today), so a new
  adjustment can never be backdated into a historical closed period; it only
  fails if *today's* period is closed (case A, working as intended).
- **G. Payroll corrections after close:** the *reversal* path (correcting a
  posted period) always posts today and succeeds (case C). The *forward*
  path (`post_payroll_journal`, keyed on `pay_date`) is correctly blocked if
  a payroll period's `pay_date` falls inside an already-closed range —
  exactly the intended protection, not a gap.
- **H. Sales returns after close:** `return_date` is a genuine request-body
  field. A return processed today with `return_date` left at today succeeds
  regardless of any historical closed period; a return explicitly backdated
  into a closed period is correctly blocked — intended behavior, not a gap.

**Conclusion:** the M22 period-close model is sufficient for every scenario
except E. No change to its create-only/no-reopen/per-store design is needed
for Balance Sheet/Equity to build on top of it later — the lock operates
purely on `posting_date` and is completely independent of which accounts a
future posting might touch (Equity included, were one ever posted).

---

## Phase 7: `PURCHASE_INVOICE_VOID` date-choice investigation

Read in full: `post_purchase_invoice_void_journal`'s docstring (`accounting/
service.py`) and `void_purchase_invoice`'s docstring (`ap/service.py`).

1. **Why was it designed this way?** No comment, docstring line, or design
   doc anywhere explains the specific choice of `posting_date=
   purchase_invoice.invoice_date` over `date.today()`. The surrounding
   docstring explains *other* design choices in detail (why it's a new
   STANDARD entry with mirrored lines rather than a fresh computation, why
   it bypasses the generic reversal mechanism, why it's restricted to
   unsettled invoices) — but is silent on the date. The most plausible
   reading is that it was written by analogy to every *forward*-posting
   function (each of which naturally uses its own operational record's
   date), without the void's own nature as a *correction* being considered
   against the pattern every other correction in this codebase follows
   (post at `date.today()`).
2. **Is this intentional accounting behavior?** No evidence that it is.
   `void_purchase_invoice`'s own docstring calls the result a "compensating
   ... journal entry" — the same word used for every other
   correction/reversal in this codebase, all of which use today's date. The
   void is the one place that language and that behavior diverge, with
   nothing in the repository resolving the inconsistency either way.
3. **Should a closed-period invoice be corrected by blocking, a
   current-period reversal, a correction entry, or another mechanism?**
   **Not decided here.** Each option has real tradeoffs the repository has
   never weighed: blocking (today's actual behavior) means a posted invoice
   whose date falls into a closed period can never be voided without
   reopening the period (reopen does not exist — Phase 1); dating the void
   at `date.today()` instead (mirroring every other correction) would make
   it consistent with the rest of the system but is itself a behavior
   change to AP semantics that is out of M22's and this discovery's scope to
   decide unilaterally.

**Classified: BUSINESS/ACCOUNTING-POLICY QUESTION, UNRESOLVED.** Carried
forward from `docs/M22_TESTING_SESSIONS.md`'s own "Limitations" section,
now root-caused rather than merely observed. No fix is proposed or applied
in this discovery.

---

## Phase 8: Balance Sheet reporting design (conditional — not implemented)

**This section documents what a complete design would look like if and only
if the Phase 1 UNRESOLVED items (Equity ownership, retained-earnings
semantics) are ever resolved by the business.** It is not a commitment to
build it and nothing here is implemented.

If authorized, the natural design extends the *existing*
`balance_sheet_summary` exactly the way `docs/M4_ACCOUNTING_CORE.md` Section
16 already anticipated for period-close — an additive change to a function
that already derives everything from `trial_balance()`, never an independent
calculation:

- **Date/as-of semantics:** unchanged — `as_of` (or none, meaning "through
  now"), matching the existing cumulative, permanent-account semantics
  established in Phase 3.
- **Store filtering:** unchanged — `store_ids: list[int] | None`, per the
  Phase 4 finding that Equity, if built, is per-store like everything else.
- **Account grouping:** add `account_type == "EQUITY"` alongside the
  existing ASSET/LIABILITY branches — no new query, same `trial_balance()`
  rows already fetched.
- **Current/non-current classification:** **no evidence this system needs
  it** — nothing in the chart of accounts or any design doc distinguishes
  current vs. non-current assets/liabilities (e.g., there is no long-term
  debt or fixed-asset account at all). Not proposed.
- **Equity presentation:** would need at minimum one seeded EQUITY account
  to hold accumulated net income (its exact name/structure is the
  UNRESOLVED decision from Phase 1) plus, if period-end closing entries are
  ever built, the closing mechanism that posts each period's `net_income`
  into it (Section 16's own suggested extension point).
- **Validation:** `total_assets == total_liabilities + total_equity` would
  become a real, checkable invariant for the first time — currently
  meaningless because there is no `total_equity` term at all. This should be
  a test-verified invariant from day one of any real implementation (see
  Phase 9, INV-9).

---

## Phase 9: Invariants for a future implementation

Retained only where applicable given this discovery's findings (numbering
matches the brief's own list where a brief-listed invariant is kept):

1. Every journal entry balances (already true today — DB-enforced deferred
   trigger, unchanged).
2. Posted historical journals remain immutable (already true today —
   `UPDATE`/`DELETE` revoked on `journal_entries`/`journal_lines`, unchanged).
3. Closed periods cannot be mutated through ordinary posting paths (already
   true today, M22's `_enforce_period_open`, unchanged).
4. Reversals balance (already true today, unchanged).
5. Corrections preserve auditability (already true today — every reversal
   records `reversed_by`/`reason` via `audit_service.log_event`, unchanged).
6. Operational transactions reconcile to GL (already true today —
   `inventory_reconciliation`, `cash_payment_method_summary`, unchanged).
7. P&L derives from the GL, never an independent calculation (already true
   today, reaffirmed by M22's own P&L fix).
8. **Balance Sheet derives from the GL, never an independent calculation**
   — already true for the existing Assets+Liabilities partial version;
   would extend unchanged to Equity if built (Phase 8).
9. **Assets = Liabilities + Equity** — not yet a real invariant (no Equity
   term exists to check); becomes real and testable only once Phase 1's
   Equity decision is resolved and implemented.
10. **Net income is represented exactly once in the appropriate equity
    mechanism** — not applicable today (no equity mechanism exists); would
    apply the moment one is built, and would need its own test proving no
    double-counting across periods.
11. Closing does not duplicate or erase economic value — already true today
    (period-close creates zero `JournalEntry` rows, M22, unchanged); would
    need re-verification the moment a real "closing entry" (zeroing P&L into
    Equity) is ever built, since that *would* create GL rows for the first
    time.
12. Store/entity boundaries remain intact — already true today (every
    construct in this system is per-store, Phase 4), and directly informs
    Equity's own scope if built.
13. Concurrent close/post operations cannot create an inconsistent state —
    already proven for the existing period-lock (M22 Session I reasoning,
    the DB-enforced `ExcludeConstraint` plus each posting's own transaction
    boundary); would need re-verification against any new Equity-affecting
    write path.

Not retained: a fiscal-year-boundary invariant (no fiscal year concept
exists — Phase 1) and a "permanent vs. temporary account" invariant stated
as such (the behavior already exists implicitly, Phase 1 — inventing a
formal invariant for an undesigned distinction would overstate what the
system actually models).

---

## Phase 10: Test-session design (not implemented)

For a future M23B implementation, each session and the invariant it would
prove:

- **A — Chart-of-accounts integrity:** the new Equity account (if any) is
  seeded correctly, `account_type='EQUITY'` accepted by the existing CHECK
  constraint (already true, verifiable without any code change).
- **B — Balance Sheet calculation:** `balance_sheet_summary` includes the
  new Equity section, derived from `trial_balance()` only (Phase 8, INV-8).
- **C — Equity behavior:** whatever posting mechanism is decided posts the
  correct amount to the correct account (INV-1, INV-4 as applicable).
- **D — Retained earnings:** if built, one period's net income is
  represented exactly once and persists correctly into the next period's
  Balance Sheet (INV-10, directly answering Phase 3's traced question).
- **E — Opening balances:** if built, an initial balance posts through the
  same `_post_journal` gate as everything else (no bypass), and is
  correctly `PERIOD_CLOSED`-refused if dated into an already-closed range.
- **F — Period close:** re-run of M22's existing Session A/B/D (unchanged,
  regression only).
- **G — Post-close correction:** re-run of M22's existing Session C, plus
  the resolution (whatever it turns out to be) to Phase 7's
  `PURCHASE_INVOICE_VOID` question.
- **H — AP invoice void after close:** a dedicated session proving whichever
  Phase 7 resolution is chosen (block / today-dated compensating entry /
  something else) behaves exactly as decided, not as currently observed.
- **I — P&L-to-equity reconciliation:** `net_income` for a closed period,
  once transferred (if built), matches exactly what `profit_and_loss` would
  have reported for that same range (INV-10).
- **J — Assets = Liabilities + Equity:** the new top-level invariant
  (INV-9), checked after a representative mix of postings.
- **K — Multi-store accounting:** Equity remains per-store; one store's
  balance never leaks into another's (INV-12, mirrors M21/M22 isolation
  methodology).
- **L — Authorization:** whichever permission gates Equity/period-close
  administration (most likely `accounting.admin`, reused per M22 precedent)
  is enforced at the service layer, not only HTTP (mirrors M21 Session C).
- **M — Concurrency:** re-run of M22's existing period-close concurrency
  reasoning against any new write path Equity introduces (INV-13).
- **N — Failure injection:** a failure mid-posting-to-Equity leaves no
  partial state (mirrors the existing `_post_journal` atomicity guarantee).
- **O — Migration safety:** additive only — new account row(s), no backfill
  of historical data, no existing-row rewrite (mirrors M22's own migration).
- **P — Adversarial bypass:** attempt to post to Equity through any path
  other than the decided mechanism; attempt to compute Balance Sheet totals
  independently of `trial_balance()` and confirm the report never does so.
- **Q — Mutation testing:** break the Equity-posting gate / the A=L+E
  validation / the retained-earnings transfer (whichever is built) and
  confirm each fails RED for the correct reason, exactly as M21/M22 did.

---

## Phase 11: STOP conditions — checked against this discovery

| Condition | Status |
|---|---|
| Equity ownership scope | Evidence points uniformly to per-store (Phase 4), but no document states this as a settled fact about the business's actual legal structure. **STOP** — documented as an informational gap, not guessed past. |
| Retained-earnings semantics | **STOP** — zero repository evidence (Phase 0/1). |
| Opening-balance semantics | **STOP** — no workflow, no design decision on whether it is ever needed (Phase 5). |
| Fiscal-year boundary | **STOP** — no concept exists anywhere (Phase 1). |
| Period-close semantics | **Not a STOP** — already fully resolved and implemented in M22; this discovery found it sufficient for every future Equity-adjacent scenario except one (Phase 6). |
| Post-close correction semantics | **Not a STOP** for the general case (already resolved, Phase 6) but **STOP** specifically for `PURCHASE_INVOICE_VOID` (Phase 7). |

Every STOP above is documented with its exact evidence gap above, per Phase
13's self-audit discipline (next section) — none is answered by appeal to
"standard accounting practice."

---

## Phase 12: Proposed M23 implementation boundary

**Recommendation: split, and do not implement in M23 at all.**

The repository does not contain sufficient authority to safely implement any
part of the Balance Sheet/Equity model — every load-bearing question (Equity
ownership scope's legal reality, retained-earnings mechanics, opening
balances, fiscal-year boundary, and the `PURCHASE_INVOICE_VOID` date policy)
is genuinely unresolved, not merely under-specified. Implementing any of it
now would mean inventing accounting policy, which every phase of this
discovery was instructed not to do and found no basis for.

- **M23 (this milestone): discovery only.** Delivered as this document.
  No implementation.
- **M23A (future, if authorized): business-policy resolution.** A
  human/business decision on: (a) is each Store a separate legal entity or
  branches of one company (resolves Equity scope); (b) what should
  `PURCHASE_INVOICE_VOID` do when its original date is closed; (c) does this
  business ever need to onboard a store with pre-existing balances; (d) does
  this business want period-end closing entries into Retained Earnings at
  all, or is the existing period-*lock* (without equity) sufficient
  indefinitely. None of these require code to answer.
- **M23B (future, only after M23A): implementation.** Scoped precisely by
  M23A's answers, using the design already sketched in Phase 8-10 as a
  starting point — likely to be smaller than a full classic Balance Sheet if
  the business answers narrowly (e.g., "yes to Equity, no to fiscal-year
  closing" is a materially smaller implementation than the full model).

This is not implementation deferred for its own sake — it is the direct,
evidence-based consequence of Phase 11's STOP conditions.

---

## Phase 13: Self-audit

**For every "required"/"partially required" item — what evidence proves it?**
Balance Sheet's PARTIAL classification rests on a shipped, tested endpoint
(`reports/service.py:671-709`, `test_reports_financial.py:56-79`) — not
inference. No item in Phase 1 was classified AUTHORITATIVELY REQUIRED, so
there is nothing else to challenge here — consistent with the brief's own
warning against inferring "required" from standard practice; this discovery
found no repository requirement strong enough to earn that label for
anything.

**For every "not required" item — what evidence proves it?** Fiscal-year
boundary and opening balances were classified NOT SUPPORTED/NOT DEFINED
(not "not required" — a stronger, evidence-only claim that the mechanism
does not currently exist) — verified by exhaustive grep and by tracing every
`post_*_journal` call site to confirm none is reachable except from a real
operational event.

**For every "unresolved" item — why can't the existing source answer this?**
Equity ownership scope: the evidence (every construct is per-store) is
consistent but the repository never states the underlying legal fact it
would be modeling — evidence can point at a scope without confirming intent.
Retained earnings / net-income transfer / fiscal year: literally zero
mentions anywhere (Phase 0's exhaustive term search) — there is nothing to
read that could answer it. `PURCHASE_INVOICE_VOID`'s date choice: read both
relevant docstrings in full (Phase 7) — neither states a reason; the silence
itself is the finding.

**For every proposed accounting behavior — could this alter historical
financial results? If yes, what correction/reversal mechanism is required?**
No accounting behavior is proposed for implementation in this discovery (all
of Phase 8-10 is explicitly conditional/future). The one *already-existing*
behavior newly re-verified here — M22's own P&L fix — was already shipped
and reported in `docs/M22_TESTING_SESSIONS.md` as a **known, user-visible
reporting change** (net income decreases once labor and other previously-
omitted expenses are included); this discovery confirms that characterization
was accurate and adds nothing new to it. The Phase 6 correction to M22's
Phase 1 table (stock adjustments are not actually backdatable) is a
documentation-accuracy fix, not a behavior change — no code changed, no
historical result is affected, and M22's own period-lock gate already
behaved correctly regardless of that table's imprecision.

---

## Final validation checklist

- Production files unchanged: confirmed — `git status` shows only this
  document as new/changed.
- Migrations unchanged: confirmed — no new revision created.
- Tests unchanged: confirmed — no test file edited (all evidence gathered
  by reading existing tests/code, never by modifying them).
- Current HEAD documented: `631c5a5` (start), same at commit time (below).
- M22 findings accurately represented: cross-checked against
  `docs/M22_DISCOVERY.md`/`docs/M22_TESTING_SESSIONS.md` directly; the one
  discrepancy found (stock-adjustment backdatability) is called out
  explicitly above, not silently corrected.
- All repository accounting documentation reviewed: M4, M6, M7, M10, M11
  (design + hardening audit), M15, M20, M21 discovery, M22 discovery +
  testing sessions.
- All Equity/Balance Sheet references searched: Phase 0's table, exhaustive.
- No accounting policy invented: every Phase 8-10 design element is labeled
  conditional/future; nothing was implemented.
- No jurisdiction assumptions introduced: none made; fiscal-year and
  jurisdiction-specific concerns are explicitly out of scope (consistent
  with M20's own fiscal-module boundary).
- F7 remains untouched: not mentioned in any implementation sense, not
  re-opened.
- M22's P&L labor decision remains unchanged: reaffirmed, not revisited.
- `PURCHASE_INVOICE_VOID` risk explicitly addressed: Phase 7, in full.
