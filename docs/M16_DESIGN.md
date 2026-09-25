# M16 Design — Administrative & Reporting UI Completion

## 1. Exact M16 scope

M16's primary objective (per the M16 discovery audit's recommendation) is to move three
backend-complete-but-frontend-placeholder capabilities to usable ERP functionality:

1. **Reports** — `ReportsPage.tsx` currently renders `PlaceholderPage`; the backend
   (`reports/service.py`, 1801 lines, 80+ tests) is fully built.
2. **Users / RBAC administration** — `UsersPage.tsx` currently renders `PlaceholderPage`.
3. **Store Settings** — `SettingsPage.tsx` currently renders `PlaceholderPage`.

Before any UI work, a bounded Phase 0 pre-implementation hardening pass addresses the ten
confirmed defects from the M16 discovery audit (§2 below), **re-validated against current
code first**, per the task's explicit instruction not to blindly trust the discovery
report's claims.

## 2. Phase 0 hardening scope — validated, then addressed

Each item was re-traced against the actual current code before any fix was written.

| # | Item | Finding on re-validation | Action taken |
|---|---|---|---|
| 1 | AP payment/credit-note correction path | **Confirmed real** — no reversal capability existed for `SupplierPayment`/`SupplierCreditNote`. | Built (§3 below). |
| 2 | `CASH_SHIFT_VARIANCE` reversal protection | **Discovery audit's claim did NOT hold up.** `CASH_SHIFT_VARIANCE` was already present in `AUTOMATED_SOURCE_TYPES` (`accounting/models.py`) and `reverse_journal_entry` already refused it correctly. No code change made. | Added a regression test (`test_shifts.py::test_cash_shift_variance_cannot_be_reversed_via_generic_journal_reversal`) proving the invariant, since none previously exercised this exact path. |
| 3 | Missing `sales.shift_id`/`sale_returns.shift_id` indexes | **Confirmed real** — M15 added both FK columns with no index, unlike every sibling FK in this codebase. | Added `ix_sales_shift_id`/`ix_sale_returns_shift_id` in migration `e1a681c4aba3`. |
| 4 | `create_stock_adjustment` missing service-layer store isolation | **Confirmed real.** | Added `caller_store_id` param + `_enforce_store_access` call; endpoint updated to pass it. Adversarial test added. |
| 5 | `get_shift`/`list_shifts` missing service-layer defense-in-depth | **Confirmed real** (both endpoints already enforced it themselves; the service layer alone did not). | Added optional `caller_store_id` params; endpoints now pass it through, preserving identical observable (404) behavior. |
| 6 | Cash-movement audit linkage | **Confirmed real** — `CASH_MOVEMENT_CREATED` events captured `movement_type`/`amount`/`reason` but not the movement's own id. | Added `movement_id`/`client_transaction_id` to the audit event's `after` payload. |
| 7 | Expected-cash edge case (cash sale, non-cash refund) | **Traced end to end — this is correct, intentional behavior, not a bug.** See §4 below. | No code change. Regression test added proving and documenting the intended behavior. |
| 8 | Operational-vs-GL return-date divergence | **Confirmed real, and worse than initially described** — `SaleReturn` had no `return_date` column at all; the GL used a value that was never persisted operationally. See §5 below. | Added `SaleReturn.return_date` (nullable, additive); `reports/service.py` now buckets by it (coalesced with `created_at` for pre-M16 rows). |
| 9 | CI infrastructure coverage (`deploy/tests/`) | Confirmed absent from `.github/workflows/ci.yml`. | See §9 (Phase 8 / CI section) below. |
| 10 | Migration safety | N/A — a standing requirement, not a one-time item. | Every M16 migration follows the established guard-first-downgrade, single-head, up/down/up-tested discipline (verified via `test_migrations.py`). |

### 2.1 New finding during Phase 0 tracing: `get_supplier_statement` reversal blindness

Not in the original ten items, but surfaced while tracing item 1's "what happens to
supplier statements and aging" requirement: `get_supplier_ap_summary`/`ap_aging` derive
live from `PurchaseInvoice.amount_paid`/`amount_credited` (correctly reflect a reversal
automatically), but `get_supplier_statement` reconstructs its running balance from raw
`SupplierPayment`/`SupplierCreditNote` event rows with **no awareness that either could be
reversed** — a reversed payment's original `-amount` event remained, with nothing to
offset it, producing a `closing_balance` that would silently disagree with
`get_supplier_ap_summary`'s `total_owed` the moment any reversal existed. Fixed by adding
`PAYMENT_REVERSAL`/`CREDIT_NOTE_REVERSAL` events to the statement's own event
reconstruction, dated at `reversed_at`, restoring the historical "never a cached total"
discipline that function's own docstring commits to.

## 3. AP payment/credit-note correction path — full design

Traced the complete lifecycle per the task's explicit checklist:

- **Operational record**: `SupplierPaymentReversal`/`SupplierCreditNoteReversal` — new,
  append-only tables mirroring `PayrollReversal` exactly (one row per reversed
  payment/credit note, `UNIQUE` on the parent id). The parent row is **never mutated** —
  "was this reversed" is a derived fact from whether a reversal row exists, exactly
  matching `PayrollPeriod.status`'s own never-changes-on-reversal convention.
- **Permanent link**: `reversal_journal_entry_id` FK + `supplier_payment_id`/
  `supplier_credit_note_id` UNIQUE FK. The reversal's GL entry reuses `source_id` = the
  ORIGINAL payment/credit-note's id (mirroring `PAYROLL_REVERSAL`'s own
  `source_id=period.id` convention), so the pre-existing `uq_journal_entries_source`
  partial unique index gives "reversed at most once" as a DB-level guarantee too, on top
  of the domain-row UNIQUE constraint.
- **Supplier balance / invoice outstanding balance**: every allocated invoice's
  `amount_paid` (payment) or `amount_credited` (credit note) is decremented by the
  reversed allocation and `_recompute_invoice_status` is re-run — the SAME function the
  original application uses. Every downstream reader (`get_supplier_ap_summary`,
  `ap_aging`, `_outstanding_balance`) is therefore correct with zero additional code.
- **Supplier statement / aging**: see §2.1 above.
- **Double reversal**: refused. Idempotent-by-existence (a second reversal *attempt*
  against an already-reversed payment is treated as the idempotent case, exactly mirroring
  `reverse_payroll_period`), backstopped by a DB UNIQUE constraint that converts a genuine
  race into a handled `ALREADY_REVERSED` `ConflictError`. Proven under real concurrency
  (`test_two_concurrent_reversal_attempts_exactly_one_creates_a_reversal_row`).
- **Race with another payment/invoice mutation**: the payment/credit-note row is locked
  `FOR UPDATE` first, then every allocated `PurchaseInvoice` in ascending id order —
  identical lock order to `record_supplier_payment`/`create_supplier_credit_note`, so a
  concurrent new payment against the same invoice serializes correctly rather than
  deadlocking or racing.
- **Downstream references**: an invoice with an active payment cannot be voided
  (`void_purchase_invoice`'s existing `INVOICE_HAS_PAYMENTS` guard) — reversing the
  payment first makes voiding possible again; no new conflict is introduced.
- **Credit-note reversal accounting difference**: a `GOODS_RETURN` credit note's original
  posting credited Inventory (the physical return was already recorded by the referenced
  `PurchaseReturn`); its reversal correctly debits Inventory back, but **does not** create
  or undo any inventory *movement* — this mirrors the original posting's own explicit
  scope ("this journal accounts for the AP-side financial consequence only, never a second
  inventory movement"). `PurchaseReturn` itself has no reversal anywhere in this codebase
  (a pre-existing M3/M7 scope boundary M16 does not remove).
- **Approval**: gated by a new permission, `ap.reverse` — see §7.
- **Audit events**: `SUPPLIER_PAYMENT_REVERSED`/`SUPPLIER_CREDIT_NOTE_REVERSED`, entity_type
  reused from the parent (`supplier_payment`/`supplier_credit_note` — both already
  registered in `audit/service.py`'s entity-classification buckets), including the amount
  (unlike payroll's deliberate amount-redaction, which is specific to wage privacy, not a
  general AP convention).
- **Idempotency key**: none client-supplied — mirrors `reverse_payroll_period` exactly;
  idempotency is a domain fact (existence of a reversal row), not a retry-key concern.
- **Failure after transaction**: neither reversal function self-commits (unlike
  `post_payroll_period`/`reverse_payroll_period`, which the M16 discovery audit itself
  flagged as an inconsistency with the rest of the codebase) — they follow AP's own
  dominant flush-only/caller-commits convention instead, so this milestone does not
  propagate that inconsistency into a new function. Proven atomic under forced GL-posting
  failure (`test_failure_during_gl_posting_leaves_invoice_balance_unchanged`).

## 4. Expected-cash / non-cash-refund investigation (item 7)

Traced the exact sale → void/return → shift-close lifecycle. `_compute_expected_cash`'s
`cash_tendered` term has no `Sale.status` filter, so a CASH sale later refunded via a
NON-CASH `refund_method` leaves the original tender counted with nothing to subtract it.

**Conclusion: this is correct, physically accurate behavior, not a bug.** `expected_cash_amount`
models the physical till only. A $20 cash payment genuinely entered the drawer the moment
it was tendered — that fact does not change based on what happens later. If a refund is
subsequently issued via bank transfer, no cash physically leaves the drawer, so the till
legitimately still holds that $20. The apparent "overstatement" the discovery audit
flagged is the till accurately reflecting reality. Reversing this would be the actual bug —
it would make the till's expected balance disagree with what a physically honest cashier
count would show. Documented with a passing regression test proving the intended behavior
(`test_expected_cash_still_reflects_original_cash_tender_when_refund_is_noncash`), per the
task's explicit instruction not to assume the audit's suspicion was correct merely because
it was raised.

## 5. Operational-vs-GL return-date divergence — full design

**Trace**: `sales_summary` bucketed returns by `SaleReturn.created_at` (insertion time).
`post_sale_return_journal` posts using the caller-supplied `return_date` parameter — which,
critically, **was never persisted anywhere on `SaleReturn` itself**, only passed through to
the journal at posting time. A backdated return (recorded today, `return_date` in an
already-reported prior period) would land in different periods on the two reports, with no
way to even retrieve the GL's own date from the operational row afterward.

**Authoritative business date chosen**: `return_date`, matching how `Sale.completed_at` is
already the single date used both for GL posting and every operational sales report — this
brings returns in line with the SAME pre-existing principle, not a new one.

**Migration/data-history boundary explicitly considered**: adding a persisted column is a
schema change. Chosen approach: **nullable, additive, not backfilled**
(`sale_returns.return_date DATE NULL`) — the established low-risk pattern this codebase
already uses repeatedly (e.g. `StockAdjustment.client_transaction_id`). Every reporting use
coalesces to `created_at`'s date when `return_date IS NULL`, so **no pre-M16 return's
historical reporting bucket changes** — only returns created from this migration forward
get the corrected, GL-matching behavior. This avoids the "stop and report the boundary"
case (no backfill, no destructive change) while still fully closing the divergence for all
future returns.

**Verified**: a fixture proving a backdated return lands in the return_date's period for
BOTH `sales_summary` and `accounting_service.profit_and_loss`, with matching COGS figures
(`test_backdated_return_lands_in_return_date_period_matching_gl`).

## 6. Architecture decisions

- **No new state-management framework, no new frontend dependencies** — Phase 2-4 UI reuses
  the existing API client / auth patterns already established (`frontend/src/api/*.ts`,
  the existing fetch-based pattern seen in `frontend/src/api/shifts.ts`).
- **Frontend never recomputes money** — every figure shown is the exact server-provided
  value, formatted for display only.
- **Frontend/backend responsibility boundary**: the frontend never encodes an
  authorization decision, a financial calculation, or a privilege-escalation check —
  every one of those lives server-side and is merely *reflected* (hidden controls,
  disabled buttons) in the UI for a better experience, never relied upon for security.

## 7. Permission matrix (M16 additions)

| Permission | Admin | Manager | Auditor | Others |
|---|---|---|---|---|
| `ap.reverse` (new) | ✓ (via `ALL_PERMISSIONS`) | ✓ (mirrors Manager's existing `accounting.reverse` + full AP write-through-commit chain; no payroll-style conflict-of-interest applies to AP) | — | — |
| `users.manage` (pre-existing, M12; now finally implemented beyond deactivate) | ✓ | — (deliberately Admin-only, unchanged) | — | — |
| `store.settings.read` (new) | ✓ | ✓ | ✓ (mirrors Auditor's universal read-only pattern) | — |
| `store.settings.write` (new) | ✓ | ✓ | — | — |

No new **roles** were created. No permission was granted to a role beyond what its existing
description/scope already justifies (Manager already held the "day-to-day store
operations" description and the full AP write chain; Auditor already held "read-only
access across the system").

## 8. Users/RBAC administration — a real backend gap found during Phase 1 tracing

Before any frontend work, the task's own instruction to trace the actual API surface
before building against it surfaced a significant fact: **the only user-management
endpoint that existed prior to M16 was `POST /users/{id}/deactivate`.** `users.manage`
("Create users and assign roles") was provisioned in M12 but no endpoint ever implemented
list, create, reactivate, or role-assignment — a placeholder permission exactly like
`ACCOUNTING_POST`/`ACCOUNTING_ADMIN`.

Per the same principle the task states for Reports ("If a report does not have a suitable
API, do not silently invent a new calculation in the frontend — identify the backend
gap"), this could not be built as a frontend illusion over a non-existent API. The gap was
closed with the minimal backend `users.manage` was always described as covering:

- `GET /auth/users` (list, store-scoped)
- `POST /auth/users` (create)
- `POST /auth/users/{id}/reactivate` (the counterpart `deactivate_user`'s own M12 docstring
  named as explicitly missing)
- `PUT /auth/users/{id}/role` (change a user's role)
- `GET /auth/roles` (list known roles, for a picker — no new roles, just exposing
  `ALL_ROLES`)

**Privilege-escalation rule** (the task's explicit Phase 3 concern): every mutating
function (`create_user`, `update_user_role`) verifies the target role's permission set is a
**subset** of the acting user's own permissions — `PRIVILEGE_ESCALATION_DENIED` otherwise.
`users.manage` is Admin-only today, so this is defense-in-depth rather than a currently
reachable gap, but it makes the check real the moment any future milestone ever grants
`users.manage` more broadly — the same discipline M14/M15/M16's own override/reversal
checks already follow. **Self-role-change is refused** (`CANNOT_CHANGE_OWN_ROLE`),
mirroring `deactivate_user`'s own self-deactivation refusal exactly (a lone Admin
downgrading their own role would have no way back in through the API at all). No new roles
or permissions were invented beyond the two Settings permissions (§7) — `users.manage`
already existed.

## 9. Store-isolation model (unchanged, reused)

Every new endpoint (`stores.py`, the four new `auth.py` routes) follows the exact
pre-existing pattern: `scoped_store_filter` for list endpoints, a direct 404
(not 403) for a store-scoped caller reading another store's single resource by id
(information-hiding, matching `sales.py`/`ap.py`/`shifts.py`), and an explicit
`ForbiddenError STORE_ACCESS_DENIED` (403) for a store-scoped caller's *write* attempt
against another store — matching the existing split between "can't see it exists" (read)
and "exists, but you may not touch it" (write) used throughout this codebase.

## 10. Reporting source-of-truth rules for the Reports UI (Phase 2)

- **GL-derived** (`accounting_service.trial_balance`/`profit_and_loss`/etc.): the
  authoritative accounting record. Labeled as such in the UI.
- **Operational** (`reports_service.sales_summary` and siblings): a genuinely useful,
  independently-computed figure, not derived from the GL. Per the M16 discovery audit's
  finding, `sales_summary.gross_profit` and `accounting_service.profit_and_loss.gross_profit`
  share an identical field name across two different endpoints with no UI-level
  distinction — **the Reports UI must not present these as interchangeable.** Each report
  card/section is labeled with its category (Operational / GL-derived / Analytical /
  Reconciliation) per the report's actual computation, and the two `gross_profit` figures,
  if ever shown on the same screen, are never presented under one unqualified label.
- **Cached**: none exist in this codebase (`reports/service.py`'s own docstring: every
  report is computed live).

## 11. Settings fields exposed

Only fields with a pre-existing, documented backend column and clear semantics, per the
task's explicit instruction not to invent Settings fields:

| Field | Source | Notes |
|---|---|---|
| `name` | `Store.name` | Basic identity, always required. |
| `address` | `Store.address` | Optional. |
| `timezone` | `Store.timezone` | IANA string, used by HR/attendance date-boundary logic. |
| `attendance_day_boundary_hour` | `Store.attendance_day_boundary_hour` (M10) | 0-23, cross-midnight shift boundary. |
| `return_approval_threshold_amount` | `Store.return_approval_threshold_amount` (M14) | Nullable; `NULL` disables the M14 approval gate for that store. A dedicated `clear_return_approval_threshold` flag disambiguates "leave unchanged" from "explicitly clear it" at the API layer. |

**Deliberately excluded**:
- `is_active` — read-only via this surface; deactivating a store is a far more
  consequential operation than a settings change, with no evidence in this repository of
  what should cascade from it (open shifts, pending POs, active users, etc.).
- Any tax, currency, fiscal, payroll-jurisdiction, or hardware configuration — **no
  evidence exists anywhere in this repository for what these values should be** (per the
  M16 discovery audit's Phase 9/10 findings on HR/payroll and tax jurisdiction remaining
  UNKNOWN). Inventing fields for them would be exactly the "speculative business
  requirement" the task's non-goals forbid.
- Multi-store "subset" manager assignment — no evidence this requirement exists (M16
  discovery audit Phase 11: the two-level `store_id` model is what exists; a genuine
  "authorized for stores 3 and 7 only" role does not).

## 12. Test strategy

See `docs/M16_TESTING_SESSIONS.md` for the full session-by-session record. Summary:
service-layer + HTTP-level tests for every new backend function, matching this codebase's
established dual coverage; genuine `SessionLocal()`-based concurrency tests (not the `db`
fixture's savepoint isolation) for the AP reversal race; failure-injection via
`monkeypatch` for the GL-posting atomicity guarantee; frontend component tests for all
three new screens mirroring the existing `PosPage.test.tsx` pattern.

## 13. Migration requirements

Three migrations, in dependency order:
1. `e1a681c4aba3` — Phase 0 hardening: `sale_returns.return_date`, the two missing shift
   indexes, the two new AP reversal tables, widened `ck_journal_entries_source_type`, and
   the `ap.reverse` permission seed.
2. `4a83c462dbff` — the two new Settings permissions (`store.settings.read`/`.write`),
   split into its own migration since it's a Phase 4 (Settings) prerequisite, not Phase 0
   hardening, keeping each migration scoped to the work that actually needs it.

Both follow the established guard-first-downgrade, single-linear-head,
`ON CONFLICT DO UPDATE/DO NOTHING` permission-seeding discipline verified by
`test_migrations.py`'s full up/down/up cycle.

## 14. Explicit non-goals (restated from the task, honored)

No fiscal/EFD/TRA integration, no jurisdiction-specific payroll, no statutory
withholding, no new currency architecture, no subset-store manager authorization, no
physical till/register hardware, no new roles, no generic settings framework, no
speculative business requirements, no unrelated refactoring or architectural rewrites.
The two new permissions (`ap.reverse`, `store.settings.*`) and the Users-management
endpoint extension were the only additions beyond the original Phase 0 list, and each is
justified in §3/§8/§11 above by an already-documented backend capability or gap, not
invented scope.
