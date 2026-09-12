# M10 — HR / Workforce / Payroll: Design

**STOP POINT: this document and the accompanying chat report are the
entire M10 deliverable so far. No model, migration, service, endpoint,
or frontend code has been written. Phase 20 of the task explicitly
requires a design review before implementation begins.**

## 0. Phase 0 findings (verified against the actual repository, not prior reports)

Baseline re-confirmed in this session, from a cold container:
- Branch `claude/grocery-erp-pos-architecture-h8a53g`, git status clean,
  HEAD at `3372a74` (M9 hardening audit rev 2).
- PostgreSQL 16 had to be started fresh (`pg_ctlcluster 16 main start`);
  once up, `alembic current` confirmed `36ec624cf083` (M9) — the actual
  migration head, matching the M9 hardening audit's own claim.
- Backend: **515/515** tests pass. Frontend: **30/30** tests pass.
  `ruff check .`, `black --check .`, `mypy app`, `tsc -b`, `oxlint`,
  `vitest run`, `vite build` all clean. This is the verified starting
  point; M10 changes nothing about it (no M9 code touched).

Architecture actually inspected (not assumed) this session:

- **`app/modules/auth/models.py`**: `Store` (id, name, address, timezone,
  is_active — no currency/jurisdiction field on the store itself, despite
  `TECHNICAL_BLUEPRINT.md` mentioning per-store settings including
  currency live in a separate `store_settings` key-value table, not
  columns on `Store`). `User` (id, `store_id` **nullable** — nullable
  means "not tied to one store," e.g. Admin/Manager/Auditor roles that
  operate cross-store — username, email, password_hash, full_name,
  is_active, last_login_at). `Role`/`Permission`/`RolePermission`/
  `UserRole` — a standard many-to-many RBAC join, permission codes are
  bare strings like `"products.read"`. **There is no Employee, HR, or
  payroll concept anywhere in the current schema.**
- **`app/modules/audit/models.py`**: `AuditLog` — append-only
  (`UPDATE`/`DELETE` revoked at the DB grant level, per its own
  docstring and the M1 migration), `user_id` nullable FK to `users`,
  generic `entity_type`/`entity_id`/`before_state`/`after_state` JSON
  columns. `app.modules.audit.service.log_event` is the one function
  every other module's service layer calls — confirmed by grep across
  purchasing/transfers/replenishment/ap.
- **`app/modules/accounting/models.py` + `service.py`**: `Account`
  (code-seeded global reference data), `JournalEntry` (`store_id`
  required, `posting_date`, `entry_type` fixed forever at insert —
  `STANDARD` or `REVERSAL`, `source_type` + `source_id` naming the
  operational event, a **partial unique index on
  `(source_type, source_id)` for `entry_type = 'STANDARD'`** — this is
  the DB-level duplicate-posting backstop I will reuse verbatim),
  `JournalLine` (`Numeric(14,6)`, exactly one of debit/credit non-zero
  per line, a deferred DB trigger enforcing `SUM(debit) = SUM(credit)`
  per entry). The posting convention, confirmed by reading
  `post_supplier_payment_journal`/`post_transfer_shipment_journal` in
  full: every `post_*_journal` function is called **inline**, inside the
  same operational service function, before that function's own final
  `db.flush()` — never a separate commit, never called from a route
  handler directly. `AUTOMATED_SOURCE_TYPES` is a closed tuple; adding to
  it is exactly how each milestone (M4 through M9) has plugged a new
  operational domain into the one ledger. `reverse_journal_entry` refuses
  to reverse anything in `AUTOMATED_SOURCE_TYPES` — every automated
  domain instead gets its own operational-aware void/reversal function
  (e.g. `post_purchase_invoice_void_journal`), because a generic journal
  reversal would silently diverge the operational and accounting
  ledgers. **This is the exact shape M10's payroll reversal must follow.**
- **`app/modules/ap/models.py` + `service.py`**: `PurchaseInvoice` is the
  closest existing analogue to a "payroll period" — a batch financial
  document with an explicit status lifecycle
  (`DRAFT → POSTED → PARTIALLY_PAID → PAID`, plus `VOIDED`) that posts
  one journal entry on its `DRAFT → POSTED` transition.
  `post_purchase_invoice` (read in full) is idempotent **by state**
  (calling it again on an already-POSTED invoice is a no-op returning
  current state — not a separate idempotency key), and locks every
  affected parent row **in ascending id order** before touching children,
  with a **post-lock re-check** of status (the M5-discovered
  "a racing caller already committed while I waited" pattern, reused
  everywhere since). This is the concrete precedent for the Payroll
  Period lifecycle and its posting transaction.
- **`app/modules/sales/models.py`**: `Sale.cashier_id` is a nullable FK
  directly to `users.id` — confirming that every existing operational
  module identifies "who did this" via `User`, never via any employee
  concept. There is no precedent to preserve here beyond "actors are
  Users"; M10 introduces the first genuinely new party (the paid
  workforce) that is deliberately **not** the same thing as a system
  actor.
- **Purchasing (M3), Transfers (M8), Replenishment (M9)**: already
  exhaustively verified across the M8/M9 sessions in this same
  conversation (effective-dated pricing via `SupplierProduct`,
  deadlock-safe ascending-id-order locking, `client_transaction_id`
  idempotency columns with a DB `UNIQUE` constraint + `IntegrityError`
  recovery, the non-committing-`_inner` + thin-committing-wrapper split,
  `pg_advisory_xact_lock` for a lock with no row to hold yet). These
  patterns are reused below rather than re-derived.
- **Migration conventions**: a strictly linear `down_revision` chain,
  one file per milestone (occasionally two — a "core" and a
  "hardening" revision), `revision`/`down_revision` as plain string
  literals, guarded destructive downgrades that raise loudly before any
  `DROP`/`ALTER` when real data would be lost (see M7/M8/M9's own
  guards). M10 will follow this exactly: one migration head appended
  after `36ec624cf083`.
- **Frontend**: `App.tsx`/`navigation.ts` are a single source of truth
  for routes + sidebar items, each gated by one permission string via
  `RequirePermission`; pages call a typed `api/<module>.ts` client.
- **No jurisdiction, country, or statutory tax authority is named
  anywhere in the project's mission documents** (`TECHNICAL_BLUEPRINT.md`
  explicitly says "Single local currency, configurable via config table
  ... Not mentioned in spec" for currency, and never names a country or
  tax regime). Per Phase 7's explicit instruction, this design therefore
  treats all statutory deduction/contribution rules as **externally
  configured, effective-dated data** — never a hardcoded formula for a
  specific country's tax code.

## 1. The identity question: Employee vs. User

**Decision: Employee is a separate entity from User, related by an
optional, nullable, unique one-to-one foreign key
(`Employee.user_id → users.id`, nullable, unique).** Neither table gains
the other's columns. This is not a casual choice — reasoning below,
organized by the consequence areas the task named explicitly.

**Why not the same entity (extend `User` with HR columns)?**
Every current caller of `User` (auth, RBAC, `Sale.cashier_id`, audit
`user_id`) needs exactly one thing: "who is acting, and what may they
do." Payroll needs an entirely different, much more sensitive thing:
"who is being paid, how much, and what is their employment history."
Fusing them means:
- A person who is paid but never logs in (a warehouse stocker with no
  ERP account) would need a phantom `User` row anyway, wasting login
  credentials on someone who has none, or the schema needs `User` to
  become fully optional-login, which is a much larger blast-radius
  change to an already-hardened auth module for no HR benefit.
- A person who logs in but is not paid by this business (an external
  bookkeeper with a read-only Auditor account, a franchise owner's own
  Admin account) would need employment/compensation columns that are
  meaningless for them, or a "not really an employee" escape hatch,
  which is exactly the kind of nullable-everything anti-pattern that
  makes a table's meaning ambiguous.
- Security/DB-grant separation becomes impossible: the project already
  has precedent for column-level sensitivity handled via **table**
  separation and DB grants (audit_logs' revoked UPDATE/DELETE,
  accounting's revoked UPDATE/DELETE on journal tables) — compensation
  and any future PII (tax ID, banking details) belong in tables that can
  have their own, stricter runtime-role grants, never bolted onto the
  authentication table every request already touches.

**Why not a strict 1:1 requirement (every Employee must have a User,
every User must have an Employee)?**
- Real grocery-store staffing includes people who are paid but do not
  need system access at all (most hourly stockers/baggers in a
  multi-store grocery chain never touch a terminal outside the POS,
  which is itself keyed to a cashier `User`, not to "clock in as
  yourself" — attendance in this design is captured by employee, not by
  authenticated session, see Phase 5).
- Some `User` accounts are legitimately not employees at all (a
  contracted bookkeeper, an external auditor given `AUDIT_READ`, a
  vendor-facing integration account in the future). Forcing every `User`
  to have an `Employee` row would require fabricating fake HR records
  for these, corrupting real headcount/payroll reporting.

**Consequences, addressed explicitly:**
- **Security**: authentication credentials and RBAC never need to touch
  compensation data to function, and vice versa — a database role that
  can read `employees`/`compensation_periods` need not be able to read
  `password_hash`, and the reverse. `Employee.user_id` is a plain
  nullable FK, never a shared primary key, so there is no risk of a
  cascading delete removing payroll history if a `User` row is ever
  deleted (deletion of `User` rows does not happen anywhere in this
  codebase today — deactivation via `is_active` is the norm — but the FK
  direction still matters for defense in depth).
- **Payroll**: every payroll calculation, journal posting, and payslip
  is keyed to `Employee.id`, which never changes and never depends on
  whether a login exists. A payroll run for a terminated employee with a
  since-deleted... no, since-deactivated `User` account still resolves
  correctly, because payroll never joins through `User` for anything
  except the optional "who can log in and see their own payslip"
  convenience feature.
- **Audit**: the *actor* of an HR/payroll action (who approved this
  payroll period) is always a `User` (via `created_by`/`approved_by`
  columns exactly like every other milestone's audit columns). The
  *subject* of that action (which employee's compensation changed) is
  always an `Employee`. These are never the same column, and a payroll
  action's audit trail names both independently — exactly like
  `ReplenishmentPlan.approved_by` (a User) versus `ReplenishmentPlan.
  product_id`/`destination_store_id` (the subject) in M9.
- **Termination**: terminating an employee is purely an HR-domain event
  (a new row in the employment-status effective-dated history, Phase 3)
  and, as a *separate*, explicitly-triggered side effect, deactivates
  the linked `User.is_active` if one exists (mirroring
  `set_product_active`'s own pattern — a dedicated, audited
  status-transition function, never a cascading trigger). The two are
  sequenced (HR event first, login disable second, both in one
  transaction) but remain conceptually and structurally independent, so
  a business rule like "disable login immediately, but employment status
  shows TERMINATED effective at the end of the pay period" is
  representable without a schema change.
- **Rehire**: the same `Employee.id` and `employee_number` are reused —
  rehire is a new effective-dated employment-status period
  (`ACTIVE`, starting the rehire date) appended after the prior
  `TERMINATED` period ends, on the *same* `Employee` row. This is only
  clean because `Employee` never depended on a live `User` row to exist;
  the person's HR identity persisted through the gap. If the rehired
  employee needs system access again, either the old `User` row is
  reactivated (`is_active = true`) and re-linked, or — if enough time
  passed that the old account was fully decommissioned — a new `User`
  row is created and `Employee.user_id` is updated to point at it. Either
  way, `Employee.id`, `employee_number`, and every historical payroll
  record are completely undisturbed by which `User` row (if any) is
  currently linked.
- **Historical records**: because payroll/attendance/compensation all
  key off `Employee.id` (never `User.id`), nothing about a `User` row's
  lifecycle (deactivation, a hypothetical future rename/re-link) can ever
  retroactively change what a historical payslip meant.

## 2. Data taxonomy: master, transactional, derived

This separation is load-bearing for every later phase (idempotency,
concurrency, immutability), so it is stated once, precisely, here.

| Tier | Definition | M10 examples | Mutability rule |
|---|---|---|---|
| **Master data** | Slowly-changing reference facts about *what exists* | `Department`, `Position`, `DeductionType`, `ContributionType` | Editable in place (like `ProductCategory`/`TaxRate`) — these are catalogs, not history |
| **Master data, identity** | The one row that IS a person, for their whole tenure | `Employee` | Only truly-immutable identity fields (`employee_number`, a stable internal ID) never change; everything that *can* legitimately change over time (store, department, position, compensation, status) is modeled as history (below), never as a mutable column on this row |
| **Effective-dated history** | "What was true, and when" | `EmploymentAssignment` (store/department/position/manager), `CompensationPeriod`, `EmploymentStatusPeriod` | Insert-only. A "change" is always a new row with a later `effective_from`; the prior row's `effective_to` is set to close it. Never an `UPDATE` of a past period's substantive values. |
| **Transactional data** | A discrete, dated event that happened once | `AttendanceRecord` (a clock-in/out or a manual entry), `PayrollPeriod` (the batch), correction audit entries | Created once; corrections are new rows or explicit correction fields with a reason and actor, never silent overwrites of the original value (mirrors M9's `ReplenishmentPlan` never being deleted, only status-transitioned) |
| **Derived payroll results** | Computed FROM transactional + effective-dated history, for one specific payroll period, and then frozen | `PayrollEmployeeResult`, `PayrollEarningLine`, `PayrollDeductionLine`, `PayrollEmployerContributionLine` | Computed once per period, persisted, and — once the period reaches `POSTED` — **immutable**, exactly like a `Sale` or `JournalEntry` row. A recalculation before posting *replaces* the derived rows (delete-and-reinsert or version-bump, see Phase 6); after posting, the only path is a reversal (a new, compensating derived result + journal), never an edit. |

This is the same discipline `SupplierProduct` (insert-only price history)
and `ReplenishmentPlan` (status-transitioned, never deleted, snapshot
columns frozen at execution) already established in M9 — M10 applies it
to a domain where getting it wrong has direct legal/compliance
consequences (a payslip must be reproducible exactly as issued, forever).

## 3. Entity model (text diagram)

```
Store (existing, M0)
  │ 1
  │
  │ *                                              User (existing, M2)
Employee ──────────────────────────────(0..1 FK, nullable, unique)────┘
  │ 1        id, employee_number (unique), legal_name, display_name,
  │          contact_info (minimal — see Phase 2), hire_date,
  │          created_at/updated_at (TimestampMixin)
  │          — NO mutable store/department/position/status/compensation
  │            columns here; all of those are history, below.
  │
  ├─ * EmploymentStatusPeriod   (effective_from, effective_to, status,
  │                              reason, actor)
  │       status ∈ {ACTIVE, ON_LEAVE, SUSPENDED, TERMINATED}
  │
  ├─ * EmploymentAssignment     (effective_from, effective_to,
  │                              store_id FK→stores, department_id,
  │                              position_id, manager_employee_id
  │                              FK→employees, actor)
  │
  ├─ * CompensationPeriod       (effective_from, effective_to,
  │                              pay_type ∈ {HOURLY, SALARY},
  │                              rate (Numeric), pay_frequency,
  │                              overtime_eligible (bool),
  │                              currency — see "assumptions", actor)
  │
  └─ * AttendanceRecord         (work_date, store_id, clock_in_at,
                                 clock_out_at, source ∈ {CLOCK, MANUAL},
                                 correction_of_id FK→attendance_records
                                 (self, nullable), correction_reason,
                                 corrected_by FK→users, status ∈
                                 {OPEN, CLOSED, VOIDED})

Department (master data)          Position (master data)
  id, name, is_active                id, title, department_id, is_active

DeductionType (master data, effective-dated RATE — see Phase 4/7)
  id, code, name, category ∈ {STATUTORY, VOLUNTARY, EMPLOYER_CONTRIBUTION},
  is_active
DeductionRate (effective-dated configuration for a DeductionType)
  deduction_type_id, effective_from, effective_to, calculation_method
  ∈ {FLAT_AMOUNT, PERCENT_OF_GROSS, PERCENT_OF_TAXABLE, BRACKETED},
  parameters (JSONB — a rate or bracket table; see Phase 7's explicit
  "do not invent statutory rules" boundary)

PayrollPeriod (transactional, the batch)
  id, store_id (nullable — see Phase 10: a period can be store-scoped
  or company-wide, decision below), period_start, period_end,
  pay_date, status (state machine, Phase 6), calculated_at,
  calculated_by, approved_at, approved_by, posted_at, posted_by,
  journal_entry_id (nullable FK→journal_entries, set only once POSTED),
  calculation_client_transaction_id (nullable, unique — idempotency),
  posting_client_transaction_id (nullable, unique — idempotency)

PayrollEmployeeResult (derived, one row per employee per period)
  id, payroll_period_id, employee_id,
  — snapshot of what was used, frozen at calculation time —
  compensation_period_id_snapshot, store_id_snapshot,
  department_id_snapshot, position_id_snapshot,
  — computed —
  regular_hours, overtime_hours, base_earnings, overtime_earnings,
  gross_pay, total_employee_deductions, total_employer_contributions,
  taxable_gross, net_pay,
  status ∈ {DRAFT, FINAL} (FINAL once the period is POSTED — see Phase 6)

PayrollEarningLine (derived, 0..n per PayrollEmployeeResult)
  payroll_employee_result_id, earning_type ∈ {REGULAR, OVERTIME, ...},
  hours (nullable — salaried lines have no hours), rate_used, amount

PayrollDeductionLine (derived, 0..n per PayrollEmployeeResult)
  payroll_employee_result_id, deduction_type_id, deduction_rate_id
  (snapshot FK — see Phase 4's "never mutate historical payroll"),
  amount, is_employer_contribution (bool — reuses one line shape for
  both employee deductions and employer contributions, distinguished
  by this flag and by which GL side it posts to)

PayrollReversal (transactional — the M10 analogue of a REVERSAL journal
entry's operational counterpart, mirroring PURCHASE_INVOICE_VOID)
  id, payroll_period_id (the period being reversed),
  reversal_journal_entry_id FK→journal_entries, reason, reversed_by,
  reversed_at
```

Every effective-dated table above carries the same two columns
(`effective_from date NOT NULL`, `effective_to date NULL` — open-ended
= currently in effect) and the same non-overlap discipline (Phase 3).

## 4. Employment lifecycle (state machine)

```
                 hire
                  │
                  ▼
              ┌─────────┐   leave granted    ┌───────────┐
     ┌───────▶│ ACTIVE  │───────────────────▶│ ON_LEAVE  │
     │        └────┬────┘                    └─────┬─────┘
     │             │                                │
     │   suspend   │                    return from leave
     │        ┌────▼──────┐                         │
     │        │ SUSPENDED │◀────────────────────────┘
     │        └────┬──────┘
     │             │ reinstate
     └─────────────┘
                  │
      terminate (from ACTIVE, ON_LEAVE, or SUSPENDED)
                  │
                  ▼
            ┌─────────────┐   rehire (new EmploymentStatusPeriod,
            │ TERMINATED  │──▶ same Employee.id, same employee_number)
            └─────────────┘   loops back to ACTIVE, above
```

Each transition is one new `EmploymentStatusPeriod` row (closing the
prior one's `effective_to`), never an `UPDATE` of a status column on
`Employee` itself — this is what lets "what was this employee's status
on June 15" be answered exactly, forever, matching Phase 2's explicit
requirement that current master data must never rewrite historical
payroll meaning. Only `ACTIVE` and `ON_LEAVE` employees may have new
`AttendanceRecord` rows created for them (a `SUSPENDED` or `TERMINATED`
employee clocking in is rejected, mirroring how a cancelled/inactive
resource is rejected everywhere else in this codebase); a payroll period
may still *reference* attendance/compensation from when the employee was
`ACTIVE` even after they later become `TERMINATED`, because the payroll
calculation for a given period always uses the effective-dated rows that
were in force *during that period*, never the employee's *current*
status.

## 5. Effective-dated assignment: the boundary-date rule

Every effective-dated table (`EmploymentAssignment`, `CompensationPeriod`,
`EmploymentStatusPeriod`) follows one rule, tested explicitly (Phase 18):

> For a given employee and dimension (store, or compensation, or
> status), at most one row may have `effective_to IS NULL OR
> effective_to >= X` for any date `X` also `>= effective_from` — i.e.
> **no two periods for the same employee/dimension may overlap**, and
> **coverage should be contiguous** (creating a new period automatically
> closes the previous one's `effective_to` to the day before the new
> period's `effective_from`, in the same transaction — never a separate,
> forgettable step).

Worked example (from the task, verified in test form, Phase 18):
Employee works Store A through June 30; Store B from July 1.
`EmploymentAssignment` rows:
`(store=A, effective_from=2024-01-01, effective_to=2024-06-30)`,
`(store=B, effective_from=2024-07-01, effective_to=NULL)`.
A June payroll period (`period_start=2024-06-01, period_end=2024-06-30`)
resolves the employee's store via
`WHERE effective_from <= period_end AND (effective_to IS NULL OR
effective_to >= period_start)` → Store A. A July period resolves to
Store B. **A period that spans a boundary (e.g. a bi-weekly period
2024-06-24..2024-07-07) is the genuinely hard case**: the calculation
engine must split labor-cost attribution at the boundary date, producing
two `PayrollEarningLine`-level store attributions inside one
`PayrollEmployeeResult`, never silently picking "whichever store was
current when the run started." This is stated as a hard requirement here
and enumerated in the test matrix (Phase 18) — it is the single most
likely place a naive implementation would quietly get the multi-store
labor-cost dimension wrong.

Database enforcement: a Postgres **exclusion constraint** (`EXCLUDE
USING gist (employee_id WITH =, daterange(effective_from, effective_to,
'[]') WITH &&)`) on each effective-dated table is the DB-level
non-overlap guarantee (Phase 16) — the same category of "invariant
belongs in the database" the M9 hardening pass applied to duplicate-plan
prevention. This requires the `btree_gist` Postgres extension (not
currently enabled in this project — a migration-strategy item, Phase 13
below).

## 6. Compensation model

`CompensationPeriod` (per Phase 4):
- `pay_type`: `HOURLY` or `SALARY`.
- `rate`: `Numeric(12,4)` — one more digit of precision than the
  project's usual 2dp money, matching how `SupplierProduct.unit_cost`
  already uses `Numeric(14,6)` for a per-unit rate that will be
  multiplied by a quantity and needs to round only at the final
  amount — an hourly rate multiplied by fractional hours has the same
  requirement.
- `pay_frequency`: `WEEKLY | BIWEEKLY | SEMIMONTHLY | MONTHLY` —
  informational on the employee's own row; a given `PayrollPeriod`'s own
  `period_start`/`period_end` are what actually drive calculation, so an
  employee's frequency and the period grid can be validated against each
  other but the period is always the authority.
- `overtime_eligible`: bool. Overtime *rules* (threshold hours, multiplier)
  are NOT hardcoded into the calculation engine (per Phase 7's
  instruction not to bake in payroll assumptions) — they are their own
  small effective-dated configuration table
  (`OvertimePolicy(effective_from, effective_to, threshold_hours_per_period,
  multiplier)`), scoped globally or per-store as the business needs
  (default: one global policy row, extensible to per-store rows later
  without a schema change, mirroring how `TaxRate` already works in M1).
- Every compensation change is a new `CompensationPeriod` row (never an
  `UPDATE` of `rate`), closing the prior period exactly like
  `EmploymentAssignment`. The worked example from the task
  ($10/hour through June 30, $12/hour from July 1) is mechanically
  identical to the store-boundary example above and uses the same
  boundary-date resolution and the same split-attribution rule for a
  period that spans the change.
- `PayrollEarningLine.rate_used` and `PayrollDeductionLine.deduction_rate_id`
  are **snapshots** — the calculation engine resolves the effective
  `CompensationPeriod`/`DeductionRate` for the period being calculated
  and freezes the *value actually used* onto the derived row, exactly
  like `ReplenishmentPlan.executed_unit_cost` freezes a supplier price at
  execution time. A later change to `CompensationPeriod.rate` (which
  cannot happen for a past period anyway, since it's insert-only) could
  never retroactively alter a posted payslip even if someone tried.

## 7. Time and attendance

`AttendanceRecord` fields (per Phase 5): `employee_id`, `store_id`,
`work_date`, `clock_in_at` (timestamptz, UTC — project convention),
`clock_out_at` (nullable while open), `source` (`CLOCK` vs `MANUAL`),
`status` (`OPEN` while clocked in, `CLOSED` once clocked out, `VOIDED`
for a corrected-away duplicate/error), `correction_of_id` (self-FK,
nullable — a correction is a NEW row referencing the record it corrects,
never an in-place edit of the original clock time, matching
`StockCount`'s recount pattern from M8: the original is preserved,
voided, and superseded, so "what did the clock actually say" and "what
was it corrected to" are both permanently answerable), `correction_reason`
(required when `correction_of_id IS NOT NULL`), `corrected_by` (FK→users).

Edge cases, each with an explicit rule (tested in Phase 18, not left to
"hours = clock_out - clock_in" naivety):

| Case | Rule |
|---|---|
| Missing clock-out | Record stays `OPEN` indefinitely; it contributes ZERO hours to any payroll calculation until closed (never assumed-closed at end of day or period) — a payroll calculation that finds an `OPEN` record inside its period range is a hard validation error (`OPEN_ATTENDANCE_RECORD`), not a silent zero, forcing a manager correction before that employee's payroll can be calculated for that period |
| Duplicate clock-in | A second `clock_in_at` for the same employee while an `OPEN` record already exists is rejected at the service layer (`ConflictError`, `ALREADY_CLOCKED_IN`) — never silently creates two open records |
| Overlapping shifts | Same rule as duplicate clock-in generalizes: no two `OPEN`-or-`CLOSED` records for the same employee may have overlapping `[clock_in_at, clock_out_at)` ranges — enforced by the same exclusion-constraint technique as Phase 5's effective-dating (Phase 16) |
| Cross-midnight shifts | `work_date` is the shift's LOGICAL business date (typically the clock-in date, configurable per store via the existing `store_settings` key-value table, never inferred from the calendar date of `clock_out_at`) — a shift starting 22:00 and ending 06:00 belongs entirely to one `work_date`, and duration is computed from the real UTC timestamps, never from a naive same-calendar-day subtraction |
| Manager correction | A `MANUAL` record with `correction_of_id` set, requiring `attendance.write` (clerk-level, self-service can be a narrower future permission) and `correction_reason`; the original is marked `VOIDED`, never deleted |
| Employee self-correction | Not built in M10 (see "explicitly not built," Phase 19) — all corrections in M10 are manager/clerk-entered, mirroring how M9 gave inventory clerks write access but reserved review/approval for managers |
| Terminated/inactive employee clocking in | Rejected at the service layer by checking the CURRENT `EmploymentStatusPeriod` (must be `ACTIVE` or `ON_LEAVE`) — `INVALID_EMPLOYEE_STATE` |
| Store mismatch | `store_id` on the attendance record must match the employee's CURRENT `EmploymentAssignment` store (or be an explicitly-flagged cross-store shift — see Phase 10) |

## 8. Payroll period lifecycle

```
DRAFT ──(open)──▶ OPEN ──(calculate)──▶ CALCULATED ──(approve)──▶ APPROVED ──(post)──▶ POSTED
  │                 │                       │                                            │
  │                 │                       │ (recalculate — DRAFT-equivalent            │
  │                 │                       │  derived rows are replaced wholesale,      │
  │                 │◀──────────────────────┘  never patched)                            │
  │                                                                                        │
  └── cancel (DRAFT/OPEN/CALCULATED/APPROVED only — never POSTED)                          │
                                                                                             │
                                                                              reverse (new PayrollReversal
                                                                               + compensating journal —
                                                                               POSTED itself is NEVER
                                                                               un-posted or edited)
```

Rationale for six explicit states rather than fewer:
- `DRAFT`: the period exists (dates chosen) but attendance for it may
  still be actively corrected without any calculation having run yet —
  mirrors `PurchaseOrder`'s own `DRAFT`.
- `OPEN`: attendance is considered substantively complete and the period
  is "locked" for NEW attendance entry in the normal flow (a correction
  is still possible but now audited more heavily — see below) — this is
  the M8 `StockCount`-style checkpoint (`OPEN` there similarly freezes
  the count-input window).
- `CALCULATED`: the engine has run and produced `PayrollEmployeeResult`
  rows; nothing has been committed to the GL yet. This state is
  **required**, not optional, because Phase 19 explicitly forbids
  "calculation accidentally implying posting" — a human must review
  `CALCULATED` output before `APPROVED`.
- `APPROVED`: a manager has signed off on the calculated numbers.
  Separated from `CALCULATED` so `payroll.approve` can be a distinct,
  narrower permission than `payroll.calculate` (Phase 11) — the same
  separation-of-duties precedent as AP's post/pay split and M8's
  count review/post split.
- `POSTED`: the journal entry has been created; `PayrollEmployeeResult`
  rows flip to `status = FINAL` and become immutable (Phase 16 — a DB
  trigger or revoked `UPDATE` grant on `FINAL` rows, mirroring
  `journal_lines`).
- A seventh state, `PAID`, is **optional and deferred** (see "explicitly
  not built," Phase 19): this milestone's payroll payable liability
  (Phase 8) is satisfied by the SAME mechanism AP already has for
  supplier payments once cash actually moves — but building the actual
  "pay employees" disbursement workflow (bank file export, payment
  method per employee) is a distinct integration boundary from
  "calculate and post payroll," and is not required for M10 to be a real
  HR/payroll foundation. `POSTED` is where M10's own responsibility ends;
  a future milestone can add `PAID` without changing anything about
  `POSTED`'s meaning.

Allowed transitions and who may perform them (detailed matrix in
Phase 11's RBAC section): `open`/`calculate`/`recalculate` require
`payroll.calculate`; `approve` requires `payroll.approve`; `post`
requires `payroll.post`; `cancel` (pre-POSTED only) requires
`payroll.calculate` or higher; `reverse` (POSTED only) requires
`payroll.reverse`, the narrowest and most tightly-held permission in
this domain, mirroring `accounting.reverse`'s own restriction. Every
transition function is **idempotent by state** (calling `post` again on
an already-`POSTED` period is a no-op returning the current state,
exactly like `post_purchase_invoice`) — this is the primary idempotency
mechanism; `calculation_client_transaction_id`/
`posting_client_transaction_id` columns are the SECONDARY, explicit-key
layer for the specific race where two concurrent callers both observe
`OPEN`/`APPROVED` before either commits (mirroring M9's two-layer
idempotency on `execute_plan`, and the same lesson from the M9 hardening
pass that a lock/state-check ALONE, without a DB uniqueness backstop, is
not sufficient — Phase 16 below adds the DB constraint deliberately).

## 9. Payroll calculation engine

Strict separation, per Phase 7's explicit instruction:

- **INPUTS** (read-only for the engine, sourced from effective-dated
  history and transactional attendance): the resolved
  `CompensationPeriod` per employee for the period's date range (split
  at boundaries per Phase 5/6), the resolved `EmploymentAssignment`
  (store/department/position, also boundary-split), summed `CLOSED`
  `AttendanceRecord` hours within `[period_start, period_end]`, the
  resolved `OvertimePolicy`, and the resolved `DeductionRate`/
  `ContributionRate` rows effective during the period.
- **CALCULATION** (pure function of the inputs above, no side effects,
  no DB writes): `regular_hours = min(total_hours, threshold)`,
  `overtime_hours = max(0, total_hours - threshold)` for hourly
  employees eligible for overtime; salaried employees get
  `base_earnings` from the compensation period's rate directly
  (pro-rated across a split boundary by calendar days in each segment —
  the explicit, documented pro-ration rule, never left ambiguous);
  `gross_pay = base_earnings + overtime_earnings`; each configured
  `DeductionRate`/`ContributionRate` is applied per its
  `calculation_method` (flat / percent-of-gross / percent-of-taxable /
  bracketed) to produce one `PayrollDeductionLine` each;
  `net_pay = gross_pay - Σ(employee deduction lines)`. This function is
  the single place all of this logic lives — one authoritative
  calculation, mirroring M9 Design Decision 1's "one authoritative
  position formula" precedent, so there is never a second, divergent
  implementation for (say) a payslip preview versus the real posted
  result.
- **RESULTS** (the only DB writes this phase performs): one
  `PayrollEmployeeResult` + its `PayrollEarningLine`/
  `PayrollDeductionLine` rows per employee, all at `status = DRAFT`
  until the period reaches `POSTED`. A `recalculate` call while still
  `CALCULATED` or `OPEN` deletes and reinserts these rows wholesale for
  a clean, unambiguous "this calculation superseded that one" story —
  never a row-by-row diff/patch that could leave stale lines behind.

**Statutory tax/deduction rules are explicitly out of scope for
hardcoding.** `DeductionType`/`DeductionRate` are generic, effective-dated
configuration (flat amount, percent, or a bracket table stored as
JSONB parameters) that an administrator populates — this design commits
to the *mechanism* (a deterministic, explainable, effective-dated rate
engine) and explicitly does NOT commit to any specific country's PAYE/
social-security/NSSF-equivalent formula, because none is named in the
project's mission documents. **Any real integration with a tax
authority's filing/remittance system is named here as a separate,
future, external integration boundary — not part of M10.**

## 10. Payroll accounting integration

Reuses the existing `JournalEntry`/`JournalLine` engine exactly — no
second ledger. Adds `PAYROLL_POSTING` (and, for reversal,
`PAYROLL_REVERSAL` — used only by the dedicated reversal function, never
by the generic `reverse_journal_entry`, per the `AUTOMATED_SOURCE_TYPES`
precedent above) to the `SOURCE_TYPES`/`AUTOMATED_SOURCE_TYPES` tuples in
`accounting/models.py`, and adds one new `post_payroll_journal` function
to `accounting/service.py`, called **inline** inside the payroll period's
`post` transition (the identical "posting function called before the
operational function's own final flush, never separately committed"
rule already governing every other domain).

One journal entry per `PayrollPeriod` (never one per employee — mirrors
`post_supplier_payment_journal`'s "one real financial event, one entry"
rule even though it aggregates many `SupplierPaymentAllocation` rows),
`source_type = "PAYROLL_POSTING"`, `source_id = payroll_period.id` — the
existing partial unique index on `(source_type, source_id)` for
`STANDARD` entries becomes, for free, the DB-level "this period can never
be posted twice" guarantee (Phase 16).

Lines (illustrative account codes — actual codes are a chart-of-accounts
seeding decision, following M4's `accounting/constants.py` pattern):

```
Dr  Wage & Salary Expense           Σ gross_pay across all employees
Dr  Employer Contribution Expense   Σ employer-side contribution lines
Cr  Payroll Payable                 Σ net_pay across all employees
Cr  Tax/Statutory Withholding Payable   Σ statutory deduction lines
Cr  Benefit/Other Deduction Payable     Σ voluntary deduction lines
Cr  Employer Contribution Payable       Σ employer contribution lines
                                         (mirrors the Dr above — the
                                          EXPENSE is recognized now;
                                          the LIABILITY to actually remit
                                          it is separate and is relieved
                                          later, exactly like AP)
```

Every amount above is computed **once**, from the same
`PayrollEmployeeResult`/`*Line` rows already persisted at `CALCULATED`
time, and reused for both the per-employee detail and the aggregate
journal — never independently re-summed by two different code paths
(the same "never independently compute two sides of a pair that must
match" rule `accounting/service.py`'s own module docstring states
explicitly). This is also exactly what makes the entry balance by
construction, and what makes Phase 9's "trace $X back to employees"
requirement possible: every journal line's amount is a `SUM(...)` over a
FK-joinable set of `PayrollEmployeeResult`/`*Line` rows tagged with the
SAME `payroll_period_id`.

**Reversal**: `reverse_payroll_period(db, payroll_period_id, reason,
actor)` — only legal on a `POSTED` period, creates a `PayrollReversal`
row and a `REVERSAL`-type `JournalEntry` (via the existing
`_post_journal(..., entry_type="REVERSAL", reversal_of_id=...)`
mechanism) with every debit/credit swapped, exactly like
`reverse_journal_entry`'s own mechanism — but through a **dedicated
payroll-aware function**, never the generic endpoint, because a generic
reversal would flip the GL without touching `PayrollEmployeeResult.
status` or `PayrollPeriod.status`, silently diverging the two ledgers
(the identical risk `AUTOMATED_SOURCE_TYPES` already exists to prevent
for every other domain). The reversed period's own status becomes
`POSTED` still (the posting itself is never un-done — only compensated),
with the `PayrollReversal` row as the queryable "this was reversed, here
is why and by whom" fact, mirroring how a `REVERSAL` journal entry is a
new row, never a mutation of the original.

## 11. Payroll liability reconciliation

"Why is the payroll liability balance $X" is answerable by construction,
not by a bespoke report joining unrelated tables:

```
JournalLine (account = Payroll Payable, or Tax Payable, etc.)
   ▲ (journal_entry_id)
JournalEntry (source_type = 'PAYROLL_POSTING', source_id = payroll_period_id)
   ▲ (payroll_period_id, matches source_id)
PayrollEmployeeResult (many rows, one per employee, this period)
   ▲ (payroll_employee_result_id)
PayrollEarningLine / PayrollDeductionLine (the actual dollar detail)
```

A reconciliation report walks this chain in the forward direction (sum
the leaf lines, compare to the journal lines they should equal) rather
than the "sum unrelated tables and hope" anti-pattern the task warns
against. Because the journal amounts were computed from these exact rows
(Section 10), a mismatch can only mean a code defect, never a legitimate
data-entry divergence — which is precisely what makes this reconciliation
a real correctness check rather than decoration.

## 12. Multi-store rules

Decisions, stated precisely (per Phase 10's explicit ask):

- **An employee belongs to exactly one store at a time** (via the
  current `EmploymentAssignment`), matching `Product`/`User`'s own
  single-store-at-a-time model — never a many-to-many "works at stores
  A and B simultaneously" relationship in M10. Cross-store work in a
  single pay period is represented as **multiple `EmploymentAssignment`
  periods within that period's date range** (the boundary-split
  mechanism in Section 5/6), not as a single row with two stores.
- **A single shift is never split across stores** — `AttendanceRecord.
  store_id` is one store per record; someone genuinely working two
  stores in one day clocks in/out twice, once per store, producing two
  records. This keeps the "which store gets the labor expense" question
  always answerable per-record, never requiring an allocation heuristic
  within a single attendance row.
- **Labor expense (the Dr Wage Expense journal line) is attributed by
  store** — the journal design in Section 10 is shown company-aggregated
  for simplicity, but the real implementation posts **one journal entry
  per store per payroll period** if a period spans multiple stores'
  employees (mirroring how `JournalEntry.store_id` is `NOT NULL` and
  every other domain's journal is store-scoped, never a single
  cross-store blob) — `PayrollPeriod` itself may be created and
  calculated **company-wide** (a Manager runs "calculate payroll for
  everyone this period"), but posting fans out into N store-scoped
  journal entries, one per distinct `store_id` appearing across that
  period's `PayrollEmployeeResult` rows, each satisfying the
  `(source_type, source_id)` uniqueness on its own
  `(PAYROLL_POSTING, payroll_period_id)` pair — **this requires
  widening the existing unique index to `(source_type, source_id,
  store_id)`** for this one source type, or minting one `PayrollPeriod`
  row per store instead of one company-wide row. This exact
  either/or is flagged as Risk #1 in Section 19 — it is a real design
  fork that should be resolved before implementation, not decided
  silently here.
- **Managers see employees**: a store-scoped `User`
  (`CurrentUser.store_id is not None`) sees only employees whose
  CURRENT `EmploymentAssignment.store_id` matches their own store,
  via the exact same `scoped_store_filter`/`enforce_store_access`
  helpers every other M10 endpoint reuses (never a parallel
  authorization mechanism). A cross-store Manager/Admin
  (`store_id is None`) sees all stores, exactly like every existing
  cross-store role.
- **Client-supplied store IDs are never trusted**: every mutating
  endpoint (create employee, record attendance, generate payroll)
  resolves the authoritative store server-side (from the employee's
  current assignment, or validates a supplied `store_id` against
  `enforce_store_access`), following the identical rule M9's
  hardening pass exhaustively tested for replenishment.

## 13. RBAC

Following the established separation-of-duties tiering (write / review-
or-approve / post-or-commit) from AP, M8, and M9 exactly:

| Permission | Grants | Analogous to |
|---|---|---|
| `hr.read` | View employees, departments, positions, employment/compensation history | `products.read` |
| `hr.write` | Create/edit employees, departments, positions, employment assignments, compensation changes, terminate/rehire | `products.write` |
| `attendance.read` | View attendance records | `inventory.read` |
| `attendance.write` | Clock in/out, enter manual records and corrections | `inventory.adjust` |
| `payroll.read` | View payroll periods, results, payslips, liability reconciliation | `ap.read` |
| `payroll.calculate` | Create/open a payroll period, run/rerun calculation | `ap.write` / `inventory.count.write` |
| `payroll.approve` | Approve a calculated period | `inventory.count.review` |
| `payroll.post` | Post an approved period to the GL | `ap.post` / `inventory.count.post` |
| `payroll.reverse` | Reverse a posted period | `accounting.reverse` (narrowest permission in the whole system) |

Role matrix (extending the existing table in
`app/modules/auth/permissions.py`):

| Role | Grants |
|---|---|
| Admin | everything above (matches `ADMIN: list(ALL_PERMISSIONS)`) |
| Manager | `hr.read`, `hr.write`, `attendance.read`, `attendance.write`, `payroll.read`, `payroll.calculate`, `payroll.approve`, `payroll.post` — **not** `payroll.reverse` (mirrors Manager already lacking `ACCOUNTING_REVERSE`... wait, Manager DOES currently have `ACCOUNTING_REVERSE`; for payroll specifically, reversal is deliberately held back a notch further than the existing accounting.reverse grant, because a payroll reversal touches real people's pay — this is a considered, tighter default than blind precedent-copying, flagged for explicit confirmation in Section 19) |
| Cashier | **none of the above** — a cashier must never accidentally receive payroll authority, per the task's explicit instruction |
| Inventory Clerk | **none of the above** — same reasoning; HR/payroll is a wholly separate domain from inventory |
| Auditor | `hr.read`, `attendance.read`, `payroll.read` only — full read visibility (auditors already get `AP_READ`/`ACCOUNTING_READ`), zero mutation authority anywhere in this domain |

A new role, **HR Clerk** (`hr.write`, `attendance.write`,
`attendance.read`, `hr.read` — but explicitly none of the `payroll.*`
tier), is proposed as an option worth considering rather than
overloading Manager with every HR duty — flagged as a decision point in
Section 19 rather than assumed.

## 14. Security / privacy

- Payroll amounts (compensation rates, gross/net pay, deduction detail)
  are returned by API only to callers holding `payroll.read` or higher —
  never included in a generic employee-list response used by, say, a
  scheduling view that only needs name/position/store.
- `AuditLog.before_state`/`after_state` for payroll-domain actions must
  NOT serialize raw compensation amounts into the generic audit JSON
  the way, e.g., `PRODUCT_UPDATED` freely logs price changes — payroll
  audit entries log the FACT of a change (`COMPENSATION_CHANGED`,
  employee_id, effective_from) without embedding the dollar figures in
  a JSON blob a lower-privileged `audit.read` holder could read. This is
  a deliberate **carve-out** from the generic audit pattern, stated
  explicitly here because it is the one point where "log everything"
  (M9's own audit discipline) must yield to "payroll is more sensitive
  than a product price," and needs its own reviewed convention before
  implementation (Section 19 risk).
- Application logs (not the audit table — actual stdout/structured
  logs) must never include compensation figures, SSNs/tax IDs, or bank
  details in any exception message or debug log — a lesson already
  partially covered by this codebase's existing error-handling
  conventions (`ConflictError`/`ValidationAppError` messages are
  business-safe today; payroll error messages need the same review, e.g.
  never `f"cannot pay {employee.legal_name} ${net_pay}"` in a log line).
- Minimal PII: per Phase 2's explicit instruction, `Employee` stores
  only what payroll/HR genuinely requires — legal name, employee number,
  hire/termination dates, department/position/store (via history),
  and compensation (via history). Contact information (phone/email) is
  included ONLY if a concrete M10 workflow needs it (e.g. nothing in
  this design currently requires it — flagged as "do not add without a
  justified need" in Section 18's assumptions). No SSN/national ID/bank
  account field is added in M10 — those are named explicitly as a
  future, jurisdiction-dependent extension, not invented here.
- Database runtime role (`erp_app`) grants: `payroll_employee_results`,
  `payroll_earning_lines`, `payroll_deduction_lines` rows at
  `status = FINAL` must become effectively immutable the same way
  `journal_lines`/`audit_logs` already are — either a revoked `UPDATE`
  grant conditioned via a trigger (Postgres doesn't support
  row-conditional grants directly, so this needs a `BEFORE UPDATE`
  trigger that raises when `OLD.status = 'FINAL'`, or the M4 pattern
  of never allowing `UPDATE` on the table at all and representing every
  correction as a new row — the latter is more consistent with this
  entire design's own insert-only philosophy and is the recommended
  approach, avoiding a new trigger pattern this codebase hasn't used
  before).

## 15. Idempotency

| Operation | Key | Mechanism |
|---|---|---|
| Open/calculate/recalculate a period | State (`status`) primarily; `calculation_client_transaction_id` for the true-concurrent race | Row lock + state check, mirroring `approve_plan`; DB unique column as the M9-hardening-lesson backstop |
| Approve | State (`status == 'APPROVED'` short-circuits) | Row lock + state check |
| Post | State (`status == 'POSTED'` short-circuits) + `posting_client_transaction_id` + the `(source_type, source_id[, store_id])` unique index on `journal_entries` as the ultimate DB-level backstop (the M9-hardening-proven pattern: state check + explicit key + DB constraint, three independent layers, because the M9 pass proved any ONE of the first two can be bypassed by a bug and the DB constraint is what actually holds) | |
| Reverse | State (a period can only be reversed once — enforced by a partial unique index on `payroll_reversals.payroll_period_id`, or simply checking no existing `PayrollReversal` row under the period's row lock) | Row lock + state/existence check |

Same-key-different-payload rejection: since every M10 mutating
transition (`open`/`calculate`/`approve`/`post`/`reverse`) takes **no
client-supplied financial payload at all** (identical to `approve_plan`/
`execute_plan` in M9, which take only an id and, for execute, a bare
key) — there is no "different payload" to smuggle in the first place;
the server computes every number itself, and the idempotency key's only
job is deduplicating the ACTION, not validating a payload against it.
This is a deliberate design choice (Section 19 lists the alternative —
allowing a client to submit pre-computed amounts — as explicitly
rejected) that sidesteps an entire class of "same key, different
payload" bugs by never accepting a payload substantial enough to
mismatch.

## 16. Concurrency threat model

All ten named races, reasoned about now (PostgreSQL-backed tests are a
Phase-18/implementation-time deliverable, not written yet — no code this
phase):

| # | Race | Resolution strategy |
|---|---|---|
| 1 | Two simultaneous calculations of the same period | Lock the `PayrollPeriod` row first (`SELECT ... FOR UPDATE`), re-check status after acquiring the lock (post-lock re-check, the M5/M9-established pattern) — the loser sees `CALCULATED` already and returns the existing result set rather than recalculating |
| 2 | Two approvals | Same row lock + state check; idempotent by state |
| 3 | Two postings | Row lock + state check + the `(source_type, source_id)` DB unique index as the proven-necessary third layer (per the M9 hardening pass's own discovery that the first two alone are not sufficient defense-in-depth without the DB constraint) |
| 4 | Posting while attendance is corrected | The period's `OPEN → CALCULATED` transition is what "locks" attendance for normal use; a correction submitted after `CALCULATED` is either rejected (`INVALID_PERIOD_STATE` if the period is already `APPROVED`/`POSTED`) or — if still `CALCULATED` — allowed but flagged, requiring a `recalculate` before `approve` can proceed (a `CALCULATED` period whose underlying attendance changed after calculation should not silently approve stale numbers — enforced by a `calculated_at` vs `AttendanceRecord.updated_at` staleness check at `approve` time, mirroring M9's own stale-recommendation re-validation philosophy) |
| 5 | Posting while compensation changes | Compensation changes are insert-only and effective-dated; a change with `effective_from` inside an already-`CALCULATED`/`APPROVED` period's range should be flagged the same staleness way as #4 — a change with `effective_from` AFTER the period ends never affects it at all, no race exists |
| 6 | Posting while employee status changes | Same staleness check — a termination effective inside a period already `CALCULATED` requires recalculation before approval, never silently posted against stale headcount |
| 7 | Two managers correcting the same attendance record | The attendance-correction function locks the specific `AttendanceRecord` row (`SELECT ... FOR UPDATE`) before creating the correction; the loser's correction is either rejected (if it would conflict — e.g. both trying to close the same `OPEN` record with different times) or serializes cleanly (mirrors M8's stock-count-recount concurrent-submission precedent, where the coarser lock serializes rather than corrupting) |
| 8 | Concurrent employee store assignment | The effective-dating exclusion constraint (Section 5) is the ultimate backstop; the service layer additionally locks the employee's "current assignment" via a row lock on `Employee` (or a dedicated per-employee advisory lock, mirroring M9's `pg_advisory_xact_lock` solution for a case with no natural row to lock before the new row exists) before closing the old period and inserting the new one, so two concurrent re-assignments can't both compute "today is the boundary" independently and produce overlapping periods |
| 9 | Concurrent payroll reversal | Lock the `PayrollPeriod` row; a partial unique index on `payroll_reversals.payroll_period_id` is the DB-level "reversed at most once" guarantee, exactly mirroring the M9 hardening pass's `ck_replenishment_plans_execution_link_matches_status` lesson: the row lock serializes, the DB constraint is what actually holds if it doesn't |
| 10 | Retry after timeout during posting | `posting_client_transaction_id`'s `UNIQUE` column + `IntegrityError` recovery path, byte-for-byte the same mechanism `execute_plan`/`ship_transfer`/`receive_goods` already use |

The single highest-risk race, worth calling out explicitly: **#3 combined
with #9** — posting and reversal are the two truly irreversible-in-effect
financial actions in this domain (like AP's post/pay), and the M9
hardening pass's single most important lesson (a real, demonstrated
defect: two plans could over-allocate a shared resource because
execution's revalidation read a value that a competing transaction's own
commit didn't update) generalizes directly here: **the payroll posting
transaction must revalidate against fresh, re-read state under the lock,
never trust a value computed before the lock was acquired** — the same
discipline that fixed M9's Defect 1 must be designed in from the start
for payroll posting, not discovered as a defect after the fact.

## 17. Failure injection matrix

Every multi-write payroll transaction gets injection points at the same
stages the task specifies, using the identical `monkeypatch`-on-a-real-
inner-call technique the M9 hardening pass established (never a
pre-transaction validation substitute):

| Transaction | Injection points |
|---|---|
| Calculate payroll (writes N `PayrollEmployeeResult` + their lines) | before any employee row; after employee row N/2 (mid-batch); immediately before the period's own `status` flip to `CALCULATED`; immediately before commit |
| Approve | immediately before commit (single-row update + audit log — mirrors M9's `approve_plan` failure-injection tests almost exactly) |
| Post (writes the journal entry + lines + flips period/result status) | after journal entry header created, before lines; after lines, before the period's `status` flip; immediately before commit |
| Reverse | after the compensating journal's header, before its lines; immediately before the `PayrollReversal` row is written; immediately before commit |

Each must prove: no partial `PayrollEmployeeResult`/line set; no orphan
`JournalEntry` (an entry with zero or unbalanced lines can't exist past
rollback, matching M4's own deferred-trigger guarantee); no incorrect
liability (a partially-posted journal never survives rollback to be
misread by a reconciliation report); no incorrect employee/period state
(the `status` flip and the journal creation are the same transaction —
never one without the other); safe retry (re-running the SAME operation
after a forced failure produces exactly one correct result, proven the
same way M9's Phase 2 failure-injection tests proved it: fail, assert
nothing persisted, restore, retry, assert exactly one correct result).

## 18. Database invariants

Constraints the DATABASE enforces (not application code alone), per
Phase 16's explicit instruction and the M9 hardening pass's own hard-won
lesson that a lock/state-check alone is not sufficient:

- `CHECK (rate >= 0)`, `CHECK (regular_hours >= 0 AND overtime_hours >= 0)`,
  `CHECK (gross_pay >= 0)`, `CHECK (net_pay >= 0)` — non-negative money/
  hours everywhere, matching every existing milestone's `Numeric`
  columns.
- `CHECK (effective_to IS NULL OR effective_to >= effective_from)` on
  every effective-dated table.
- `EXCLUDE USING gist (employee_id WITH =, daterange(effective_from,
  effective_to, '[]') WITH &&)` on `EmploymentAssignment`,
  `CompensationPeriod`, `EmploymentStatusPeriod` — the non-overlap
  invariant, DB-enforced (requires enabling the `btree_gist` extension —
  a migration-strategy note, Section 20).
- `EXCLUDE USING gist (employee_id WITH =, tstzrange(clock_in_at,
  clock_out_at, '[)') WITH &&)` on `AttendanceRecord` (excluding
  `VOIDED` rows via a partial exclusion constraint) — no overlapping
  shifts for one employee.
- `CHECK (status IN (...))` on every lifecycle column (`PayrollPeriod.
  status`, `EmploymentStatusPeriod.status`, `AttendanceRecord.status`),
  matching every existing milestone's identical pattern.
- `UNIQUE (employee_number)` on `Employee` — duplicate employee numbers
  are a database-level impossibility, not an application-layer check
  that could race.
- The existing `journal_entries` partial unique index on
  `(source_type, source_id)` for `entry_type = 'STANDARD'` — reused
  as-is (or widened to include `store_id`, per the multi-store posting
  design fork in Section 12/19) — is the duplicate-payroll-posting
  prevention, requiring zero new schema beyond the tuple additions.
- `UNIQUE (payroll_period_id)` on `payroll_reversals` (or a partial
  unique index if a period could theoretically be reversed more than
  once in some future design — M10's own default is "at most once").
- `UNIQUE (calculation_client_transaction_id)`,
  `UNIQUE (posting_client_transaction_id)` on `PayrollPeriod` — the
  explicit-key idempotency layer, mirroring
  `ReplenishmentPlan.execution_client_transaction_id` exactly.
- A `BEFORE UPDATE` trigger (or, preferably per Section 14's
  recommendation, simply never granting `UPDATE` on
  `payroll_employee_results`/`payroll_earning_lines`/
  `payroll_deduction_lines` to `erp_app` past a row's creation — needs a
  concrete design decision, Section 20) enforcing that a `FINAL`-status
  payroll result row can never be altered.

## 19. Reporting

Derived from the authoritative tables above, batch-fetched (no N+1 —
the M9 hardening pass's own N+1 lesson in `get_exceptions` applies
directly: any report iterating employees and querying per-employee
inside the loop is wrong):

- **Employee list**: current assignment/status/department/position
  joined once, batch-fetched (mirrors `_to_po_read`'s batch-fetch
  pattern rather than a per-row relationship traversal).
- **Employee history**: all effective-dated rows for one employee,
  ordered by `effective_from` — a direct, single-employee query, no
  batching concern.
- **Attendance summary**: aggregated hours per employee per period from
  `AttendanceRecord`, one query with `GROUP BY`.
- **Payroll-period summary**: aggregates directly from
  `PayrollEmployeeResult` for one period — one query, no per-employee
  loop.
- **Employee payslip**: one `PayrollEmployeeResult` + its lines, plus
  the snapshot columns (compensation/store/department at the time) —
  reproducible exactly as issued, forever, even if the employee's
  current master data has since changed (this is the direct payoff of
  Section 2's taxonomy).
- **Payroll register**: all employees' results for one period in one
  report — the same query as the period summary, just row-level instead
  of aggregated.
- **Payroll liability summary**: the Section 11 reconciliation chain,
  aggregated by liability account.
- **Payroll-to-GL reconciliation**: Section 11's chain, exposed as its
  own report endpoint (`payroll.read`) rather than only an internal
  invariant.

## 20. API surface proposal (routes, not code)

```
POST   /api/v1/hr/employees                       hr.write
GET    /api/v1/hr/employees                        hr.read  (store-scoped)
GET    /api/v1/hr/employees/{id}                    hr.read
PUT    /api/v1/hr/employees/{id}                    hr.write   (identity-only fields)
GET    /api/v1/hr/employees/{id}/history            hr.read
POST   /api/v1/hr/employees/{id}/assignments        hr.write   (new EmploymentAssignment)
POST   /api/v1/hr/employees/{id}/compensation       hr.write   (new CompensationPeriod)
POST   /api/v1/hr/employees/{id}/status             hr.write   (new EmploymentStatusPeriod —
                                                                  terminate/suspend/reinstate/rehire)
GET    /api/v1/hr/departments, POST .../departments  hr.read / hr.write
GET    /api/v1/hr/positions,   POST .../positions    hr.read / hr.write

POST   /api/v1/attendance/clock-in                  attendance.write
POST   /api/v1/attendance/clock-out                 attendance.write
POST   /api/v1/attendance/{id}/correct              attendance.write
GET    /api/v1/attendance                           attendance.read (store-scoped)

POST   /api/v1/payroll/periods                      payroll.calculate
GET    /api/v1/payroll/periods                      payroll.read
GET    /api/v1/payroll/periods/{id}                 payroll.read
POST   /api/v1/payroll/periods/{id}/open            payroll.calculate
POST   /api/v1/payroll/periods/{id}/calculate       payroll.calculate
POST   /api/v1/payroll/periods/{id}/approve         payroll.approve
POST   /api/v1/payroll/periods/{id}/post            payroll.post
POST   /api/v1/payroll/periods/{id}/reverse         payroll.reverse
POST   /api/v1/payroll/periods/{id}/cancel          payroll.calculate
GET    /api/v1/payroll/periods/{id}/results         payroll.read
GET    /api/v1/payroll/employees/{id}/payslips/{period_id}   payroll.read
GET    /api/v1/payroll/reports/register/{period_id}          payroll.read
GET    /api/v1/payroll/reports/liability-reconciliation      payroll.read

GET    /api/v1/hr/deduction-types, POST ...          hr.write (or a narrower
                                                       payroll-admin permission —
                                                       flagged in Section 19 risks)
```

The generated journal entry itself is reachable via the EXISTING
`/api/v1/accounting/journal-entries/{id}` endpoint (assumed to exist —
confirm during implementation) — payroll never duplicates the accounting
module's own read endpoints, exactly matching M9's explicit "never
duplicate existing PO/transfer endpoints" precedent applied to
accounting instead.

## 21. Frontend surface proposal

New sidebar sections (`navigation.ts`), each gated by its own permission
exactly like every existing entry:

- **Employees** (`hr.read`) — list/detail/history, assignment/
  compensation/status change forms.
- **Attendance** (`attendance.read`) — clock-in/out (likely a
  store-terminal-friendly view distinct from the admin list), correction
  UI, a daily/period summary.
- **Payroll** (`payroll.read`) — period list, period detail with the
  lifecycle actions (`open`/`calculate`/`approve`/`post`/`reverse` each
  gated client-side by the matching permission, exactly like
  `SupplyChainPage.tsx`'s existing pattern — never the sole
  authorization, server-side is authoritative), payroll register,
  liability reconciliation, per-employee payslip view.

## 22. Migration strategy

One new migration, `down_revision = "36ec624cf083"` (the current M9
head), following the exact linear-chain convention. Contents: new
tables (`departments`, `positions`, `employees`,
`employment_status_periods`, `employment_assignments`,
`compensation_periods`, `overtime_policies`, `deduction_types`,
`deduction_rates`, `attendance_records`, `payroll_periods`,
`payroll_employee_results`, `payroll_earning_lines`,
`payroll_deduction_lines`, `payroll_reversals`), all exclusion/check/
unique constraints from Section 18, the four new permission codes'
seeding (matching M9's own `_NEW_PERMISSIONS` + role-grant migration
pattern, with the same understood caveat from the M9 hardening audit
that `e6180fca2ee0`'s live-import seeding means a fresh from-scratch
build will show these permissions earlier than "M10 head" in a from-
scratch test — already documented as a systemic, accepted characteristic,
not something M10 needs to solve). **Requires `CREATE EXTENSION IF NOT
EXISTS btree_gist`** for the exclusion constraints — the first migration
in this project to enable a Postgres extension; confirm the runtime
database role has privilege to do so (or that it's pre-enabled in the
target environment) before committing to this approach, flagged in
Section 19. A downgrade guard, following the M7/M8/M9 precedent, refuses
to drop these tables while real `PayrollPeriod` rows in
`CALCULATED`/`APPROVED`/`POSTED` status exist.

## 23. Test matrix (categories, not full enumeration — that is an
implementation-time deliverable)

| Requirement | Unit | Integration | AuthZ | Store-isolation | Concurrency | Failure-injection | Migration | Frontend | Mutation |
|---|---|---|---|---|---|---|---|---|---|
| Employee CRUD + history | ✓ | ✓ | ✓ | ✓ | — | — | ✓ | ✓ | — |
| Effective-dating non-overlap | ✓ | ✓ | — | — | ✓ (#8) | — | ✓ (constraint) | — | ✓ |
| Compensation boundary split | ✓ | ✓ | — | ✓ | — | — | — | — | ✓ |
| Attendance edge cases (Section 7 table) | ✓ | ✓ | ✓ | ✓ | ✓ (#7) | — | ✓ (constraint) | ✓ | — |
| Payroll calculation determinism | ✓ | ✓ | — | ✓ | ✓ (#1) | ✓ | — | — | ✓ |
| Payroll approve/post/reverse lifecycle | ✓ | ✓ | ✓ | ✓ | ✓ (#2,3,9) | ✓ | — | ✓ | ✓ |
| Payroll accounting integration/balance | ✓ | ✓ | — | ✓ | ✓ (#3) | ✓ | — | — | ✓ |
| Liability reconciliation | ✓ | ✓ | ✓ | — | — | — | — | ✓ | — |
| Idempotency (all keys) | ✓ | ✓ | — | — | ✓ (#10) | ✓ | — | — | — |
| RBAC matrix (5+ roles) | — | ✓ | ✓ | ✓ | — | — | — | ✓ | — |
| Migration up/down/re-up + M0-M9 integrity | — | — | — | — | — | — | ✓ | — | — |

**What could cause the company to lose money, and how it's caught**:
double-posting the same period → the `(source_type, source_id)` unique
index + the state-check idempotency, tested exactly like M9's
`test_concurrent_execution_...` races; over/under-paying an employee due
to a boundary-date bug → the explicit split-attribution tests in
Section 5/6, run against the exact worked examples the task specifies;
a stale calculation approved after attendance/compensation changed
underneath it → the staleness-check tests for races #4-#6; a reversal
that doesn't actually reverse (leaves the liability wrong) → a balance
assertion identical to M4's own reversal tests, extended to payroll;
an N+1 in the payroll register/reconciliation reports silently
encouraging someone to avoid running them at scale → the exact
query-count regression technique the M9 hardening pass used for
`get_exceptions`.

## 24. Explicit assumptions

1. Single local currency per the existing `TECHNICAL_BLUEPRINT.md`
   statement — no multi-currency payroll in M10; a `currency` column on
   `CompensationPeriod` is included defensively (matching the store
   settings' own per-store currency key) but multi-currency CALCULATION
   (conversion) is out of scope.
2. No statutory tax/social-security formula is implemented — only the
   generic, effective-dated `DeductionType`/`DeductionRate` mechanism.
   An administrator (via `hr.write` or a narrower future permission)
   configures actual rates; none are seeded by migration beyond perhaps
   an inert example.
3. `PayrollPeriod` granularity (company-wide vs. per-store) is left as
   an open fork (Section 12/19, Risk #1) rather than assumed.
4. Attendance is captured by employee identity directly (a manager or
   the employee enters clock times through the ERP UI) — this design
   does NOT assume a physical time clock, badge reader, or biometric
   integration; those would be a future hardware-integration boundary.
5. Employees needing system access get an explicitly-linked `User`
   row created through the EXISTING user-management flow
   (`users.manage`), not a new employee-specific account-creation path —
   M10 adds the LINK (`Employee.user_id`), not a new way to create
   `User` rows.
6. No employee self-service portal in M10 (payslip viewing, leave
   requests) — every M10 UI surface is staff-facing (HR clerk, manager,
   payroll admin), per "explicitly not built" below.

## 25. Explicitly NOT built in M10

- Statutory tax/social-security integration with any real government
  authority (filing, remittance, e-submission).
- Actual payment disbursement (bank file export, direct deposit,
  check printing) — M10 ends at `POSTED` (a payable liability exists);
  paying it is future work, analogous to how AP's `ap.pay` already
  exists as a separate concern from `ap.post`.
- Employee self-service (viewing your own payslip, requesting leave,
  clocking in from a personal device).
- Leave/PTO balance tracking and accrual (only a coarse
  `ON_LEAVE` employment status, no hours-accrued ledger).
- Shift scheduling / rostering (attendance records what happened, not
  what was planned).
- Benefits administration (health insurance enrollment, etc.) beyond a
  generic "voluntary deduction" line.
- Multi-currency payroll calculation.
- A physical time-clock/biometric hardware integration.
- Workforce analytics/labor-cost-per-store dashboards beyond the basic
  reports in Section 19 (a natural M11+ extension, explicitly designed
  for by keeping `PayrollEarningLine`/`AttendanceRecord` store-attributed
  from day one).

## 26. Risks requiring a design decision before implementation

1. **`PayrollPeriod` granularity** (Section 12): one company-wide period
   fanning out into N store-scoped journal entries at posting, versus
   one `PayrollPeriod` row per store from the start. The former is more
   convenient for a single payroll run covering the whole company; the
   latter keeps `PayrollPeriod` itself store-scoped like almost every
   other transactional table in this codebase (`Sale`, `PurchaseOrder`,
   `ReplenishmentPlan`) and avoids widening the `journal_entries` unique
   index. **Recommendation for discussion: per-store `PayrollPeriod`
   rows**, grouped in the UI/API by a shared `payroll_run_id` for the
   "run everyone at once" convenience, keeping every table's store-
   scoping uniform with the rest of the system — but this is a real
   fork, not decided unilaterally here.
2. **Manager's `payroll.reverse` grant**: Section 13 proposes withholding
   it from Manager (tighter than Manager's existing `accounting.reverse`
   grant) given how much more sensitive a payroll reversal is — needs
   explicit confirmation, since it is a deliberate deviation from
   otherwise-consistent precedent.
3. **A new "HR Clerk" role** vs. folding everything into Manager —
   affects the role-seeding migration and the default staffing model;
   needs a decision, not an assumption.
4. **`btree_gist` extension enablement** — confirm the target database
   role/environment permits `CREATE EXTENSION`; if not, the non-overlap
   invariant must fall back to an application-level check plus a
   narrower DB technique (e.g. a unique constraint on
   `(employee_id, effective_from)` alone, which is weaker — catches
   exact-date collisions but not general overlap — and would need its
   own documented trade-off).
5. **Audit-log carve-out for payroll amounts** (Section 14) — this is
   the first milestone where "audit everything" (M9's own standard) is
   proposed to be deliberately narrowed for sensitivity reasons; this
   precedent-breaking choice should be confirmed, not assumed.
6. **Cross-midnight `work_date` assignment rule and the store-setting
   that governs it** — confirm whether `store_settings` (the existing
   per-store JSONB key-value table) is the right place, or whether a
   dedicated column is warranted given how load-bearing this rule is for
   payroll correctness.
7. **Overtime policy scope** (global vs. per-store, Section 6) — a
   grocery chain operating across jurisdictions with different overtime
   thresholds may need per-store policies from day one rather than as a
   later extension; worth confirming before the schema is finalized.
