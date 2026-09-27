# M23A: Accounting Policy Resolution

**Status:** Policy/design only. No production code, migration, seeded account,
API behavior, frontend behavior, or test changed. Starting HEAD: `0249dc2`
(M23 discovery commit).

## 1. Executive decision summary

| # | Policy area | Decision |
|---|---|---|
| 1 | Equity requirement | **NO** — not required by the product mission. Reserved-but-unused, as today. |
| 2 | Retained earnings / net-income carry-forward | **NOT REQUIRED** (direct consequence of #1). |
| 3 | Opening balances / store onboarding | **NOT REQUIRED** — no subsystem exists or is built; a clear future extension point is documented, not built speculatively. |
| 4 | Fiscal-year boundary | **NOT REQUIRED** beyond the ordinary accounting-period locking M22 already built. |
| 5 | `PURCHASE_INVOICE_VOID` date policy | **Option A — a void always posts on the current date**, for consistency with every other compensating-entry mechanism already in this codebase. This is a real, scoped M23B fix. |
| 6 | Store/entity accounting scope | **Each store is its own accounting entity.** No consolidated company/organization model; consolidated reporting stays an aggregation over `store_ids`, never a stored "company" row. Inter-store balances remain explicit (never eliminated), matching the existing Inter-Store Transfer accounting. |

Only decision #5 requires an M23B code change. Decisions #1–#4 and #6 require
**no new code** — they close out `docs/M23_DISCOVERY.md`'s open questions by
confirming the current, already-shipped behavior is the intended, permanent
behavior, not a temporary gap.

---

## 2. Evidence reviewed

- `docs/M23_DISCOVERY.md` (this milestone's own immediate predecessor —
  every UNRESOLVED item it raised is resolved or explicitly re-affirmed
  unresolved below).
- `docs/TECHNICAL_BLUEPRINT.md` — the **original product mission
  specification**, read for the first time in this accounting sub-thread
  (M4/M11/M22/M23 discovery never cited it directly). This is the decisive
  new evidence this milestone adds:
  - Section G, "Accounting and Reporting" (the authoritative accounting
    spec): eleven required metrics — Gross Sales, Discounts, Tax Collected,
    Net Sales, COGS, Gross Profit, Gross Margin %, Purchases, Inventory
    Value, Stock Movement, Daily Sales, Supplier totals. **No Balance
    Sheet, Equity, Assets, or Liabilities line ever appears in this list.**
  - FR-15 `[SPEC]`: "Profit & Loss reporting (gross sales, COGS, gross
    profit)" — the complete, explicit statement of this product's
    financial-reporting requirement.
  - "Supplier balance/AP is out of spec's stated scope; the schema leaves
    room for a future `supplier_invoices`/`supplier_payments` pair" — proof
    that this codebase has a track record of **building real accounting
    machinery (AP, in M6/M7) beyond the original spec only when a concrete
    operational need justified it**, never to chase completeness for its
    own sake. The chart-of-accounts/double-entry journal engine itself
    (M4) is exactly this pattern: built to make the *required* P&L
    reproducible and non-hand-editable (BR-1/BR-2), not to deliver GAAP
    financial statements nobody asked for.
  - "the specification's own scale (single store, ~\$20/month)" — the
    product's origin is explicitly single-store; multi-store was a named
    *future* roadmap item (risk table: "Scope creep before MVP ships
    (multi-store, ...)"), later built out for real (M8+) without ever
    introducing a company/organization entity above `Store`.
- `docs/M4_ACCOUNTING_CORE.md`, `docs/M11_DESIGN.md` (Section 5.2, 18),
  `docs/M11_HARDENING_AUDIT.md` (Section 12), `docs/M22_DISCOVERY.md`,
  `docs/M22_TESTING_SESSIONS.md` — each independently concludes "no Equity
  account exists, nothing produces a transaction that would post to one,"
  across four separate milestones (M4, M11, M22, M23) with zero
  contradiction between them.
- `backend/app/modules/accounting/{models,service,constants}.py`,
  `backend/app/modules/reports/service.py` (`balance_sheet_summary`),
  `backend/tests/test_reports_financial.py`
  (`test_balance_sheet_has_no_equity_section_by_design` — the absence of
  Equity is a **tested, intentional invariant**, not merely an unaddressed
  gap).
- `backend/app/modules/accounting/service.py`'s complete posting/reversal
  set (`post_purchase_invoice_void_journal`, `post_supplier_payment_
  reversal_journal`, `post_supplier_credit_note_reversal_journal`,
  `post_payroll_reversal_journal`, `reverse_journal_entry`) — re-read in
  full for policy area 5.
- `backend/app/modules/fiscal/models.py::FiscalConfig` — confirmed this is
  per-sale tax-submission configuration, unrelated to fiscal-year
  accounting; contains no year-boundary concept.
- Repo-wide search (repeated from M23 discovery, re-confirmed unchanged):
  zero hits anywhere for "onboard[ing]," "fiscal year," "retained
  earnings," "year-end close," "income summary."

---

## 3. Policy decisions (detailed)

### 3.1 Equity requirement — **NO**

This ERP does not need to support an actual `EQUITY` account. The product's
own accounting specification (`TECHNICAL_BLUEPRINT.md` Section G) defines
its full financial-reporting requirement as P&L-family metrics plus
inventory valuation and stock movement — never a balance sheet, and never an
equity concept. The double-entry ledger (M4) exists to make *those* required
numbers trustworthy and reproducible, not to deliver a GAAP-complete
financial-statement set nobody has ever asked for in five milestones of
accounting work (M4, M6, M7, M10, M22) plus this discovery/policy pair.

Since the decision is NO, per the brief's own instruction the remaining
sub-questions collapse:

- **What the Balance Sheet is officially intended to represent:** exactly
  what it already says it is — `reports/service.py::balance_sheet_summary`'s
  own docstring, unchanged: "Assets and Liabilities ONLY... this system's
  chart of accounts has no Equity/Retained-Earnings account... Labeled
  everywhere as 'Balance Sheet Summary (Assets & Liabilities)', never as a
  complete balance sheet." This is now the **permanent, intended** design,
  not a stopgap awaiting a future Equity account.
- **Why `EQUITY` remains reserved but unused:** the `account_type` CHECK
  constraint's inclusion of `'EQUITY'` (M4 migration) costs nothing to leave
  in place — it is schema-level flexibility, not a commitment. Removing it
  would be a needless, disruptive schema change for a value that is
  harmless sitting unused, consistent with how `accounting.post` and
  `accounting.admin` permissions are already documented as "reserved" in
  this same codebase without being exercised.

### 3.2 Retained earnings / net-income carry-forward — **NOT REQUIRED**

Direct consequence of 3.1: retained earnings only has meaning as an Equity
mechanism, and Equity is not required. The traced behavior from
`docs/M23_DISCOVERY.md` Phase 3 — P&L is period-scoped and re-computed live
on every call; the Balance Sheet Summary is cumulative-to-date and never
"closes" anything — is **hereby confirmed as the intended, permanent
design**, not a gap awaiting a closing mechanism. Concretely:

- **Account(s):** none.
- **Posting mechanism / timing:** none — no closing entry is ever created.
- **Fiscal-year vs. accounting-period behavior:** moot (see 3.4).
- **Monthly period close (M22):** unaffected — it remains exactly what it
  already is, a posting *lock*, never a P&L-zeroing event.
- **Year-end close:** not distinct from period locking, because neither
  involves any equity transfer (see 3.4).
- **Profit vs. loss treatment:** identical — `profit_and_loss()` already
  reports a signed `net_income`; a loss is simply a negative value, with no
  different downstream handling required since nothing consumes it as a
  posting input.
- **Reversal/correction behavior:** unaffected — corrections already work
  entirely within the existing GL (M22 Session C), independent of any
  equity mechanism.
- **Multi-store scope:** moot — no equity to scope.

### 3.3 Opening balances / store onboarding — **NOT REQUIRED**

No workflow, endpoint, or account has ever existed for this in 22
milestones, and the product's own integrity principle (`TECHNICAL_BLUEPRINT.md`
BR-1/BR-2: every reported figure is "computed from transaction history...
never stored as manually editable totals") is in direct tension with an
opening-balance injection, which is by definition a number with no
transactional provenance. Every store in this system begins at $0 across
every account and accumulates purely through real, individually-accounted
operational events (sales, receipts, invoices, payments, adjustments,
transfers, payroll) — this is not an oversight, it is the same
"never hand-editable" discipline applied consistently to store creation.

This is decided, not merely deferred: **no opening-balance subsystem is
required by the current mission.** If the business ever needs to onboard a
store with pre-existing inventory/cash/payables (e.g., migrating an
existing physical shop's books into this system), the correct future
mechanism — documented here as an extension point, not built now — would be
a dedicated `OPENING_BALANCE` source type posted once, through the same
`_post_journal` gate as everything else (no bypass), balanced against
whatever the Equity decision would be at that future time. Building it
speculatively today, with no real onboarding need in front of it, would be
exactly the unnecessary subsystem the brief warns against.

### 3.4 Fiscal-year boundary — **NOT REQUIRED beyond ordinary period locking**

Distinguishing the brief's three categories precisely:

- **(A) Ordinary accounting-period locking:** **required, and already
  fully built** (M22's `AccountingPeriod`). This is the actual, sufficient
  mechanism for this product's needs — an admin closes an arbitrary
  date range per store to stop further backdated posting into it.
- **(B) Fiscal-year closing** (a formal annual boundary that zeroes P&L
  into Retained Earnings): **not required.** It is meaningless without
  Equity/Retained Earnings (3.1/3.2), which are not required, so there is
  nothing for a fiscal-year close to *do* beyond what period locking (A)
  already accomplishes.
- **(C) Permanent historical finalization:** **already effectively achieved**
  by (A) plus the pre-existing immutability of `journal_entries`/
  `journal_lines` (M4's `UPDATE`/`DELETE` revocation) — a closed period's
  entries can never be altered, which is the substance of "permanent
  finalization" without needing a distinct calendar/fiscal-year concept
  layered on top.

No calendar-year assumption is introduced or required. `AccountingPeriod`'s
existing arbitrary-date-range design (not tied to any year boundary) is
confirmed as sufficient and intentional, not a placeholder for a future
fiscal-year feature.

### 3.5 `PURCHASE_INVOICE_VOID` after a closed period — **Option A: always post on the current date**

Rejected reasoning first, to show the choice is not made "merely because it
is conventional": Option A is *also* what a textbook accounting system would
do, but that is not why it is chosen here. It is chosen because of the
following in-codebase evidence, which is stronger than an appeal to
convention:

- Every other compensating/reversal mechanism this exact codebase has ever
  built — `post_supplier_payment_reversal_journal`,
  `post_supplier_credit_note_reversal_journal`,
  `post_payroll_reversal_journal`, and the generic
  `reverse_journal_entry` — posts at `date.today()` / `datetime.now(UTC).date()`.
  That is 4-for-4, unanimous, spanning three separate milestones (M4, M6/M16,
  M10), with `PURCHASE_INVOICE_VOID` as the sole, unexplained exception
  (`docs/M23_DISCOVERY.md` Phase 7: no docstring, comment, or design doc
  anywhere justifies the divergence).
- `void_purchase_invoice`'s own docstring already calls its result a
  "compensating... journal entry" — the exact term this codebase uses for
  the today-dated corrections above. The *language* already claims Option
  A's behavior; only the *code* does not.
- Choosing Option A costs nothing architecturally: it slots into the
  existing `_post_journal`/`_enforce_period_open` gate exactly like every
  other correction, with zero special-casing.

**Defined for the selected policy:**

- **Accounting date semantics:** `post_purchase_invoice_void_journal`'s
  `posting_date` becomes `date.today()` (or the UTC-consistent equivalent
  the other reversal functions use), replacing
  `purchase_invoice.invoice_date`.
- **Closed-period behavior:** a void now succeeds regardless of how far in
  the past the original invoice was posted, exactly like every other
  correction — it is only refused if *today's own* period is closed (the
  same universal, already-documented sharp edge every other correction
  accepts, M22 Phase 3).
- **Audit requirements:** unchanged — `void_purchase_invoice` already
  records `voided_by`/`reason`; no new audit gap is introduced or closed by
  this change.
- **Relationship to AP document status:** unchanged — void remains scoped
  to unsettled invoices only (`amount_paid == 0 AND amount_credited == 0`,
  M7's own documented limitation); this policy does not touch that scope.
- **Tax implications:** none beyond what already exists — the void mirrors
  the original entry's tax line exactly (`_purchase_invoice_journal_lines(...,
  reverse=True)`), unaffected by which date it posts on.
- **Inventory implications:** none — `PURCHASE_INVOICE` postings never
  touch Inventory (that is `PURCHASE_RECEIPT`'s role); voiding an invoice
  was never an inventory event and remains one.
- **Supplier balance implications:** the void still nets the same AP/
  Purchase Clearing amounts to zero; only the *date* on which that
  correction is recorded changes, not the amount or the accounts touched.
- **Reversal/correction semantics:** a void remains a one-time, idempotent
  operation (an already-voided invoice's `void_purchase_invoice` call is
  already a no-op per its own code) — unaffected by this date change.
- **Idempotency:** unaffected — the existing `uq_journal_entries_source`
  partial unique index (`source_type='PURCHASE_INVOICE_VOID'`,
  `source_id=<invoice id>`) already prevents a double-void regardless of
  posting date.
- **Concurrency:** unaffected — `void_purchase_invoice` already locks the
  invoice row (`with_for_update()`) before touching it; the date change
  does not alter that.

**Must the same policy apply to purchase returns, supplier payments, credit
notes, sales corrections, and other financial corrections?** No — and the
reason is itself evidence-based, not an inconsistency:

- **Purchase returns, sale returns, supplier payments, credit notes:**
  these are **not voids** — each is a brand-new, distinct economic event
  with its own real-world date (a customer returned goods on a specific
  day; a payment was wired on a specific day), correctly modeled with a
  caller-supplied date field exactly as `docs/M23_DISCOVERY.md` Phase 6
  already verified. They are correctly, and already, blocked from being
  *backdated* into a closed period while remaining free to post today —
  the intended behavior, not a gap. Changing them to always use today's
  date would be wrong: it would silently misdate a real transaction that
  legitimately happened on an earlier day.
- **Supplier payment reversal, credit-note reversal, payroll reversal,
  generic manual reversal:** already use today's date (the pattern
  `PURCHASE_INVOICE_VOID` is being brought into line with) — no change
  needed.
- **`PURCHASE_INVOICE_VOID` is unique** among all of these precisely because
  it is the one operation that **erases** an original document's own
  postings by mirroring them exactly (not recording a new, independent
  economic event) — making it a true compensating entry in the same family
  as the reversal functions above, not a return/payment/credit-note.

### 3.6 Store / entity accounting scope — **each store is its own accounting entity**

- **Independent accounting entity per store:** **yes.** Every financial
  construct already built treats `Store` as the terminal scope —
  `JournalEntry.store_id`, `AccountingPeriod.store_id`, `FiscalConfig`
  (per-store, unique index on `store_id`), and `Store.legal_name`/
  `Store.tax_registration_number` (each store's *own* optional legal
  identity, M20) — never a shared parent.
- **One company ledger shared by all stores:** **no.** No `Company`/
  `Organization` model has ever existed, and the mission's own origin
  ("single store, ~\$20/month," multi-store added later without a
  consolidating entity) gives no basis to invent one now.
- **Equity store-specific:** moot (3.1 — not required), but if the business
  ever revisits 3.1, per-store would be the only choice consistent with
  everything above.
- **Retained earnings store-specific:** moot (3.2).
- **Opening balances store-specific:** moot (3.3), but the documented future
  extension point (3.3) would be per-store, matching every other mechanism.
- **Fiscal periods store-specific:** **yes, already true** — `AccountingPeriod`
  is per-store today (M22), reaffirmed as correct, not revisited.
- **Consolidated reporting required:** **yes, already built, and
  sufficient** — every relevant report function already accepts
  `store_ids: list[int] | None` for "this caller's authorized subset of
  stores" (`trial_balance`, `profit_and_loss`, `balance_sheet_summary`,
  `reports/service.py`'s analytics functions) — this **is** this system's
  consolidated reporting, an aggregation over explicit store lists, not a
  stored company-level rollup. No new consolidation entity or table is
  needed.
- **Inter-store balances eliminated or explicit:** **explicit, and already
  correctly modeled** — `post_transfer_shipment_journal`/
  `post_transfer_receipt_journal` (M8) post through a real
  `Inventory In Transit` account with a genuine source/destination store
  pair; nothing is silently netted away. This is consistent with treating
  each store as its own entity (inter-entity transfers are real, visible
  transactions, not eliminated as they would be in a single consolidated
  legal entity's own internal ledger).

---

## 4. Policy matrix

| Policy | Decision | Evidence | Required for M23B | Consequences |
|---|---|---|---|---|
| Equity | NOT REQUIRED | `TECHNICAL_BLUEPRINT.md` Section G lists no Equity/Balance Sheet metric; M4/M11/M22/M23 unanimously confirm nothing posts to one; `test_balance_sheet_has_no_equity_section_by_design` is a tested invariant | No | `EQUITY` account_type stays reserved, unused, and undocumented-as-a-gap going forward |
| Retained earnings | NOT REQUIRED | Consequence of Equity decision; zero repo mentions anywhere (M23 Phase 0) | No | No closing/carry-forward mechanism is ever built |
| Net-income carry-forward | NOT REQUIRED | Same as above; `profit_and_loss()`'s live, period-scoped computation is confirmed as the permanent design | No | Each period's P&L is always computed fresh; no persisted "prior period" figure |
| Opening balances | NOT REQUIRED | No workflow in 22 milestones; in tension with BR-1/BR-2's "never hand-editable" principle; no onboarding scenario documented anywhere | No | Every store starts at $0 in every account; future onboarding, if ever needed, gets a dedicated extension (documented, not built) |
| Fiscal year | NOT REQUIRED beyond period locking | No calendar/fiscal-year concept anywhere; `FiscalConfig` (M20) is per-sale tax submission, unrelated | No | `AccountingPeriod`'s arbitrary-date-range design (M22) is confirmed sufficient and permanent |
| Year-end close | NOT REQUIRED | Meaningless without Equity/Retained Earnings (moot per above) | No | None — no year-end-specific behavior is ever built |
| Period locking | ALREADY SUFFICIENT (no change) | M22's `AccountingPeriod` + `_enforce_period_open`, reassessed against 8 scenarios in `docs/M23_DISCOVERY.md` Phase 6, found sufficient for 7/8 | No | Reaffirmed as-is |
| Post-close correction (general) | ALREADY CORRECT (no change) | Every reversal/void except one already posts today (M23 Phase 6 table) | No | Reaffirmed as-is |
| Purchase invoice void | **CHANGE: post at `date.today()` (Option A)** | Internal consistency with 4-for-4 other reversal mechanisms; own docstring already calls it "compensating" | **Yes** | One-line change to `post_purchase_invoice_void_journal`'s `posting_date` argument; void succeeds after the original period closes, refused only if today's own period is closed |
| Purchase return correction | NO CHANGE | Not a void — a new dated economic event, correctly caller-dated (M23 Phase 6) | No | Reaffirmed as-is |
| Supplier payment correction | NO CHANGE | Already posts at today's date (reversal function) | No | Reaffirmed as-is |
| Sales correction (return/void) | NO CHANGE | Sale void is terminal-at-finalization (no reversal path); sale return is a new dated event, correctly caller-dated | No | Reaffirmed as-is |
| Store/entity scope | Each store is its own accounting entity | Every financial construct is already per-store; no Company/Organization model exists; mission origin is single-store | No | Reaffirmed as-is; no new entity model |
| Consolidated reporting | ALREADY SUFFICIENT (no change) | `store_ids: list[int] \| None` pattern already implements this across every report function | No | Reaffirmed as-is |
| Inter-store accounting | ALREADY CORRECT (no change) | `Inventory In Transit` account + explicit source/destination store on every transfer posting (M8) | No | Reaffirmed as-is; balances stay explicit, never eliminated |

---

## 5. Accounting flow diagrams (text form)

**Current, confirmed-permanent flow — a sale's economic life:**

```
Sale finalized (today, server-set date)
  -> post_sale_journal (Dr Cash/Clearing, Dr COGS / Cr Sales Revenue, Cr Inventory)
  -> profit_and_loss(date_from, date_to) queries this range live, any time, as often as needed
  -> balance_sheet_summary(as_of) sums this store's ASSET/LIABILITY lines from inception through as_of
       (the sale's cash/inventory effect is IN total_assets; there is no separate
        "this period's profit" line anywhere -- confirmed intentional, not a gap, per 3.1/3.2)
```

**`PURCHASE_INVOICE_VOID`, before and after this policy:**

```
BEFORE (current code):
  invoice posted 2025-06-01 (invoice_date, backdatable) --------> [June closed by admin, 2025-07-01]
  void attempted today (2025-08-01)
    -> post_purchase_invoice_void_journal(posting_date = invoice.invoice_date = 2025-06-01)
    -> _enforce_period_open(store, 2025-06-01) -> BLOCKED (June is closed) -- the M22/M23 gap

AFTER (Option A, M23B):
  invoice posted 2025-06-01 --------> [June closed by admin, 2025-07-01]
  void attempted today (2025-08-01)
    -> post_purchase_invoice_void_journal(posting_date = date.today() = 2025-08-01)
    -> _enforce_period_open(store, 2025-08-01) -> allowed (August is open)
    -> succeeds, mirrors the original invoice's lines exactly, dated today
       (identical shape to every other reversal in this codebase)
```

---

## 6. M23B implementation boundary

**Single change, fully scoped:**

- **Model changes:** none.
- **Migration requirements:** none (no schema change — this is a one-line
  logic change to an existing function's argument).
- **Seed/account changes:** none.
- **Service changes:** `app/modules/accounting/service.py::
  post_purchase_invoice_void_journal` — change `posting_date=
  purchase_invoice.invoice_date` to `posting_date=date.today()` (matching
  the exact pattern already used by `post_supplier_payment_reversal_journal`
  et al. two lines away in the same file).
- **API changes:** none — the endpoint (`POST
  /ap/purchase-invoices/{id}/void` or equivalent) already just calls
  `void_purchase_invoice`; its request/response shape is unaffected.
- **Frontend changes:** none — no UI surfaces `posting_date` for a void
  distinctly from any other AP action.
- **Report changes:** none — `trial_balance`/`profit_and_loss`/
  `balance_sheet_summary` already consume whatever `posting_date` a
  `JournalEntry` carries; no report-side logic changes.
- **Audit requirements:** none new — `void_purchase_invoice`'s existing
  audit logging is unaffected.
- **Permissions:** none new — `ap.reverse`-equivalent gating (whatever
  `void_purchase_invoice`'s endpoint already requires) is unchanged.
- **Invariants:** M22's existing INV-5 ("a reversal/void whose own posting
  date is `date.today()` succeeds even when the original entry's date falls
  inside a closed period... except the documented `PURCHASE_INVOICE_VOID`
  exception") is **updated to remove its own stated exception** — after
  M23B, INV-5 applies without exception, to every correction mechanism in
  the system.
- **Concurrency risks:** none new — the existing `with_for_update()` lock on
  the invoice row is unaffected by the date change.
- **Idempotency requirements:** none new — the existing partial unique
  index on `(source_type, source_id)` already covers this.
- **Cross-store isolation requirements:** none new — `store_id` is
  unaffected by this change; `_enforce_store_access` in `void_purchase_invoice`
  is untouched.

No other policy area in this document requires any M23B code change — each
is either already correctly implemented (reaffirmed, not modified) or
decided as genuinely not required by the product's mission (not built,
documented as an explicit non-goal rather than a silent gap).

---

## 7. M23B testing-session specification

Sessions A–F below are genuine **new** test coverage for the one real code
change (3.5). Sessions G–Q are **policy-compliance regression sessions** —
they exist to lock in *this document's* NOT-REQUIRED decisions as
structurally verified facts, so a future milestone cannot silently
reintroduce Equity/fiscal-year/opening-balance machinery without deliberately
revisiting this policy. This is a legitimate test category precisely because
the policy itself is the thing at risk of silent drift, not because a
not-required feature needs "testing."

| # | Session | Objective | Invariant | Test level | Failure mode prevented | Expected result |
|---|---|---|---|---|---|---|
| A | Void posts today | A void of a POSTED, unsettled invoice posts at `date.today()`, not `invoice_date` | Updated INV-5 (no exception) | Unit/service | Void silently reverts to the old, wrong date | `JournalEntry.posting_date == date.today()` for the void entry |
| B | Void after closed period succeeds | Voiding an invoice whose `invoice_date` falls inside a now-closed `AccountingPeriod` succeeds | Updated INV-5 | Service | The exact M22/M23-identified gap reappears | Void succeeds; new entry dated today; original closed period untouched |
| C | Void refused only if today is closed | Closing *today's* period blocks a void attempted today, same as every other correction | M22 INV-2/INV-5 sharp edge, now uniform | Service | Void becomes a special case that bypasses today's-own-period protection | `409 PERIOD_CLOSED` when today's period is closed |
| D | Void amount/account fidelity unaffected | The void's debit/credit lines are byte-identical to before this change except for the date | Balanced-entry invariant (unchanged) | Unit | The date fix accidentally alters amounts or accounts | Lines match `_purchase_invoice_journal_lines(..., reverse=True)` exactly, as before |
| E | Void idempotency unaffected | Voiding an already-voided invoice remains a no-op | Existing idempotency invariant | Service | The date change interacts badly with the existing no-op check | Second call returns the same invoice, no second `JournalEntry` |
| F | Mutation test: revert the date fix | Reverting `posting_date` to `purchase_invoice.invoice_date` makes Session B fail RED | N/A — mutation proof | Mutation | The new test doesn't actually exercise the changed line | Session B fails with `PERIOD_CLOSED` when mutated; passes when reverted |
| G | Equity absence is permanent policy, not a gap | `balance_sheet_summary` never gains an equity section | Policy 3.1 | Service/regression | A future change adds Equity without revisiting this policy | `test_balance_sheet_has_no_equity_section_by_design` (existing) still passes unmodified |
| H | Chart of accounts has zero EQUITY rows | No `Account` row with `account_type='EQUITY'` is ever seeded | Policy 3.1 | Service/regression | A future migration seeds an Equity account silently | `SELECT COUNT(*) FROM accounts WHERE account_type='EQUITY'` stays 0 |
| I | P&L remains period-scoped, never persisted | `profit_and_loss()` for the same range returns the same figures regardless of call order/count | Policy 3.2 | Service | A future change introduces a stored/cached net-income figure that could drift from the GL | Two calls with identical params return identical, GL-derived results |
| J | No `OPENING_BALANCE` source type exists | `SOURCE_TYPES` never gains this value without a deliberate future decision | Policy 3.3 | Unit/regression | Silent addition of an opening-balance posting path | `"OPENING_BALANCE" not in SOURCE_TYPES` |
| K | `AccountingPeriod` stays fiscal-year-agnostic | Closing an arbitrary, non-calendar-aligned date range is accepted | Policy 3.4 | Service/regression | A future change adds a fiscal-year-boundary constraint | Closing e.g. `2025-03-17..2025-04-02` succeeds (M22 behavior, re-verified) |
| L | Purchase return still caller-dated | `post_purchase_return_journal` continues to accept a backdatable `return_date`, unlike void | Policy 3.5 (non-application) | Service | Someone "fixes" returns the same way as void, misdating real events | Return posts at the supplied `return_date`, refused only if that date is closed |
| M | Supplier payment reversal unaffected | Still posts at today's date, unchanged by this milestone | Policy 3.5 (already correct) | Regression | Accidental collateral change while touching the void function | Existing M6/M16 reversal tests pass unmodified |
| N | Sales correction paths unaffected | Sale return/void behavior unchanged | Policy 3.5 (non-application) | Regression | Accidental scope creep into sales corrections | Existing M5 return/void tests pass unmodified |
| O | Store isolation on the fixed void path | A store-scoped caller cannot void another store's invoice, before or after this change | Existing `_enforce_store_access` invariant | Service/adversarial | The date-logic change accidentally touches the store check | `ForbiddenError` for a cross-store `caller_store_id`, exactly as before |
| P | Inter-store transfer accounting unaffected | `Inventory In Transit` postings unchanged by this milestone | Policy 3.6 (already correct) | Regression | Unrelated drift during this milestone's change | Existing M8 transfer-accounting tests pass unmodified |
| Q | Consolidated (`store_ids`) reporting unaffected | Multi-store report aggregation unchanged | Policy 3.6 (already correct) | Regression | Unrelated drift during this milestone's change | Existing M11 `store_ids` tests pass unmodified |
| R | Concurrent void attempts | Two concurrent void requests for the same invoice never both succeed | Existing `with_for_update()` invariant | Concurrency | The date change weakens the existing row lock | Exactly one void succeeds; the second sees the already-VOIDED status and no-ops |
| S | Audit trail for the fixed void | The void's audit log entry is unaffected in shape by the date change | Existing audit invariant | Service | The fix drops or alters audit fields | Same `voided_by`/`reason` fields recorded as before |
| T | GL/report reconciliation after the fix | Trial balance still balances (`Σdebit == Σcredit`) after a void posts on a different date than before | Core double-entry invariant | Service | The date fix somehow unbalances the entry | Unaffected — `_purchase_invoice_journal_lines(reverse=True)` guarantees balance regardless of date |
| U | Migration/seed correctness | No migration or seed change accompanies this fix | Policy: M23B is code-only | Migration/regression | An unnecessary migration is introduced for a pure logic change | `alembic heads` unchanged; no new revision file |

21 sessions total (A–U), exceeding the requested "A–Q or more."

---

## 8. Explicit unresolved decisions

None. Every policy area the M23A brief listed was resolved above using
repository evidence (principally `TECHNICAL_BLUEPRINT.md` Section G,
previously un-consulted in this accounting sub-thread, plus the unanimous
M4/M11/M22/M23 record). Where the brief's own instructions explicitly permit
a decisive "not required" answer without inventing a subsystem (opening
balances, fiscal year), that path was taken and documented as a decision,
not left open.

If a future stakeholder conversation changes the underlying business
assumption this policy rests on — most importantly, whether each Store is
genuinely a separate legal/tax entity or all stores belong to one company
(`docs/M23_DISCOVERY.md` Phase 4's informational gap, not fully closable
from source code alone) — decisions 3.1, 3.2, 3.3, and 3.6 would need to be
revisited from that new information, not from anything this repository
currently contains.

---

## 9. Risks and assumptions

- **Assumption:** `TECHNICAL_BLUEPRINT.md` remains the authoritative
  statement of this product's mission even though it predates M4–M22 and
  was never updated to reflect them. Risk: if a stakeholder has, outside
  this repository, since decided the business now wants full financial
  statements (e.g., for a future audit, loan application, or investor
  requirement), that decision would not appear anywhere in this codebase
  and this policy would need to be revisited. Nothing in the repository
  suggests this has happened.
- **Assumption:** the legal/tax relationship between stores (one company vs.
  several) does not change the NOT-REQUIRED decisions in Section 3, only
  Equity's *scope if it were ever built* (3.6). This is called out
  explicitly rather than assumed silently.
- **Risk of the one real change (3.5):** none identified beyond ordinary
  regression risk, mitigated by Sessions A–F and D/M/T above (amount
  fidelity, unrelated-path regression, and balance invariants all
  explicitly re-verified).
- **Risk of documenting rather than building opening balances (3.3):** if
  the business does need to onboard an existing store sooner than expected,
  the extension point is documented but not implemented — this is a
  deliberate, evidence-based deferral (per the brief's own instruction),
  not an oversight.
