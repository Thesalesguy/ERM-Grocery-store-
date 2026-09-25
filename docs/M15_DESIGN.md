# M15 — Cashier/Till Shift Session Management: Design

## 0. Scope and origin

The M15 Discovery & Architecture Audit (conducted before this milestone began) established that mission capability #13 — "cashier/register/till operations" — was the only mission capability still flatly "missing" in the repository, evidenced directly by the code: `Sale` ties cash handling only to `cashier_id`+`store_id` with no time-bounded session, the GL's own `Cash on Hand` account description ("physical cash held at the till") implies a till concept that has no corresponding model, and no test file anywhere references till/register/shift/cash-count. This document is the design for closing that gap.

Also closed as a small, isolated **pre-M15 hardening** fix (its own migration/commit, not counted as M15 functionality): `inventory/service.py::create_stock_adjustment` was the one financial-posting flow in the codebase without a `client_transaction_id` idempotency key, found during the same discovery audit. See §10.

## 1. Business rules

- A **cashier shift** (`CashierShift`) is a bounded cash-handling session for one cashier at one store: an opening float, an OPEN status, and — once closed — a physical cash count, a server-computed expected cash figure, and a variance.
- **One active shift per cashier at a time**, enforced by a partial unique index (`status = 'OPEN'`) — this milestone's deliberate bounded scope, not a general multi-till model. A store may have many cashiers each with their own concurrent shift.
- Shift attribution to a `Sale`/`SaleReturn` is **opportunistic, never mandatory**: if the cashier has an OPEN shift at the moment of finalization, the sale/return is attributed to it (`shift_id` set); if not, the sale/return proceeds exactly as it did before M15 (`shift_id = NULL`). No existing POS test needed to change to accommodate M15 — this was the deciding scope boundary (see the design-validation report at the top of this milestone's session transcript).
- **Cash movements** (`CashMovement`, `PAID_IN`/`PAID_OUT`) may be recorded against an OPEN shift for float top-ups, petty disbursements, etc.
- **Closing a shift** computes `expected_cash_amount` from the shift's own authoritative `Sale`/`Payment`/`SaleReturn`/`CashMovement` rows (never a separately-maintained running balance — see §4), accepts a `closing_counted_amount`, and freezes `variance_amount = closing_counted_amount − expected_cash_amount`.
- **Self-close vs. override-close**: the shift's own cashier may always close it (`shift.manage`). Closing a *different* cashier's shift additionally requires `shift.override` (Manager/Admin). This is a plain authorization check, not an M14-style inline-credential relay — see §7.

## 2. Data model

### `CashierShift`
| Column | Type | Notes |
|---|---|---|
| `store_id`, `cashier_id` | FK | |
| `status` | `OPEN`/`CLOSED` | |
| `opening_float` | `NUMERIC(12,2)` | ≥ 0 |
| `opened_at` | timestamptz | |
| `client_transaction_id` | unique | open-request idempotency key |
| `closed_at`, `closing_counted_amount`, `expected_cash_amount`, `variance_amount`, `closed_by` | nullable | set together, atomically, at close |
| `close_client_transaction_id` | unique, nullable | a **separate** idempotency key from the open key, mirroring how `Sale.client_transaction_id`/`SaleReturn.client_transaction_id` are two independent keys on two related-but-distinct rows |

CHECK constraints (DB-level, not just application code — the same "belt-and-suspenders" discipline `ck_sales_grand_total_consistent` uses):
- `status`/closing-fields consistency: every closing field is NULL together (OPEN) or NOT NULL together (CLOSED) — never half-closed.
- `variance_amount = closing_counted_amount − expected_cash_amount` whenever CLOSED — ties the frozen variance to its own stored inputs, so a future bug can never write an inconsistent value.
- A partial unique index `uq_cashier_shifts_one_open_per_cashier` on `cashier_id` `WHERE status = 'OPEN'` — the real enforcement of "one active shift per cashier," the same partial-unique-index technique `journal_entries.uq_journal_entries_source` already uses for its own conditional-uniqueness requirement.

**Why `expected_cash_amount`/`variance_amount` are persisted rather than computed at read time** (explicitly required to justify, per this milestone's own instructions): `PurchaseInvoice.balance_due` is deliberately *not* stored, because `amount_paid` keeps changing after the invoice is created — an ongoing balance. `CashierShift.expected_cash_amount` is the opposite case: it is computed exactly once, inside the atomic close transaction, and **cannot** change afterward, because no further `Sale`/`SaleReturn`/`CashMovement` can ever attach to a CLOSED shift (the close transaction holds the shift row's lock for its whole duration — see §6). This is the same "freeze a derived fact at the moment of completion" pattern `Sale.grand_total` already uses, not an independently-maintained second source of truth — it is tied by CHECK constraint to its own stored inputs and derived by a single pure function (`_compute_expected_cash`) from the shift's own authoritative child rows.

### `CashMovement`
| Column | Type | Notes |
|---|---|---|
| `shift_id` | FK | |
| `movement_type` | `PAID_IN`/`PAID_OUT` | |
| `amount` | `NUMERIC(12,2)` | > 0 |
| `reason` | text | required |
| `created_by` | FK | |
| `client_transaction_id` | unique | |

Append-only by application-code discipline (no route or service function ever edits one after creation) — the same convention `InventoryMovement` and sale line items already follow.

### `Sale.shift_id` / `SaleReturn.shift_id`
Nullable FK to `cashier_shifts.id`, on **both** models, not derived one from the other. `Payment` gets no `shift_id` of its own — every `Payment` row for one `Sale` is created in the same checkout event, so `Sale.shift_id` alone is sufficient to attribute every payment on the sale. `SaleReturn` needs its **own** `shift_id`, separate from the original sale's: a return is a distinct transactional event that can happen under a different (even a later day's) shift than the original sale, and its own `refund_method`/`refund_amount` affect physical cash independently.

## 3. Shift association — the critical integration point

`finalize_sale`/`create_sale_return` look up the cashier's/processing-user's active shift via `shifts_service.lock_active_shift_for_cashier` (not the plain, unlocked `get_active_shift_for_cashier` used by read-only endpoints) and set `shift_id` accordingly. See §6 for why this must be the *locked* variant.

## 4. Expected-cash formula

```
expected = opening_float
    + Σ(cash Payment.amount across Sales attributed to this shift)
    − Σ(Sale.change_due across Sales attributed to this shift)
    − Σ(refund_amount of CASH-method SaleReturns attributed to this shift)
    + Σ(PAID_IN CashMovement.amount)
    − Σ(PAID_OUT CashMovement.amount)
```

Derived, not invented, from `finalize_sale`'s actual payment semantics: a CASH `Payment.amount` is what was physically **tendered**, not the amount applied to the sale (e.g. a $100 sale tendered with $150 cash has `Payment.amount == 150`, `Sale.change_due == 50`). `finalize_sale`'s own `OVERPAYMENT_NOT_ALLOWED` rule guarantees change can only ever originate from a cash overpayment, so subtracting the sale's whole `change_due` against its cash tender is always correct — proven end-to-end by `test_expected_cash_handles_change_correctly` (physical cash increase for that sale is exactly 150 − 50 = 100, the sale total, not 150).

Non-cash tenders (CARD/MOBILE_MONEY/BANK_TRANSFER/OTHER) never appear in this formula — they don't touch the physical till (`test_expected_cash_excludes_noncash_tender`).

## 5. Over/short sign convention

`variance_amount = closing_counted_amount − expected_cash_amount`. Positive = overage (more cash physically present than the books expect); negative = shortage. Tested explicitly for both signs.

## 6. Concurrency — the load-bearing design decision

**Sale finalization racing shift closure** is the highest-stakes race in this milestone: without protection, a sale could read "shift X is open," then `close_shift` (which itself takes `FOR UPDATE` on the shift row) could close X and freeze `expected_cash_amount` before the sale commits — the sale would attach `shift_id = X` to an already-frozen, now-CLOSED shift without ever being counted, silently understating the till.

Fixed by having `finalize_sale`/`create_sale_return` acquire the shift row `FOR UPDATE` too (`lock_active_shift_for_cashier`), filtered on `status = 'OPEN'`. Under PostgreSQL's default READ COMMITTED isolation, a `SELECT ... FOR UPDATE WHERE status = 'OPEN'` **re-evaluates the WHERE clause against the latest committed row** once the lock is actually granted — so if `close_shift` won the race and committed (flipping status to CLOSED) while the sale's lock request was blocked, the sale's query correctly returns no row once unblocked, and the sale simply proceeds with `shift_id = NULL` (opportunistic, never a reason to fail the sale — per the scope boundary in §1). If the sale wins the race instead, it holds the shift's lock until its own commit, so `close_shift` (blocked behind it) can only proceed after the sale is durably committed and therefore counted. **Proven, not just asserted**: `test_sale_finalization_racing_shift_close_is_never_lost` deterministically forces both orderings using a barrier + a controlled lock-hold delay against real PostgreSQL connections, and asserts the sale is always attributed and always counted in the resulting variance.

The same lock-based serialization makes `record_cash_movement` vs. `close_shift` races resolve deterministically too (`test_cash_movement_racing_shift_close_resolves_deterministically`) — no lost update, no partial state, in either winning order.

## 7. Authorization: why M14's approval pattern does not apply here

M14's return/void approval gate needs *inline credential re-verification* because the approver is a **different, unauthenticated-in-this-request** principal (the initiating cashier's own HTTP request carries the approver's username/password as request fields, since the approver isn't the one logged in for that call).

Closing another cashier's shift is structurally different: the **manager themselves** is the one making the authenticated API call, via their own normal JWT session. There is no second, different principal supplying credentials inline — it is a plain authorization-scope question ("is this authenticated caller allowed to act on someone else's shift?"), answered by a `shift.override` permission check, re-checked in the service layer (not just at the endpoint) exactly the way `_resolve_return_approval` re-checks `sales.return.approve` — so a direct service call bypassing the HTTP layer cannot skip it either (`test_direct_service_call_cannot_bypass_override_check`).

No new rate limiter was needed for this reason — there is no credential-verification surface here to throttle.

## 8. Permissions

| Code | Grants |
|---|---|
| `shift.manage` | Open/close/record-cash-movement on **one's own** shift | Cashier, Manager, Admin |
| `shift.read` | View shift history/reconciliation detail | Cashier, Manager, Admin, Auditor |
| `shift.override` | Close / record a cash movement on a **different** cashier's shift | Manager, Admin |

`shift.read` is granted to Cashier alongside `shift.manage`, mirroring the existing `pos.use`+`sales.read` pairing — operating something and viewing your own history of it go together everywhere else in this permission matrix (discovered as a real, missing gap during the security testing session, not designed in up front — see `M15_TESTING_SESSIONS.md`).

## 9. GL treatment

A shift-close variance is posted **only when non-zero** (an entry with two zero-amount lines would also be rejected by `journal_lines`' own `debit > 0 OR credit > 0` CHECK — there is no financial event to record on exact reconciliation). One new account, **Cash Over/Short** (`5910`, EXPENSE, DEBIT-normal but routinely carries a credit balance for a run of overages) — a single contra-style account for both directions, mirroring `Purchase Price Variance`'s existing precedent rather than Inventory's separate Gain/Shrinkage split, since a cash variance is one economic question ("did the till reconcile"), not two structurally different event types. Positive variance: `Dr Cash on Hand / Cr Cash Over/Short`. Negative: the reverse. Source type `CASH_SHIFT_VARIANCE`, one entry per shift close (the `uq_journal_entries_source` partial unique index backstops against double-posting, same as every other automated source type).

**Cash movements are deliberately NOT posted to the GL on their own.** What specific expense/asset category a `PAID_OUT` serves (a petty-cash disbursement, a courier payment, a bank deposit) is not evidenced anywhere in this repository, and inventing one would be inventing an unspecified business rule — the same "do not invent" discipline this milestone's own audit applied to the tax/jurisdiction question. The amount is never lost — it's folded directly into `expected_cash_amount` at close time — just not yet double-posted to a guessed GL account. See §11.

## 10. Pre-M15 hardening: stock adjustment idempotency

`inventory/service.py::create_stock_adjustment` lacked a `client_transaction_id` — the one financial-posting flow in the codebase (of sales, sale returns/voids, AP invoices/payments/credit notes, payroll postings, transfers) without one. Fixed by adding a nullable, unique `client_transaction_id` column, mirroring `Sale.client_transaction_id`'s exact fast-path-lookup + `IntegrityError`-recovery pattern. Nullable (not required) because `post_stock_count`'s internal per-variance-line calls have no natural per-call client key of their own — that path's idempotency is already provided by `StockCount`'s own status-based checks, so a caller with no key to give gets exactly the pre-hardening behavior, unchanged. Its own migration/commit, kept separate from M15's own migration so M15 stays scoped to the shift feature.

## 11. What M15 deliberately did not build

1. **A general multi-till model.** One active shift per cashier is this milestone's explicit, bounded scope — no per-register/per-drawer concept beyond that.
2. **Mandatory shift enforcement on checkout.** A sale never fails or is blocked for lacking an open shift — opportunistic attribution only (§1). Making it mandatory would have broken every existing POS test that doesn't open one first, which this milestone's own "no pre-existing test may be weakened" rule forbids working around.
3. **GL posting for individual cash movements.** See §9 — no evidenced category to post them to; folded into the shift-level variance instead.
4. **A currency model.** No currency column exists anywhere in this schema (a pre-existing, cross-cutting gap this milestone doesn't attempt to close); all amounts are the store's implicit single currency, same as every other monetary field in the codebase.
5. **Hardware integration** (physical cash-drawer trigger, receipt printer) — out of scope, matches the blueprint's own "core POS, not full fiscal/hardware integration" precedent from M2.
