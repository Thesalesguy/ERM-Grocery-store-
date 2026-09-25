# M21 Discovery — Mission Traceability & Gap Analysis

**Implementation status update (post-discovery):** Findings F1 (HR
employment/compensation history isolation) and F5/F6 (AP supplier-financial
and invoice-matching-status isolation) — the CRITICAL/HIGH/MEDIUM
cross-store isolation gaps identified below — have since been remediated.
See `docs/M21_TESTING_SESSIONS.md` for the full implementation record
(service-layer fixes, mutation testing, regression results). F7 and F8
remain explicitly unresolved/out of scope, exactly as this document
originally found — this update does not alter any discovery conclusion
below; it only records that the remediation milestone happened. All other
findings, gaps, and the proposed roadmap in this document are unchanged
and still reflect the state of the repository as of its original HEAD
(`98570fd`).

**Status: Discovery only. No production code, migrations, or tests were changed to
produce this document.** This audit was conducted entirely by reading the
current repository at HEAD `98570fd` (M20's closing commit) and the full
set of prior milestone documentation (M1–M20). Findings are backed by
concrete `file:line` evidence throughout; anywhere the evidence was
insufficient to support a classification, that is stated explicitly rather
than guessed.

Methodology: seven parallel, read-only research passes (accounting;
purchasing/supply-chain/logistics; vendor management; HR/payroll;
multi-store isolation; analytics/reporting; production readiness), each
instructed to read actual code — not filenames or prior docs' claims — and
to challenge any "COMPLETE" classification until it could show real
end-to-end integration, authorization, and test coverage. Findings were
cross-checked against each other during synthesis; where two passes touched
the same code (e.g., the isolation pass and the vendor pass both examined
supplier endpoints), their conclusions agreed. Section 3 (traceability
matrix) and the remaining sections restate and organize those seven raw
reports; they are not re-derived from scratch here.

---

## 1. Executive Summary

This grocery ERP has, over 20 milestones, built a genuinely deep and
well-tested core: real double-entry accounting enforced by a Postgres
CONSTRAINT TRIGGER (not just application trust), a complete purchase-to-pay
lifecycle, a real replenishment engine that creates real purchase orders
and transfers, a substantive HR/payroll system with a correctly-separated
`Employee`/`User` model, a consistent and heavily-tested multi-store
isolation pattern applied to the overwhelming majority of the codebase, a
real analytics backend with fail-closed store-scoping, and — as of M20 — a
jurisdiction-neutral fiscal integration boundary. 1,138 automated tests
(1,010 backend + 55 frontend + 73 deploy-infra) exist, and CI verifies all
of them job-by-job on every push.

This is not, however, a finished ERP against the full mission (back-office
accounting + supply-chain logistics + vendor/supplier management + HR/payroll
+ multi-store operations + multi-store analytics + tax/fiscal integration).
Five findings are severe enough to name up front:

1. **A genuine, previously-undiscovered cross-store data leak** (CRITICAL):
   three HR endpoints return any employee's full employment/compensation
   history — including pay rate — to any store-scoped Manager, regardless
   of which store that employee belongs to. Two related AP endpoint
   families have the same defect at HIGH/MEDIUM severity. None of this was
   caught by 20 milestones of isolation-focused hardening because these
   specific endpoints were added without the surrounding file's own
   established discipline, and no test exercises them cross-store.
2. **The company's P&L structurally excludes labor cost** — the largest
   operating expense line in a grocery business — and this has been true
   since M4, restated but not flagged as a defect until this audit.
3. **Inter-store transfers have no loss/write-off/cancellation path once
   shipped** — goods lost, damaged, or shipped in error leave a permanent,
   silently-growing, unreconciled GL balance with no way to close it out.
4. **There is no accounting period-close concept** — a backdated posting
   can silently change an already-reported P&L with no lock and no alert.
5. **Multi-store analytics — the mission's own stated goal — has no
   frontend control to invoke it**, despite the backend being fully built,
   tested, and store-scope-aware. This is a UI gap sitting on top of solid
   infrastructure, not a backend gap.

None of these are signs of sloppy engineering; the surrounding code is
unusually disciplined (algebraic balance-by-construction, DB triggers as
real backstops, honest self-labeling of every other known gap this audit
re-confirmed). They are real, unaddressed risks precisely because the rest
of the system is trustworthy enough that a gap like this would not be
suspected without a dedicated audit — which is exactly what this milestone
was commissioned to do.

**Jurisdiction status, reconfirmed**: no tax jurisdiction is established
anywhere in the project source. This audit did not invent one, and every
tax/statutory-payroll item below is classified **BLOCKED BY EXTERNAL/
JURISDICTION INPUT**, not as a defect to fix.

---

## 2. Current Repository Baseline (Phase 0)

| Item | Value |
|---|---|
| Current HEAD | `98570fddc3e40b4f5493078e2977049d861c3e8d` |
| Working tree | Clean (`git status --short` empty) |
| Alembic head(s) | `9c4c5a209aa9` — single head |
| Migration count | 28 files (`backend/alembic/versions/*.py`) |
| Backend test count | 1,010 (`pytest --collect-only`) |
| Frontend test count | 55 tests across 15 test files (`vitest run`) |
| Deploy/infra test count | 73 (`pytest deploy/tests --collect-only`) |
| **Total automated tests** | **1,138** |
| CI status for final HEAD | All 4 jobs (`backend`, `frontend`, `deploy-infra`, `deploy-infra-native`) confirmed green individually on `98570fd`, run `36117267572` (M20's own final validation) |

**Existing milestone documentation (M13–M20)**: `M13_DESIGN.md`,
`M13_HARDENING_AUDIT.md`, `M13_PRODUCTION_RUNBOOKS.md`,
`M13_TESTING_SESSIONS.md`; `M14_DESIGN.md`, `M14_HARDENING_AUDIT.md`,
`M14_TESTING_SESSIONS.md`; `M15_DESIGN.md`, `M15_HARDENING_AUDIT.md`,
`M15_TESTING_SESSIONS.md`; `M16_DESIGN.md`, `M16_HARDENING_AUDIT.md`,
`M16_TESTING_SESSIONS.md`; `M17_DESIGN.md`, `M17_DISCOVERY.md`,
`M17_HARDENING_AUDIT.md`, `M17_TESTING_SESSIONS.md`; `M18_DESIGN.md`,
`M18_DISCOVERY.md`, `M18_HARDENING_AUDIT.md`, `M18_OPERATIONS_RUNBOOK.md`,
`M18_TESTING_SESSIONS.md`; `M19_DESIGN.md`, `M19_DISCOVERY.md`,
`M19_HARDENING_AUDIT.md`, `M19_TESTING_SESSIONS.md`; `M20_DESIGN.md`,
`M20_DISCOVERY.md`, `M20_HARDENING_AUDIT.md`, `M20_TESTING_SESSIONS.md`.

**Existing architecture/design documentation**: `TECHNICAL_BLUEPRINT.md`,
`M1_DATABASE_DESIGN.md`, `M2_AUTH_AND_POS.md`, `M2_HARDENING_AUDIT.md`,
`M3_PURCHASING_RECEIVING_WAC.md`, `M4_ACCOUNTING_CORE.md`,
`M4_HARDENING_AUDIT.md`, `M5_RETURNS_VOIDS_REFUNDS.md`,
`M5_HARDENING_AUDIT.md`, `M6_AP_VENDOR_ACCOUNTING.md`,
`M6_HARDENING_AUDIT.md`, `M7_ADVANCED_AP_SETTLEMENT.md`,
`M7_HARDENING_AUDIT.md`, `M8_ADVANCED_INVENTORY_DESIGN.md`,
`M8_HARDENING_AUDIT.md`, `M9_SUPPLY_CHAIN_DESIGN.md`, `M9_HARDENING_AUDIT.md`,
`M10_DESIGN.md`, `M10_HARDENING_AUDIT.md`, `M11_DESIGN.md`,
`M11_HARDENING_AUDIT.md`, `M12_DESIGN.md`, `M12_HARDENING_AUDIT.md`.

**Repository-wide TODO/FIXME/placeholder sweep**: grepped
`TODO|FIXME|XXX|placeholder|not.?implemented|deferred|out of scope|
unimplemented` across `backend/app` and `frontend/src`. Every hit found is
a *documented, deliberate* scope boundary (e.g. `accounting/constants.py`'s
non-recoverable-purchase-tax note, `transfers/service.py`'s
deferred-in-transit-loss-reversal comment, `config.py`'s bootstrap-password
placeholder marker) — none is an unexplained stub. The one genuinely dead
UI artifact found is `frontend/src/components/PlaceholderPage.tsx`, which is
defined but never imported/used anywhere (confirmed by grep) — every actual
route renders a real page. `frontend/src/pages/` has no test file for
`InventoryPage.tsx`, `StockCountsPage.tsx`, `TransfersPage.tsx`,
`DashboardPage.tsx`, or `SupplyChainPage.tsx` — a real, if narrow, UI test
gap (see Section 13).

---

## 3. Mission Traceability Matrix

Classification legend: **COMPLETE** / **PARTIAL** / **INFRASTRUCTURE ONLY**
/ **PLACEHOLDER** / **MISSING** / **BLOCKED BY JURISDICTION**.

| # | Requirement | Class | Evidence (model/service/API/frontend/tests) |
|---|---|---|---|
| 1 | Chart of accounts | COMPLETE (fixed, not user-manageable) | `accounting/constants.py:167-367` (26 seeded accounts), `accounting/models.py:154-180` (`Account`), migration `8df037a45976`. No POST /accounts endpoint — by design. |
| 2 | Journal entries, debit/credit balancing | COMPLETE | Deferred Postgres CONSTRAINT TRIGGER `trg_journal_lines_balance` (migration `8df037a45976:220-269`), verified at commit time; `test_accounting_concurrency.py::test_a_deferred_trigger_rejects_unbalanced_entry_at_commit` |
| 3 | Sales posting to GL | COMPLETE | `sales/service.py:426` → `accounting_service.post_sale_journal`; `test_accounting.py`, `test_accounting_concurrency.py` |
| 4 | Purchase posting to GL | COMPLETE | `purchasing/service.py:670,896`, `ap/service.py:794,962`; `test_ap_invoices.py`, `test_ap_reversal.py` |
| 5 | Inventory accounting (COGS/WAC posting) | COMPLETE | `inventory/service.py` WAC (`_WAC_QUANTUM`), `accounting/service.py:293-298` |
| 6 | Tax payable posting | COMPLETE (sales-side; purchase tax deliberately non-recoverable) | `accounting/service.py:290-291,355-356`; `accounting/constants.py:109-120` |
| 7 | Accounts payable (invoice/payment/credit/reversal) | COMPLETE | `ap/models.py`, `ap/service.py` (2,372 lines); `test_ap_*.py` (8 files, 3,735+ lines) |
| 8 | Cash movements / cash over-short / shift accounting | COMPLETE | `shifts/service.py:193-520`; `accounting/service.py:1057-1102`; `test_shifts*.py` |
| 9 | Trial Balance / P&L reports | COMPLETE, with a material P&L completeness gap | `accounting/service.py:1309-1438`; **`net_income` excludes payroll/labor expense — see Finding F2** |
| 10 | Balance Sheet | PARTIAL — honestly self-labeled | `reports/service.py:672-709`: no Equity account exists; function is named/documented "Summary," never a true balance sheet |
| 11 | Accounting period close | MISSING | No `AccountingPeriod`/period-lock entity anywhere; running ledger only — see Finding F4 |
| 12 | GL/operational reconciliation | COMPLETE as on-demand reports; not scheduled/alerted | `inventory_reconciliation`, `cash_payment_method_summary`, `in_transit_reconciliation`, `payroll_gl_reconciliation`, `ap_reconciliation` — all real, none automated |
| 13 | Correction/reversal mechanisms | COMPLETE for AP/payroll; MISSING for stock adjustment/receipt/return/transfers | `accounting/service.py:1108-1225` generic mechanism blocks `AUTOMATED_SOURCE_TYPES`; dedicated reversal fns exist only for AP payments/credit notes and payroll |
| 14 | Supplier → PO → receiving → AP → payment lifecycle | COMPLETE | Reconfirmed from M19; `purchasing/service.py`, `ap/service.py` |
| 15 | Replenishment / reorder points | COMPLETE | `replenishment/service.py:478-643,825-1069` — real recommend→approve→execute pipeline creating real POs/transfers |
| 16 | Inter-store transfers | COMPLETE for the lifecycle it supports; MISSING loss/cancel path | `transfers/service.py`; state machine has no path back from `SHIPPED` — see Finding F3 |
| 17 | Warehouse/bin/zone sub-locations | MISSING | Zero matches for `Location`/`Warehouse`/`Bin`/`Zone` anywhere; inventory is store-granularity only |
| 18 | Stock reservations / available-to-promise | MISSING | No reservation table; POS checks raw `current_qty_on_hand` only |
| 19 | Shipment/carrier/tracking/ETA | MISSING | Zero matches for `carrier`/`tracking_number`/`ETA` |
| 20 | Damaged goods / shortage / overage recording | PARTIAL (PO receiving, shallow) / MISSING (transfers) | `GoodsReceiptItem.condition_notes` free-text only; zero equivalent for transfers |
| 21 | Procurement planning / demand forecasting | MISSING (explicit non-goal, documented twice) | M9 design doc's own stated scope boundary |
| 22 | Purchasing analytics | COMPLETE | `reports/service.py:1283-1553`; `ReportsPage.tsx` PurchasingTab |
| 23 | Supplier master data | COMPLETE (deliberately minimal fields) | `purchasing/models.py:36-88` |
| 24 | Supplier contacts (multiple) | MISSING | Single flat `contact_name` string only |
| 25 | Supplier payment terms | COMPLETE, but frontend never exposes it | `ap/service.py:352-355` uses it for due-date calc; no UI field anywhere |
| 26 | Supplier credit limits | MISSING | Zero matches; not conventionally an AP concept anyway |
| 27 | Supplier status (active/inactive) | COMPLETE, consistently enforced | `purchasing/service.py:168-185`; blocks new PO/invoice creation, doesn't disrupt open commitments |
| 28 | Supplier performance tracking | PARTIAL | Only average lead time computed; no on-time %, no quality/rejection metric, no per-supplier price trend |
| 29 | Purchasing history per supplier | PARTIAL — fragmented, one dead endpoint | POs filterable by supplier; invoices/payments/credits have a dedicated history function; **no purchase-return list exists at all** |
| 30 | Outstanding AP per supplier / aging | COMPLETE | `ap/service.py:1931-1984,2072+,2222-2285` |
| 31 | Vendor management as a whole | PARTIAL | Real AP integration + real (thin) analytics, but supplier edit/payment-terms/transaction-history are backend-only, never reachable from the UI |
| 32 | Employee records | COMPLETE (deliberately minimal) | `hr/models.py:77-101` |
| 33 | Employment status history (effective-dated) | COMPLETE | `hr/models.py:104-142`, DB EXCLUDE constraint |
| 34 | Store assignment (effective-dated, single-store-at-a-time) | COMPLETE (documented decision) | `hr/models.py:145-176` |
| 35 | HR "role"/position vs. RBAC role | COMPLETE, correctly distinct | `Position`/`Department` vs. `Role`/`Permission` — zero FK between them |
| 36 | Attendance/time tracking | COMPLETE | `hr/models.py:253-312`, cross-midnight resolution, correction pattern |
| 37 | HR scheduling/rostering (distinct from POS till shift) | MISSING (POS till shift is COMPLETE, separately, M15) | No roster/schedule-template concept exists |
| 38 | Leave management | PARTIAL | Coarse `ON_LEAVE` status only; no accrual/balance/type/request workflow |
| 39 | Payroll periods (lifecycle) | COMPLETE | `payroll/models.py:143-217`, 6-state lifecycle, DB CHECK constraints |
| 40 | Gross pay calculation (hourly + salary) | COMPLETE | `payroll/calculation.py:136-252` |
| 41 | Deductions (generic mechanism) | COMPLETE, jurisdiction-neutral | `payroll/models.py:90-140`; admin must configure real rates |
| 42 | Statutory tax withholding | BLOCKED BY JURISDICTION | No formula exists anywhere; `ACCOUNT_STATUTORY_WITHHOLDING_PAYABLE` seeded but never posted to |
| 43 | Employer contributions (generic mechanism) | COMPLETE | `payroll/models.py:71`, own GL account pair |
| 44 | Net pay | COMPLETE, DB-enforced | `ck_payroll_employee_results_net_math` CHECK constraint |
| 45 | Payroll approval (separation of duties) | COMPLETE | approve ≠ post ≠ reverse permissions; staleness guard |
| 46 | Payroll posting to accounting | COMPLETE | `payroll/service.py:691-772` → `post_payroll_journal` |
| 47 | Payroll history | COMPLETE | `list_payroll_periods`, `PayrollPage.tsx` |
| 48 | Payslips | PARTIAL | Line-item data exists and is API-exposed; frontend shows only a coarse gross/net table, no itemized/printable payslip |
| 49 | Payroll corrections (pre-posting) / reversals (post-posting) | COMPLETE | `payroll/service.py:456-663` (recalc), `775-845` (reversal), Admin-only |
| 50 | Payroll audit trail | COMPLETE, with deliberate PII carve-out | Rates/amounts never written to `AuditLog` before/after state |
| 51 | Multi-store payroll attribution | COMPLETE, proven by mutation test | Per-store `PayrollPeriod`, store-scoped employee/attendance queries |
| 52 | RBAC/User vs. Employee — does RBAC alone satisfy HR? | COMPLETE — correctly NOT conflated | `User`/`Employee` are separate tables, one nullable FK; this is the right answer, not a gap |
| 53 | Sales/returns/inventory/shifts store isolation | COMPLETE | Reconfirmed, canonical pattern applied consistently |
| 54 | Purchasing/AP/accounting store isolation | COMPLETE, **except three endpoint families** | See Finding F1 (CRITICAL), F5, F6 |
| 55 | HR/payroll store isolation | COMPLETE, **except employment/compensation history endpoints** | See Finding F1 (CRITICAL) |
| 56 | Reports/analytics store isolation | COMPLETE — best-designed module in the audit | `resolve_authorized_store_ids` applied to all 29 endpoints; zero unscoped aggregates found |
| 57 | Transfer authorization symmetry | PARTIAL — open design question | `create_transfer` needs only one side's authorization, not both — see Finding F7 |
| 58 | Operational reports (sales, inventory, purchasing, payroll) | COMPLETE | `reports/service.py`, `ReportsPage.tsx` |
| 59 | Cashier/shift performance & variance reporting | MISSING as a report | Raw `variance_amount` exists per shift; zero aggregation/trend query anywhere |
| 60 | Store comparison / multi-store dashboard UI | INFRASTRUCTURE ONLY at the UI layer | Backend fully supports explicit multi-store queries; frontend never exposes a store-selector — see Finding F8 |
| 61 | Export capability (CSV/PDF/Excel) | MISSING | Zero matches anywhere in the codebase |
| 62 | Tax domain (rates, calc, rounding, immutability) | COMPLETE, pre-existing, unaffected by M20 | `tax/models.py`, `sales/service.py::_resolve_tax/_round_money` |
| 63 | Fiscal integration boundary | COMPLETE (jurisdiction-neutral, M20) | `fiscal/models.py`, `fiscal/service.py`; reconfirmed isolated in this audit |
| 64 | Jurisdiction-specific tax authority integration | BLOCKED BY JURISDICTION | Correctly not built; `NullFiscalProvider` is the only registered provider |
| 65 | PITR/WAL | MISSING | No `wal_level`/`archive_command` anywhere; full-dump backup only |
| 66 | Off-box backup transport | DEV-SIMULATION-ONLY | `rclone` mechanism real; only ever tested against a local-filesystem remote |
| 67 | External alerting (email/Slack/PagerDuty) | MISSING | `alertmanager.yml`'s only receiver has its webhook commented out |
| 68 | Monitoring / alert rules | COMPLETE for defined scope | 15 alert rules, all runbook-linked; only 2 live-fire-tested end-to-end |
| 69 | Secrets management | PARTIAL | Fail-closed env-var validation; zero vault/secrets-manager integration |
| 70 | TLS | PARTIAL | Real termination + tests; self-signed cert only, no real CA cert exercised |
| 71 | Zero-downtime deployment | MISSING (explicitly disclaimed in code comments) | `deploy/scripts/deploy.sh:6-9` |
| 72 | Migration safety | COMPLETE, reconfirmed | Single head; guards live-tripped in M19/M20 |
| 73 | Disaster recovery | PARTIAL — real processes, same-host only | Honestly labeled "local simulation" throughout M13/M18/this audit |
| 74 | Observability (correlation IDs, structured logs) | COMPLETE; no distributed tracing | `correlation.py`, `logging.py`; tracing explicitly out of scope |

---

## 4. Accounting Audit

Full findings from the dedicated accounting-subsystem pass. Summary
verdict: **this is a genuinely usable, well-engineered back-office
accounting subsystem, not merely transaction-posting infrastructure.**
Every operational flow posts a real, DB-balance-enforced double-entry
journal entry inline with the operational transaction. Real reporting
exists beyond raw journal dumps (Trial Balance, P&L with comparative
periods, five independent automated reconciliation reports). Corrections
for the highest-volume flows (AP, payroll) are proper, audited, idempotent,
dedicated functions.

Two substantive gaps, not previously called out as deliberate scope the way
every other limitation in this codebase is:

- **No accounting-period/period-close concept** (Finding F4). A running
  ledger with no boundary against backdated postings silently altering
  already-reported figures.
- **Incomplete correction-workflow coverage**: AP and payroll got
  first-class reversal mechanisms in M16; inventory-adjustment,
  goods-receipt, purchase-return, and inter-store-transfer postings did
  not, leaving no clean GL-and-operational correction path for those event
  types (partially overlapping Finding F3).

A third finding, surfaced independently by the analytics pass rather than
the accounting pass (worth cross-referencing here since it's an accounting
defect): **the P&L's `net_income` never includes payroll/labor expense**
(`accounting/service.py:1425` sums only `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE`
into `operating_expenses`) — see Finding F2.

Chart of accounts, journal balancing (real DB CONSTRAINT TRIGGER, not app
trust), sales/purchase/payroll posting, WAC/COGS, tax-payable posting, cash
movements, cash over/short, store attribution, and audit trail are all
**COMPLETE** with concrete, tested evidence (see Section 3 rows 1–13 for
citations).

---

## 5. Purchasing / Supply-Chain / Logistics Audit

**Purchasing is complete; logistics is only partially complete — the two
must not be conflated.** The PO-to-payment financial lifecycle (M19's own
territory, reconfirmed here) is thoroughly built, store-isolated, and
heavily tested. Separately, the replenishment/reorder-point engine is
genuinely complete: a real persisted recommend→approve→execute pipeline
that creates real DRAFT POs/transfers, with a critical cross-store
over-allocation bug already found and fixed during M9's own hardening pass
(restated in Finding history, not a live defect today).

True physical logistics is thin:

- Inter-store transfers correctly move inventory atomically between two
  store-level buckets with frozen shipment cost and solid concurrency
  protection — but **once `SHIPPED`, a transfer can never be cancelled,
  reversed, or written off** (`_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)`,
  `transfers/models.py:41`). See Finding F3.
- No shipment/carrier/tracking/ETA concept exists at all.
- No warehouse/bin/zone sub-location granularity — everything is
  store-level only.
- No stock-reservation or available-to-promise calculation distinct from
  raw on-hand.
- No structured damaged-goods/shortage/overage recording for transfers; PO
  receiving has only a shallow, unrequired, unvalidated free-text
  `condition_notes` field, and PO over-receipt is silently allowed
  (flagged, not blocked, no second-approver gate) — a real WAC-integrity
  exposure at the margin (rated LOW-MEDIUM by the auditing pass).
- No procurement planning/demand forecasting — confirmed to be an explicit,
  twice-documented non-goal, not an oversight.

Purchasing analytics (spend by supplier/store/product, PO fulfillment,
delivery performance, price variance) is **COMPLETE** and wired end-to-end.
`SupplyChainPage.tsx` (423 lines, the replenishment dashboard) has **zero
test coverage under any filename** — a standing UI-test gap, unremediated
since M19.

---

## 6. Vendor-Management Audit

**More than "supplier CRUD with an AP integration," but short of a
complete vendor-management subsystem operable from a single supplier-centric
view.** The AP integration is deep (payment terms genuinely drive due-date
calculation; a first-class supplier statement/aging/summary exists;
transaction history spans invoices/payments/credit notes) and real
analytics exist underneath (delivery lead time, spend-by-supplier, price
variance, an effective-dated supplier-product pricing table feeding
automatic sourcing decisions).

But:

- The supplier record itself stays deliberately minimal: one flat
  `contact_name` string (no multi-contact concept), no credit-limit
  concept, no scorecard beyond raw average lead time.
- Several backend features are **wired but never reach the UI**: supplier
  edit (no `PUT /suppliers/{id}` frontend caller exists at all), the
  `default_payment_terms_days` field (settable only via direct API call),
  and the per-supplier transaction-history endpoint (`getSupplierTransactions`
  is defined in the frontend API client but has zero call sites).
- **Purchase returns have no list/query path at all** — by supplier or
  otherwise. `list_purchase_returns` does not exist as a function anywhere
  in `purchasing/service.py`. This is a genuine gap in a core vendor-
  management data type, not just a UI omission.
- `PurchaseReturn` → `SupplierCreditNote` linkage remains a manual,
  unenforced two-step workflow (M19 finding, unremediated).
- Supplier write operations (create/edit/deactivate) have **no store
  isolation at all** — by design, since `Supplier` is deliberately
  company-wide reference data. This is correct, not a bug, but it means
  any store-scoped user holding `purchasing.write` can deactivate a
  supplier another store still depends on, with no cross-store guard or
  warning. The read-side of this fact is tested; the write-side is not.

---

## 7. HR/Payroll Audit

**Substantively complete and genuinely integration-tested, not a scaffold.**
Of the 23 items audited in this pass, 18 are COMPLETE with real, tested,
end-to-end implementations, verified in several cases by mutation tests
that were confirmed to fail without the protection they claim to prove
(e.g., the multi-store payroll-attribution guard, the API-layer permission
enforcement).

The `User`/`Employee` architectural split is the **correct** design, not a
gap: `User` (auth/RBAC) answers "who is acting and what may they do,"
`Employee` (HR/payroll) answers "who is being paid and what is their
employment history" — two genuinely separate tables joined by a single
optional, nullable FK, exactly as M10's design doc reasoned. Had the
codebase instead extended `User` with HR columns, *that* would have been
the real defect.

Real gaps, honestly scoped:

- **Leave management** is PARTIAL: a real `ON_LEAVE` status transition
  exists (effective-dated, audited), but there is no accrual, balance,
  leave-type distinction, or request/approval workflow — this is a
  business-policy feature to build, not blocked by jurisdiction.
- **Payslips** are PARTIAL: the underlying earning/deduction line data
  exists and is API-exposed, but the frontend renders only a coarse
  gross/net table with no per-employee name resolution, no itemization,
  and no printable/downloadable document.
- **HR scheduling/rostering** is MISSING entirely (distinct from, and
  never conflated with, the POS till-shift concept from M15).
- Statutory tax withholding and any jurisdiction-specific employer
  contribution are correctly **BLOCKED BY JURISDICTION INPUT** — no
  formula was invented anywhere.
- One honestly-disclosed structural caveat: `payroll_periods`/
  `payroll_employee_results` lack DB-level UPDATE/DELETE revocation
  (unlike `journal_entries`/`payroll_reversals`, which are structurally
  append-only) — tamper-resistance for these two tables is an
  application-layer-only guarantee, verified still-effective at the real
  API layer but not bypass-proof at the database-privilege level.

**Then a genuinely severe, newly-discovered gap** (found by the separate
isolation pass, restated here because it's squarely an HR finding): three
employment/compensation-history endpoints have **zero store isolation** —
see Finding F1.

---

## 8. Multi-Store Audit

The established isolation pattern (`enforce_store_access` for writes,
`scoped_store_filter` for list reads, a per-module service-layer
`_enforce_store_access` backstop, 404-not-403 on single-resource
cross-store reads) is applied with **remarkable, verified consistency**
across sales, returns, inventory, shifts/cash movements (the M17 fix
re-confirmed live), purchasing/POs, the transactional side of AP,
accounting/GL, fiscal (M20), payroll, and — the best-designed module in
this entire audit — reports/analytics (29 endpoints, zero unscoped
aggregates found after tracing every `func.sum`/`func.count` in the
module).

Three genuine, confirmed, previously-untested gaps were found, all in
**read** endpoints added without the surrounding file's own established
discipline:

- **HR**: `get_employment_status_history`, `get_employment_assignment_history`,
  `get_compensation_history` — zero store check. **Finding F1, CRITICAL.**
- **AP**: `get_supplier_summary`, `get_supplier_transactions`,
  `get_supplier_statement` — zero store check on itemized, store-scoped
  invoice/payment/credit-note data. **Finding F5, HIGH.**
- **AP**: `get_matching_status`, `get_matching_status_multi` — zero store
  check on cross-store PO/invoice matching detail. **Finding F6, MEDIUM.**

One authorization-design question, not a data leak: `create_transfer`
requires only **one** of the two stores' authorization (either side can
draft a transfer naming the other as counterparty) — **Finding F7**, flagged
as an open business-policy question per the Stop Conditions in Section 15,
not resolved by assumption here.

No instance was found, across twelve domains, of a service function
trusting a client-supplied `store_id` for a *write* decision without
independently checking it against the caller's actual store — the three
gaps above are a different failure mode (complete absence of any check on
specific reads), not an authorization bypass via a spoofed parameter.

---

## 9. Analytics/Reporting Audit

Roughly **60–65%** of the "genuine multi-store analytics" mission goal is
met today. The backend is the strong half: real SQL aggregation (never
Python-side summing outside AP's `sum()` calls — which are exactly where
Finding F5's leak lives), correct COGS/margin/turnover definitions, exact
GL reconciliations, and thoroughly tested, fail-closed store authorization
down to the query level.

**The single biggest gap: no frontend control to invoke multi-store
comparison.** Every report call in `ReportsPage.tsx` omits `store_id`
entirely — there is no store-selector UI anywhere in the page — so a
regional manager can never select "compare stores 3, 7, and 9" through the
UI even though the backend was built, tested, and explicitly designed for
exactly that. **Finding F8.**

Secondary, concrete gaps:

- **The P&L's `net_income` structurally excludes labor cost** and is never
  reconciled against the KPI dashboard's separately-reported labor-cost
  figure. **Finding F2, HIGH.**
- Cashier/shift variance has **zero reporting layer** despite the
  underlying `variance_amount` data existing per shift.
- No category-level profitability breakdown (only product/store).
- The comparative-P&L endpoint exists and is tested at the API layer but
  has zero frontend call sites (dead API surface).
- AP aging is real but scoped to a single-supplier lookup tool
  (`AccountsPayablePage.tsx`), never surfaced as a company-wide aging
  report.
- `/dashboard`, the app's actual home route, is **still the literal M1-era
  placeholder text** ("Sales, inventory, and P&L summary widgets will
  appear here in later milestones") — the real KPI dashboard M11/M16 built
  only exists as a tab inside `/reports`, never wired to the app's actual
  Dashboard entry point.
- No export capability (CSV/PDF/Excel) exists on any report anywhere.

---

## 10. Tax/Fiscal Audit

Per the mission brief's explicit instruction: this section verifies, it
does not redesign, M20.

- **Tax domain remains separate from fiscal integration**: `TaxRate`/sale
  tax calculation (pre-M20) and `FiscalConfig`/`FiscalSubmission` (M20) are
  distinct modules with a single, narrow integration point
  (`sales/service.py`'s one call to
  `fiscal_service.create_fiscal_submission_if_enabled`), confirmed
  unchanged since M20's own closure.
- **Tax calculations remain exact and immutable historically**: `Decimal`
  arithmetic, `ROUND_HALF_UP`, frozen per-line `tax_amount` — unaffected by
  any M21-discovery-phase activity (no code was written this milestone).
- **The M20 fiscal boundary remains jurisdiction-neutral**: reconfirmed by
  the isolation pass reading `test_fiscal_isolation.py` in full and by a
  direct repository-wide grep for jurisdiction-specific strings (`TRA`,
  `EFD`, `VFD`, `ZRA`, `KRA`, `HMRC`, `IRS`, `VAT 18`, `Tanzania`, `Kenya`)
  across `backend/app` and `frontend/src` — **zero matches** outside
  documentation that explicitly quotes and disclaims the old, unconfirmed
  ambiguity note.
- **No unsupported jurisdiction has been introduced.**
- **Readiness for a future jurisdiction-specific provider**: the
  `FiscalProvider` protocol, `FiscalConfig`/`FiscalSubmission` state
  machine, and idempotency/retry infrastructure are real and tested; a real
  provider adapter is the only missing piece, and building one requires:
  (a) the actual authority's name and API/file-submission contract, (b)
  its required payload fields and numbering rules, (c) its signing/
  authentication mechanism, (d) its rejection/retry semantics, and (e)
  confirmation of VAT/sales-tax rates and category rules — **none of which
  this audit invents**.

No additional tax/fiscal work is proposed in the roadmap below beyond
"implement a real provider once a jurisdiction is confirmed," which remains
explicitly out of scope until that confirmation happens.

---

## 11. Production-Readiness Audit

Re-verified against the current HEAD (`98570fd` is a docs-only commit; the
last code change was `2300d80`, a deploy-test flakiness fix — M19/M20 were
business-domain milestones that touched no infrastructure). Every prior
"verified"/"tested" claim in M13/M18 held up against a direct re-read of
the current code; every "simulation"/"intentional limitation" label remains
accurate.

**Genuinely production-ready today, as code** (not aspirational — backed by
tests against real nginx/uvicorn/Postgres/Prometheus/Alertmanager
processes, most wired into CI): migration safety (single head, populated-
data downgrade guards, live-tripped in M19/M20); restore procedures
(scripted, tested, boots a real app against restored data); operational
runbooks (command-level, 16 numbered scenarios); the fail-closed secrets/
CORS startup validator; TLS termination mechanics; structured JSON logging
with request-ID correlation and secret redaction; the core Prometheus/
exporter/alert-rule pipeline (15 rules, all runbook-linked).

**Would need real infrastructure, not more code, to close**: PITR/WAL
archiving (a genuine build gap — no code exists at all, not just an
environment gap); a real off-box `rclone` remote pointing at physically
separate storage (the encryption/checksum/retry code is correct, it has
simply never talked to a real remote); a real Alertmanager receiver
(one YAML block once a destination exists); a CA-issued TLS certificate
against a real public domain; confirming `pg_hba.conf`'s behavioral
requirement against whatever real production Postgres is ultimately used.

**Genuine engineering decisions still owed regardless of infrastructure**:
audit-log retention/capacity planning (correctly left as an undecided
business decision, not invented); Postgres memory tuning (`shared_buffers`/
`work_mem` — currently entirely absent, connection/timeout tuning is
present and reasoned); broader live-fire coverage of the 13 alert rules
that are today only syntax-validated rather than triggered end-to-end;
multi-worker deployment is correctly not enabled (the in-process rate
limiter would silently multiply its effective budget per instance if
`--workers` were added without first migrating to a shared store — this is
documented and unenforced, a real latent trap for a future change, not a
current defect).

---

## 12. Financial-Safety Audit

Cross-cutting search performed directly (not only via the domain passes):
zero uses of `float()` near any money-sounding variable anywhere in
`backend/app/modules` (confirmed by grep); 108 `Numeric` columns confirm
Decimal discipline holds system-wide; `client_transaction_id`/idempotency-
key coverage spans sales, purchasing, AP, inventory, payroll, replenishment,
shifts, transfers, and fiscal; `with_for_update` row-locking coverage spans
the same set plus HR and auth. No float-based money handling was found.

| Finding | Severity | Scenario |
|---|---|---|
| **F1** — HR employment/status/assignment/compensation-history endpoints have zero store isolation | **CRITICAL** | Any store-scoped Manager (holds `hr.read`) can enumerate `employee_id` and read another store's employees' full compensation (pay type/rate/frequency) and employment history. `hr/service.py::compensation_history/employment_status_history/employment_assignment_history` filter only by `employee_id`, never `store_id`. Reproduction: authenticate as a store-A-scoped Manager, call `GET /hr/employees/{store-B-employee-id}/compensation-history` — succeeds today. |
| **F2** — P&L `net_income` structurally excludes payroll/labor expense | **HIGH** | `accounting/service.py:1425`'s `operating_expenses` sums only `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` — never wage/employer-contribution/purchase-price-variance/purchase-tax/cash-over-short expense accounts, all of which are real EXPENSE-type accounts in the chart of accounts. The figure shown as "net income" in both `AccountingPage.tsx` and `ReportsPage.tsx` omits the largest cost line in a grocery business, and is never cross-checked against the KPI dashboard's separately-reported labor-cost figure. This is a pre-existing M4 characteristic, not introduced this milestone, but never previously flagged as a defect. |
| **F3** — Transfer loss/shrinkage/cancellation has no path once shipped | **HIGH** | `_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)` — a `SHIPPED` transfer can never be cancelled, reversed, or written off. Goods lost/damaged/misrouted in transit leave the transfer permanently `SHIPPED` with a non-zero "Inventory In Transit" GL balance that can never be forced to zero, and this condition is not even surfaced in the replenishment module's own exceptions list. |
| **F4** — No accounting period/period-close concept | **MEDIUM-HIGH** | A backdated posting (e.g. a late payroll reversal or stock adjustment with a `posting_date` in an already-reported month) silently changes a previously-reported P&L/Trial Balance with no lock and no automated alert. Repro: report January P&L to a stakeholder; in March, post a `STOCK_ADJUSTMENT` with a February date; re-running February's P&L now differs silently. |
| **F5** — AP supplier-summary/transactions/statement endpoints have zero store isolation | **HIGH** | `get_supplier_ap_summary`/`get_supplier_transaction_history`/`get_supplier_statement` filter only by `supplier_id`. Since a supplier does business with multiple stores, any `ap.read` holder (Manager, Inventory Clerk, Auditor — all store-scopable) sees every store's itemized invoices/payments/credit-notes for that supplier. |
| **F6** — AP invoice-matching-status endpoints have zero store isolation | **MEDIUM** | `get_invoice_matching_status`/`_multi` never compare the PO's `store_id` to the caller's — reveals cross-store PO/invoice quantity and product detail. |
| **F7** — Transfer creation needs only one store's authorization, not both | **MEDIUM (open design question, not confirmed as a defect)** | A store-B user can draft a transfer naming store A as source without any store-A user's consent. Bounded impact (no inventory moves, no data leak — both parties can already see the transfer once drafted) but undocumented as intentional; flagged per Section 15's stop-condition for ambiguous business rules rather than assumed to need a fix. |
| **F8** — No frontend store-selector for any report | **MEDIUM (UX/mission-completeness, not a safety defect)** | The backend's `store_id`/`store_ids` multi-store query support is fully built and tested; zero UI control exists to invoke it. Restated here because "genuine multi-store analytics" is an explicit mission requirement this gap directly blocks. |
| Incomplete correction-workflow coverage (STOCK_ADJUSTMENT/PURCHASE_RECEIPT/PURCHASE_RETURN) | MEDIUM | No dedicated GL-and-operational reversal function exists for these source types; only a new offsetting operational transaction can correct them. |
| PO over-receipt allowed with only free-text notes, no approval gate | LOW-MEDIUM | Directly and permanently skews WAC; no required justification, no second approver. |
| Reconciliation reports are on-demand only, never scheduled/alerted | LOW | A real GL/operational divergence could go unnoticed indefinitely if nobody opens the report — ties to F4. |
| Purchase-side tax always expensed as non-recoverable | INFORMATIONAL | Documented, deliberate, conservative (over-expenses rather than under-records). |
| Closed-shift cash variance has no reversal path | INFORMATIONAL | Documented M15 scope boundary. |
| Orphaned purchase-return credits (no forced linkage to a credit note) | LOW | Restated from M19, unremediated. |
| `payroll_periods`/`payroll_employee_results` lack DB-level immutability | INFORMATIONAL | Application-layer-only guarantee, honestly disclosed, verified still-effective at the API layer. |

---

## 13. Test-Coverage Gap Analysis

Beyond raw test counts (Section 2), the following business-invariant
coverage gaps were identified:

- **No isolation test exists for any of Findings F1/F5/F6's endpoints** —
  this is precisely why they went undetected through 20 milestones of
  isolation-focused hardening. `test_store_isolation.py` is narrow (9
  tests, products/inventory/sales only); isolation coverage for other
  domains lives in scattered per-module test files, and these five
  endpoints simply never got one.
- No test exercises the "belongs to only one of the two stores" scenario
  for `create_transfer` (Finding F7) — the existing test only proves a
  caller belonging to *neither* store is rejected.
- No test exists for a transfer shortage/damaged-goods scenario, because
  the feature itself doesn't exist (Finding F3/logistics gap).
- No cross-store *write* test for supplier edit/deactivate — only the read
  side is proven; the write side shares the identical code path but is
  untested.
- No HTTP-level test for `/ap/suppliers/{id}/transactions` — only a
  service-level test exists.
- No test asserts the specific before/after audit content of supplier
  activate/deactivate (only that the HTTP call succeeds).
- Frontend: zero test files for `SupplyChainPage.tsx`, `InventoryPage.tsx`,
  `StockCountsPage.tsx`, `TransfersPage.tsx`, `DashboardPage.tsx`.
- Production readiness: only 2 of 15 Prometheus alert rules are live-fire-
  tested end-to-end (the rest are syntax/structure-validated only); no
  automatic test-restore is wired into the backup script itself (only the
  test suite proves restorability).
- No dedicated `tax_id`-field test for suppliers (shares a generic code
  path with `contact_name`, which is tested).

No test was found to be weakened, skipped, or xfailed anywhere in this
audit — all gaps above are *absences*, confirmed by grep for the relevant
endpoint/function names combined with isolation vocabulary
(`isolat|store_access|forbidden|another.?store|cross.?store|403|404`)
returning zero matches.

---

## 14. End-to-End Workflow Analysis

| Workflow | Status | First incomplete point |
|---|---|---|
| **A.** Sale → inventory → GL → cash → reporting | COMPLETE | None within the chain itself; the P&L that would summarize it structurally excludes labor cost (Finding F2, a reporting-completeness issue one hop downstream). |
| **B.** Return → inventory → accounting → cash/customer balance | COMPLETE | None identified. |
| **C.** PO → receiving → inventory → AP → payment → GL | COMPLETE | None in the money math; over-receipt has no second-approver gate (soft risk, not a break). |
| **D.** Purchase return → inventory → supplier credit → AP → GL | PARTIAL | The return→credit-note linkage step: manual, unenforced, and purchase returns have no list/query endpoint at all (per-supplier or otherwise). |
| **E.** Cashier shift → sales → cash movements → close → variance → accounting | COMPLETE forward flow | Once closed, an incorrect count has no reopen/reversal path — a correction-workflow gap, not a break in the forward chain. |
| **F.** Inter-store transfer | PARTIAL | Post-shipment: no cancellation, no shortage/loss recording — Finding F3. The forward ship→receive path itself is complete and well-tested. |
| **G.** Employee → attendance/shift → payroll → accounting | COMPLETE core chain | Payslip presentation is thin (no itemized document); leave/statutory-deduction rules are correctly blocked by jurisdiction, not a defect. |
| **H.** Fiscal submission → external provider boundary → retry/recovery → audit | COMPLETE as a jurisdiction-neutral boundary | By design, terminates at `NullFiscalProvider` until a real jurisdiction/authority is confirmed — correctly blocked, not a defect. |

---

## 15. Stop Conditions Encountered

Per the mission brief's explicit instruction, discovery must stop (for the
affected item only, not globally) on: an ambiguous business requirement
that materially changes accounting behavior; missing jurisdiction
information; unclear source-of-truth between implementations; evidence of
destructive migration/data behavior; conflicting business rules; or a
financial invariant whose intended behavior cannot be established. Three
items met this bar and were **not resolved by guessing**:

1. **Finding F2 (P&L excludes labor cost)**: is the current behavior an
   intentional scoping decision (e.g., "operating expenses" was meant to
   mean only inventory-related opex, with labor tracked separately by
   design) or a genuine, unnoticed omission? This materially changes a
   reported financial figure and must be confirmed with the system owner
   before any milestone changes it — proposed M22 (Section 16) treats
   "confirm intended P&L scope" as its first, blocking step, not an
   assumption.
2. **Finding F7 (transfer creation authorization asymmetry)**: is
   one-side-drafts-a-transfer intentional (a "pull request" style stock
   ask that the other side implicitly consents to by later shipping/
   receiving) or should drafting itself require both stores' explicit
   authorization? No design doc states either answer. Proposed M23 treats
   this as a question to put to the business owner before implementing
   either direction.
3. **Unclear source-of-truth**: `AccountingPage.tsx` and `ReportsPage.tsx`
   independently implement overlapping Trial Balance/P&L views against two
   different (though numerically consistent) code paths
   (`accounting/service.py` directly vs. `reports/service.py`'s wrapper),
   never linked to each other in the UI. Whether these should be
   consolidated into one view or are deliberately serving two different
   audiences (bookkeeper vs. operations manager) is a product decision,
   not inferred here.

No migration, jurisdiction, or conflicting-business-rule stop condition was
encountered beyond the jurisdiction status already established and
correctly respected throughout M20 and this audit.

---

## 16. Proposed Milestone Roadmap

Grounded strictly in the findings above; no milestone here duplicates
completed M13–M20 work. Each states objective, scope, dependencies,
financial risks, required testing sessions, completion gates, and explicit
non-goals. **None of these are implemented by this document.**

### M21 — Cross-Store Isolation Remediation (urgent, small, security-only)

- **Objective**: close Findings F1, F5, F6 — the three confirmed, zero-
  isolation endpoint families — and resolve or explicitly document Finding
  F7.
- **Scope**: add `caller_store_id`/store-filter checks to
  `hr/service.py::employment_status_history/employment_assignment_history/
  compensation_history`, `ap/service.py::get_supplier_ap_summary/
  get_supplier_transaction_history/get_supplier_statement`, and
  `ap/service.py::get_invoice_matching_status(_multi)`, following the exact
  pattern already established elsewhere in each of those same files. For
  F7, present the open question (Section 15, item 2) to the business owner
  and implement whichever answer is given, or explicitly document the
  current behavior as intentional if confirmed.
- **Dependencies**: none — pure remediation of existing code using an
  already-established pattern; no schema change required.
- **Financial risks**: none introduced; this milestone *closes* a real
  data-confidentiality risk (compensation/AP data leak).
- **Testing sessions required**: (1) functional correctness of the new
  filters, (2) authorization/isolation (the actual point of the milestone
  — cross-store denial tests for all five endpoints, mirroring
  `test_fiscal_isolation.py`'s pattern), (3) regression (full existing
  suite must stay green), (4) adversarial (attempt the exact reproduction
  scenarios in Findings F1/F5/F6 and confirm denial).
- **Completion gates**: all five endpoints have a passing cross-store-
  denial test; full 1,138-test regression suite green; CI green job-by-job;
  Finding F7 has either a code change with tests or an explicit, reviewed
  "intentional, documented" note.
- **Non-goals**: no new features; no schema changes; do not touch any other
  domain's isolation logic (already confirmed correct).

### M22 — Accounting Integrity Closure

- **Objective**: close Findings F2 and F4, and the correction-workflow gap
  for STOCK_ADJUSTMENT/PURCHASE_RECEIPT/PURCHASE_RETURN.
- **Scope**: (1) **first**, resolve the Section 15 stop condition on
  intended P&L scope with the business owner, then implement accordingly;
  (2) design and build an accounting-period/period-lock mechanism
  (open/close periods, refuse postings with a `posting_date` in a closed
  period, with an explicit, audited period-close action); (3) build
  dedicated, audited GL-and-operational correction functions for stock
  adjustments, goods receipts, and purchase returns, mirroring the pattern
  already proven for AP payments and payroll.
- **Dependencies**: M21 not required but recommended first (isolation fixes
  are lower-risk and faster).
- **Financial risks**: HIGH-impact by nature — this milestone changes how
  historical financial figures can and cannot be altered. Migration must
  never rewrite historical journal entries; period-close must be additive
  (a new gating mechanism), never a retroactive reinterpretation of
  existing postings.
- **Testing sessions required**: functional correctness; financial
  invariants (period-lock actually refuses backdated postings; P&L change,
  if any, is proven correct against a hand-computed example); authorization
  (who can open/close a period); multi-store isolation (period close is
  presumably per-store — confirm); concurrency (two closes racing); failure
  injection; migration safety (new period table, safe add-nullable-backfill
  pattern); adversarial; mutation testing on the new lock-enforcement
  logic; end-to-end reconciliation (period close should force/trigger the
  existing reconciliation reports).
- **Completion gates**: period-close mechanism proven to reject a real
  backdated posting attempt; new correction workflows proven idempotent and
  audited; P&L scope question resolved and documented; full regression
  green; CI green.
- **Non-goals**: no new report types beyond what's needed to support
  period-close; do not touch payroll/AP reversal mechanisms (already
  correct).

### M23 — Logistics Completeness

- **Objective**: close Finding F3 (transfer loss/cancellation) and the
  structured damaged-goods-recording gap.
- **Scope**: (1) **first**, resolve the Section 15 stop condition on
  transfer-creation authorization (Finding F7) if not already closed by
  M21; (2) add a transfer state/mechanism to record in-transit loss/
  shortage and close out the corresponding GL "in-transit" balance,
  including surfacing stuck/overdue transfers in the replenishment
  exceptions list; (3) add a structured (not free-text-only) damaged/
  short/over quantity field to PO receiving with a required reason, and
  consider a second-approver gate for large over-receipts.
- **Dependencies**: none blocking; benefits from M22's correction-workflow
  patterns as a template.
- **Financial risks**: MEDIUM-HIGH — this milestone touches WAC-affecting
  quantities (receiving) and GL in-transit balances (transfers); must not
  allow a loss write-off to be used to silently inflate/deflate valuation
  without an audit trail.
- **Testing sessions required**: functional correctness; financial
  invariants (a loss write-off correctly zeroes the in-transit GL balance
  and never fabricates inventory); authorization; multi-store isolation
  (a loss write-off likely needs both stores' visibility); concurrency
  (a loss recorded while a receipt is in flight); failure injection;
  migration safety; adversarial (attempt to write off more than was
  shipped); mutation testing.
- **Completion gates**: a shipped-and-lost transfer can be closed out with
  a correct, audited GL entry and no longer appears "stuck" forever; PO
  over-receipt requires a reason and (if built) a second approval above a
  threshold; full regression green; CI green.
- **Non-goals**: no carrier/tracking/ETA integration (no evidence this is
  required by the mission — flag as a possible future milestone only if a
  concrete need emerges); no warehouse/bin/zone sub-location modeling (same
  caveat); no stock-reservation/ATP system (same caveat).

### M24 — Multi-Store Analytics UI Completion

- **Objective**: close Finding F8 and the secondary analytics gaps
  (cashier variance reporting, category profitability, dead API wiring,
  export, the placeholder Dashboard route).
- **Scope**: (1) add a store-selector control to `ReportsPage.tsx` wired to
  the already-built backend `store_id`/`store_ids` parameters (no backend
  change needed for this part); (2) add a cashier/shift variance
  trend report (backend query + frontend view); (3) add category-level
  profitability; (4) wire the already-built comparative-P&L endpoint and
  company-wide AP aging into their respective UIs; (5) add CSV export to
  at least the most-used reports; (6) replace `/dashboard`'s M1-era
  placeholder with the real KPI dashboard (or a genuine summary view).
- **Dependencies**: none — almost entirely additive frontend work against
  already-tested backend endpoints.
- **Financial risks**: LOW — this is a reporting/UX milestone, not a
  posting-logic change; the main risk is ensuring the store-selector cannot
  be used to bypass `resolve_authorized_store_ids` (it can't, per the
  isolation audit, but the new UI must not construct requests that
  circumvent it).
- **Testing sessions required**: functional correctness; authorization
  (store-selector must not allow a store-scoped user to request unauthorized
  stores — reuse the existing backend test pattern, add a frontend test);
  multi-store isolation (explicit regression that the new UI respects
  existing backend denials); frontend/browser behavior (this milestone is
  UI-heavy — genuine browser-driven tests are warranted, not just component
  tests); end-to-end reconciliation (the new cashier-variance report must
  match the sum of underlying shift variances).
- **Completion gates**: a company-wide-authorized user can compare an
  explicit subset of stores through the UI; cashier variance is reportable
  over a date range; `/dashboard` shows real data; full regression green;
  CI green.
- **Non-goals**: no new backend aggregation logic beyond what's listed (the
  backend is already largely sufficient); no PDF export (CSV only, unless
  a concrete need is confirmed).

### M25 — Vendor-Management & HR/Payroll UX Completion

- **Objective**: close the vendor-management frontend gaps and the
  leave-management/payslip PARTIAL findings.
- **Scope**: (1) build a supplier-edit UI (including
  `default_payment_terms_days`); (2) add a `list_purchase_returns` function
  and endpoint (by supplier and globally), and surface it; (3) consider
  enforcing (or at least prompting for) `PurchaseReturn`→
  `SupplierCreditNote` linkage; (4) design and build leave accrual/balance/
  type/request-approval as a company-configurable (not jurisdiction-
  specific) feature; (5) build an itemized, per-employee payslip view
  using the already-existing `PayrollEarningLine`/`PayrollDeductionLine`
  data.
- **Dependencies**: none blocking.
- **Financial risks**: LOW for the UI/query additions; MEDIUM for leave
  accrual if it affects payroll calculation (a leave-accrual balance that's
  wrong could under/over-pay) — must be built with the same rigor as the
  existing deduction engine (effective-dated, tested, reversible).
- **Testing sessions required**: functional correctness; financial
  invariants (if leave pay is introduced, it must round-trip through the
  existing net-pay CHECK-constraint discipline); authorization; multi-store
  isolation; migration safety; frontend/browser behavior; adversarial
  (leave-balance manipulation attempts); mutation testing on any new
  calculation logic.
- **Completion gates**: supplier edit UI functional and tested; purchase
  returns listable; leave accrual (if built) reconciles correctly with
  payroll; payslip view shows real itemized data; full regression green;
  CI green.
- **Non-goals**: no supplier scorecard/RFQ workflow (no evidence of mission
  need beyond what exists); no multi-contact-per-supplier unless a concrete
  need is confirmed.

### M26 — Production Infrastructure Hardening (deployment-environment-dependent)

- **Objective**: close the production-readiness gaps that require real
  infrastructure decisions, once those decisions are made.
- **Scope**: PITR/WAL archiving; a real off-box backup remote; a real
  Alertmanager receiver; a real CA-issued TLS certificate; Postgres memory
  tuning against the real target's resources; broader live-fire alert-rule
  testing; a secrets-manager integration decision (if the deployment target
  warrants one beyond the current fail-closed env-var validator).
- **Dependencies**: **a concrete production deployment target must be
  chosen first** — this milestone cannot meaningfully proceed on decisions
  this codebase has correctly refused to invent (which cloud/host, which
  off-box storage provider, which alerting destination, which real domain
  for TLS). This is explicitly a "wait for a real decision" milestone, not
  a "guess and build" one.
- **Financial risks**: LOW directly (infrastructure, not business logic),
  but backup/recovery failures have HIGH indirect financial risk if this
  milestone is skipped before a real production launch.
- **Testing sessions required**: failure injection (against the real
  target this time, not a simulation); migration safety (unchanged);
  disaster recovery (a genuine test-restore against the real off-box
  target); adversarial (secrets exposure against the real secrets
  mechanism chosen).
- **Completion gates**: a real backup has been off-box-transported and
  restored from a genuinely separate location; an alert has genuinely paged
  someone; a real browser shows no certificate warning against the real
  domain.
- **Non-goals**: does not re-litigate any already-correct decision (audit-
  log retention remains a deferred business decision unless the business
  now has an answer; multi-worker deployment is not enabled unless the
  rate limiter is first migrated to a shared store, which is out of scope
  here).

---

## 17. Required Testing Sessions Per Milestone

Summarized from Section 16; each proposed milestone's testing-session list
already covers, at minimum: functional correctness, financial invariants,
authorization, multi-store isolation, concurrency (where applicable),
failure injection, migration safety, adversarial cases, mutation testing,
frontend/browser behavior (where a UI workflow exists), and end-to-end
reconciliation — consistent with every prior milestone's own discipline
(M10–M20) and explicitly required by this milestone's own brief. No
proposed milestone omits any of these categories without a stated reason
(e.g., M21 has no meaningful "concurrency" session because it adds read-
side filters, not new mutation logic — noted, not silently dropped).

---

## 18. Explicit Unresolved Questions

1. Is the P&L's exclusion of labor cost from `operating_expenses`
   (Finding F2) an intentional scoping decision or an unnoticed omission?
   (Section 15, item 1 — blocks M22's first step.)
2. Should inter-store transfer creation require both stores' explicit
   authorization, or is the current one-side-drafts model intentional?
   (Section 15, item 2 — blocks M23's first step.)
3. Should `AccountingPage.tsx` and `ReportsPage.tsx`'s overlapping
   Trial Balance/P&L views be consolidated, or do they deliberately serve
   different audiences? (Section 15, item 3 — informational, no milestone
   currently depends on resolving this.)
4. What is the actual target deployment infrastructure (cloud provider,
   off-box backup destination, alerting channel, TLS domain)? Blocks all
   of M26.
5. Does the business want leave accrual/balances at all, and if so, what
   policy (e.g., days-per-year, carryover rules)? This is a business-policy
   question, not a jurisdiction one — no statutory formula is implied by
   answering it, but M25's leave-management scope depends on an answer.
6. Is there a real, named audit-log retention requirement yet (M12's
   original deferred decision, restated unchanged through M18 and this
   audit)? Still open.

---

## 19. Explicit Assumptions Avoided

Per the mission's Critical Jurisdiction Rule and general anti-invention
mandate, this discovery pass deliberately did **not**:

- Assume any tax authority, VAT rate, payroll statutory formula, or
  fiscal-document numbering rule for any unconfirmed jurisdiction.
- Assume the P&L's labor-cost exclusion (Finding F2) is a bug and silently
  recommend "just fix it" — it is flagged as a stop-condition question
  instead, because changing a reported financial figure without
  confirmation would itself be the kind of undocumented behavior change
  this engagement's own discipline forbids.
- Assume the transfer-creation authorization asymmetry (Finding F7) is
  either "correct" or "a bug" — both are plausible business intents, and
  neither was asserted.
- Invent a warehouse/bin/zone location model, a shipment/carrier/tracking
  concept, or a stock-reservation/ATP system merely because a mature
  logistics platform conventionally has one — these are noted as MISSING,
  with no proposed milestone building them speculatively (M23's non-goals
  explicitly exclude them absent a confirmed need).
- Assume a specific production cloud target, backup destination, or
  alerting channel for M26 — that milestone is explicitly gated on a real
  decision, not a guess.
- Treat any prior milestone's "COMPLETE" self-assessment as sufficient
  without independent re-verification — every classification in Section 3
  traces to code read during this pass (or, for tax/fiscal, this pass's own
  direct grep), not to a prior document's claim alone.

---

## 20. Definition of Mission Closure

Based on the evidence gathered, mission closure (the point at which this
ERP genuinely satisfies its full stated scope) requires, at minimum:

1. **Security**: Findings F1/F5/F6 closed (M21) — a system with a known
   compensation-data leak cannot be considered mission-complete regardless
   of feature breadth.
2. **Accounting integrity**: a period-close mechanism exists and is
   enforced (M22), and the P&L's completeness question is resolved and
   correct (M22).
3. **Logistics**: inter-store transfers have a real closing state for the
   loss/damage/error case (M23) — a supply-chain system that can silently
   accumulate un-closeable GL balances is not production-safe for that
   domain.
4. **Analytics**: multi-store comparison is actually reachable by a real
   user through the UI (M24) — the mission explicitly names "multi-store
   analytics" as a goal, and a fully-built, fully-tested, but UI-
   unreachable backend does not satisfy that goal.
5. **Vendor/HR completeness**: the identified UX and data-model gaps in
   M25 are closed or explicitly, permanently deferred with stakeholder
   sign-off (not silently left incomplete).
6. **Tax/fiscal**: remains correctly gated on jurisdiction confirmation —
   this is not a blocker to declaring the *jurisdiction-neutral* portion of
   the mission complete, per M20's own closure logic, restated here.
7. **Production readiness**: M26's infrastructure-dependent items are
   either completed against real infrastructure or explicitly, knowingly
   deferred with the business's informed acceptance of the associated risk
   (e.g., accepting PITR/WAL's absence as a bounded RPO/RTO risk rather
   than an oversight).

Until items 1–5 above are addressed, this audit's own judgment is that the
mission is **substantially but not fully closed**: the foundation (20
milestones of disciplined, tested, honestly-documented engineering) is
sound and unusually trustworthy, but the five findings in the Executive
Summary are real, user-facing, or financially material gaps that a
genuinely complete ERP cannot carry indefinitely.

---

*This document was produced entirely through read-only repository analysis.
No production code, migration, or test file was modified to produce it.
Per the milestone brief: M21 implementation does not begin automatically
following this document's commit.*
