# M22 Testing Sessions — Accounting Integrity and Financial-Period Control

Companion to `docs/M22_DISCOVERY.md` (the discovery that established the
business rules this milestone implements). Scope, per the M22 brief:
implement only the accounting-integrity slice for which a business rule
is established (period-lock, and the P&L operating-expenses fix). Balance
Sheet/Equity and transfer authorization (F7) remain out of scope and
unresolved — nothing in this milestone touches them.

## Baseline (Phase 0)

- Starting HEAD: `953a33f` (M21 implementation commit).
- Working tree: clean before any code change.
- Full backend suite green before any change: 1040 passed (re-confirmed
  after the migration-count test update below, still 1040 passed).
- Single alembic head before any change: `9c4c5a209aa9`.

## Implementation summary

- **`AccountingPeriod`** (`app/modules/accounting/models.py`): a
  denylist-style table, one row per CLOSED date range per store. Non-
  overlap enforced by a `gist` `ExcludeConstraint`
  (`excl_accounting_periods_no_overlap`), the same mechanism
  `EmploymentStatusPeriod`/`EmploymentAssignment`/`CompensationPeriod`
  already use. `UPDATE`/`DELETE` revoked from `erp_app` in the migration
  (`aa9ac6ad7476_m22_accounting_periods_and_pl_expense_.py`) — the same
  carve-out `journal_entries`/`journal_lines` already have — since there
  is deliberately no reopen.
- **`_enforce_period_open`** (`app/modules/accounting/service.py`): the
  single gate. Called from exactly two places — the top of `_post_journal`
  (covers all forward-posting functions and every reversal function that
  routes through it) and inside `reverse_journal_entry` (the one path
  that builds a `JournalEntry` directly). No other code path in the
  codebase creates a `JournalEntry` row (confirmed by the exhaustive
  Phase 1 trace in `docs/M22_DISCOVERY.md`).
- **`close_accounting_period` / `list_accounting_periods`**
  (`app/modules/accounting/service.py`): service functions. Closing is
  create-only, `_enforce_store_access`-gated (mirrors every other
  service-layer bypass guard in this module), overlap surfaced as
  `ConflictError(error_code="PERIOD_OVERLAP")` rather than a raw
  `IntegrityError`, and audit-logged via `audit_service.log_event`
  (`ACCOUNTING_PERIOD_CLOSED`). `accounting_period` added to
  `audit/service.py`'s `_DIRECT_STORE_ENTITY_MODELS` so a store-scoped
  reader can see their own store's period-close events in the audit log.
- **`POST /accounting/periods` / `GET /accounting/periods`**
  (`app/api/v1/endpoints/accounting.py`): gated by the pre-existing,
  previously-unused `accounting.admin` permission (currently Admin-only —
  no role-grant change was needed or made). `GET` is `accounting.read`
  and store-scoped via `scoped_store_filter`, matching every other list
  endpoint in this file.
- **`profit_and_loss()` fix** (`app/modules/accounting/service.py`):
  `operating_expenses` now sums `net_debit` over every `EXPENSE`-type
  account in the trial balance except `ACCOUNT_COGS`, instead of the one
  hardcoded M4-era account. Resolves Finding F2 (P&L excludes labor) and
  the same latent omission for purchase price variance, purchase
  discounts, purchase tax expense, and cash over/short — none of which
  were in `operating_expenses` before this fix either. No schema change;
  derived entirely from the same `trial_balance()` rows already computed.
- **`tests/test_migrations.py`** updated: the upgrade/downgrade/upgrade
  cycle test's table-count checkpoint moved from 67 (M20 head) to 68 (M22
  head, `accounting_periods`), with `M22_HEAD_REVISION` added alongside
  the existing per-milestone revision constants.

## Testing sessions

**Session A — period lifecycle.** `test_close_period_creates_row_and_is_listed`,
`test_close_overlapping_period_for_same_store_rejected` (DB-level
`ExcludeConstraint`, surfaced as `PERIOD_OVERLAP`),
`test_close_period_invalid_range_rejected`.

**Session B — closed-period posting protection (INV-1/INV-2).**
`test_posting_inside_a_closed_period_is_refused` (also asserts zero
`JournalEntry` rows exist before and after the refused attempt — no
partial write), `test_posting_just_outside_a_closed_period_boundary_succeeds`
(both edges: the day after and the day before a closed range).

**Session C — correction/reversal survives a closed period (INV-5).**
`test_reversal_succeeds_even_when_the_original_entrys_date_is_now_closed`
(post an entry, close a period covering its date afterward, reversal
still succeeds because it posts at `date.today()`) and
`test_reversal_is_refused_when_todays_own_period_is_closed` (the one
sharp edge documented in `docs/M22_DISCOVERY.md` Phase 3: closing
*today* blocks same-day corrections too, by design).

**Session D — accounting reconciliation.**
`test_closing_a_period_posts_no_journal_entries` (closing is a
control-plane row, not a GL posting) and
`test_trial_balance_still_balances_after_closing_a_period`.

**Session E — P&L / net income (INV-6/7/8, Question A now in scope).**
`test_profit_and_loss_includes_wage_salary_expense`,
`test_profit_and_loss_sums_multiple_expense_accounts_at_once` (payroll +
purchase price variance + cash over/short together), and
`test_profit_and_loss_never_double_counts_cogs_in_operating_expenses`.

**Session F — Balance Sheet.** Explicitly skipped. No Equity account, no
Balance Sheet, no period-close-to-equity workflow was built (see
`docs/M22_DISCOVERY.md` Phase 5 — BUSINESS DECISION REQUIRED, unresolved).

**Session G — multi-store isolation (INV-3).**
`test_closing_one_store_does_not_block_another_store`.

**Session H — authorization (INV-10).**
`test_admin_can_close_a_period_via_the_api`,
`test_manager_cannot_close_a_period_via_the_api` (Manager holds
`accounting.reverse` but not `accounting.admin`),
`test_close_period_missing_reason_rejected_by_schema`,
`test_close_accounting_period_rejects_cross_store_service_caller`
(service-layer bypass check, mirroring M21 Session C's methodology — a
caller scoped to a different store is refused calling the service
function directly, not only at the HTTP layer),
`test_store_scoped_manager_only_sees_own_stores_periods_via_api`.

**Session I — concurrency.** Not materially different from M21's own
Session E judgment: `_enforce_period_open` is a plain `SELECT` inside the
same transaction as the posting it gates, and `close_accounting_period`'s
only new concurrency surface is the DB-enforced `ExcludeConstraint`
(already proven correct by Session A's overlap test — Postgres itself
serializes conflicting inserts against a `gist` exclusion constraint, the
same guarantee `EmploymentStatusPeriod` already relies on in production).
No new dedicated concurrency test was judged necessary beyond that.

**Session J — failure injection.** Covered by this module's existing
transactional-atomicity guarantee (module docstring: no `post_*` call
site ever calls `db.commit()` itself — a failure anywhere after posting
discards the whole operation when the caller's session closes without
committing). `_enforce_period_open` raises before any row is written, so
there is nothing to roll back in the closed-period-refusal path itself
(confirmed directly by Session B's zero-rows assertion).

**Session K — migration safety.** `tests/test_migrations.py`'s full
upgrade/downgrade/upgrade cycle re-run and green (16/16), table count
68 at head, single head `aa9ac6ad7476`. Additive only: one new table, no
backfill, no existing-row rewrite, no data migration.

**Session L — adversarial.**
`test_closing_a_period_that_already_has_postings_in_it_succeeds`
(closing is about the future, not retroactively invalidating what
already posted).

**Session M — mutation testing.** 5 targets, all caught (RED for the
correct reason), all reverted and diff-verified byte-identical against a
backup taken before mutating:

| # | Target | Mutation | Test(s) that caught it |
|---|--------|----------|------------------------|
| 1 | `_post_journal`'s period gate | Removed the `_enforce_period_open(...)` call | `test_posting_inside_a_closed_period_is_refused` — failed with "DID NOT RAISE ConflictError" |
| 2 | `reverse_journal_entry`'s period gate | Removed the `_enforce_period_open(...)` call | `test_reversal_is_refused_when_todays_own_period_is_closed` — failed with "DID NOT RAISE ConflictError" |
| 3 | `profit_and_loss()`'s expense sum | Reverted to the old hardcoded single-account `operating_expenses` | `test_profit_and_loss_includes_wage_salary_expense` and `test_profit_and_loss_sums_multiple_expense_accounts_at_once` — both failed (`Decimal('0') == Decimal('660.00')`) |
| 4 | `close_accounting_period`'s store-access check | Removed the `_enforce_store_access(...)` call | `test_close_accounting_period_rejects_cross_store_service_caller` — failed with "DID NOT RAISE ForbiddenError" |
| 5 | `POST /accounting/periods`'s permission dependency | Swapped `_admin_permission` for `_read_permission` | `test_manager_cannot_close_a_period_via_the_api` — failed (Manager got 201, expected 403) |

## Regression and quality gates

- Backend: 1040 pre-existing tests + 19 new (`tests/test_m22_accounting_periods.py`)
  = 1059, all green.
- Frontend: unaffected (M22 has no frontend surface by design —
  `docs/M22_DISCOVERY.md` Phase 10 explicitly excludes a period-admin UI
  from scope). 55 tests, lint (`oxlint`), format (`prettier --check`),
  and `tsc -b` all clean.
- `ruff check .`, `black --check .`, `mypy app`: all clean.
- Single alembic head: `aa9ac6ad7476`.

## Limitations and intentionally unresolved items (carried forward, not silently dropped)

- **F7 (transfer creation authorization)**: unchanged from M21. No
  authoritative rule found; not touched by M22.
- **F8 (Reports frontend store-selector gap)**: unchanged from M21,
  out of scope for M22.
- **Balance Sheet / Equity / Retained Earnings**: BUSINESS DECISION
  REQUIRED (`docs/M22_DISCOVERY.md` Phase 5). No Equity account, no
  Balance Sheet, no period-close-to-equity workflow exists after M22.
- **`PURCHASE_INVOICE_VOID`'s original-date exception**: documented,
  not fixed (`docs/M22_DISCOVERY.md` Phase 1/6). Voiding a posted invoice
  whose original `invoice_date` now falls inside a closed period will
  itself be blocked by the same gate, since void posts at the *original*
  date rather than today like every other correction. No business rule
  in scope for M22 requires changing AP void semantics to fix this; it is
  a real, pre-existing-shape risk surfaced by adding the period lock, not
  a regression M22 introduces.
- **No reopen**: a closed `AccountingPeriod` row cannot be reopened.
  Deliberate scope exclusion (undefined reopen semantics is one of the
  brief's own STOP conditions), not a bug.
