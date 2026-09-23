# M16 Hardening Audit

Companion to `docs/M16_DESIGN.md` and `docs/M16_TESTING_SESSIONS.md`.
Distinguishes what was actually fixed, what was investigated and found
to be intentional/correct as-is, and what remains a known, documented
gap — per the explicit instruction not to claim a requirement complete
merely because backend code exists if the user-facing capability, or the
supporting infrastructure, is still incomplete.

## 1. Phase 0 defects — fixed, with regression coverage

| # | Item | Disposition | Regression test |
|---|------|-------------|------------------|
| 1 | AP payment/credit-note correction path | **Fixed** — new `reverse_supplier_payment`/`reverse_supplier_credit_note`, mirroring payroll's reversal pattern. Also closed a related gap found while implementing this: `get_supplier_statement` did not include reversal events at all. | `test_ap_reversal.py` (14 tests) |
| 3 | Shift query indexes | **Fixed** — `ix_sales_shift_id`, `ix_sale_returns_shift_id` added in `e1a681c4aba3`. | Covered by migration upgrade/downgrade cycle in `test_migrations.py`; no separate query-plan test (see §3 below — accepted, not a gap). |
| 4 | Stock-adjustment store isolation | **Fixed** — `create_stock_adjustment` now takes `caller_store_id` and enforces it at the service layer, not just the endpoint. | `test_inventory_idempotency.py::test_cross_store_direct_service_call_cannot_mutate_another_stores_stock` |
| 5 | Shift service defense-in-depth (`get_shift`/`list_shifts`) | **Fixed** — both now take `caller_store_id` and enforce it directly, independent of the endpoint layer. | `test_shifts.py::test_direct_service_call_to_get_shift_and_list_shifts_enforces_store_isolation` |
| 6 | Cash-movement audit linkage | **Fixed** — the smallest consistent change: `record_cash_movement`'s audit event `after` payload now includes `movement_id` and `client_transaction_id`, so an audit row can be tied back to the exact movement row it describes. | `test_shifts.py::test_cash_movement_audit_event_identifies_the_exact_movement_row` |
| 8 | Operational-vs-GL return-date divergence | **Fixed** — `SaleReturn.return_date` (a genuinely new column; none existed before) is now the single date both GL posting and reporting read, mirroring `Sale.completed_at`'s existing role. `COALESCE(return_date, DATE(created_at))` preserves historical rows' reporting behavior. | `test_reports_sales.py::test_backdated_return_lands_in_return_date_period_matching_gl` |
| 9 | CI coverage for `deploy/tests/` | **Partially fixed** — see §4 below; not a full close. | 22/73 tests now run in CI. |
| 10 | Migration safety | **Satisfied continuously**, not a discrete fix — every migration in this milestone follows the established nullable-additive-column (no downgrade guard needed) vs. real-data (guard-first downgrade) pattern per column. | `test_migrations.py` upgrade/downgrade cycle. |

## 2. Investigated and found to be intentional — no code change

| # | Item | Finding |
|---|------|---------|
| 2 | `CASH_SHIFT_VARIANCE` reversal protection | The M16 discovery audit's claim that this source type was missing from `AUTOMATED_SOURCE_TYPES` (and therefore reversible via the generic journal-reversal endpoint) was **false** — direct code inspection showed it was already present and blocked. This is documented explicitly in `accounting/models.py` next to `AUTOMATED_SOURCE_TYPES` so a future reader does not have to re-derive this, and a regression test now guards the correct existing behavior rather than "fixing" something that was not broken. |
| 7 | Expected-cash / non-cash-refund investigation | The discovery audit proposed making `_compute_expected_cash` filter out refunds by `Sale.status`/payment method. Investigated and **rejected**: the till reflects actual physical cash movement, not economic reversal — a refund paid out in cash IS a real cash movement regardless of what payment method the original sale used, and the existing behavior is correct. Documented in `M16_DESIGN.md` §4 with the reasoning, and guarded by `test_shifts.py::test_expected_cash_still_reflects_original_cash_tender_when_refund_is_noncash`. |

## 3. Deliberately not built — architectural non-mirroring

`post_supplier_payment_reversal_journal`/`post_supplier_credit_note_reversal_journal`
do **not** self-commit, unlike `payroll.service.post_payroll_period`/
`reverse_payroll_period` — which the M16 discovery audit itself flagged
as an inconsistency with the rest of the codebase (AP's own dominant
convention is flush-only, caller commits). Rather than propagate that
flagged inconsistency into AP, the new reversal functions follow AP's
existing convention. This is a deliberate choice to fix a documented
inconsistency going forward rather than spread it, not an oversight.

No dedicated query-plan/`EXPLAIN` test was written for the new shift
indexes (item 3 above) — their purpose (avoiding a sequential scan on
`sales`/`sale_returns` filtered by `shift_id`, a query pattern this
milestone's own reporting/reversal code introduces) is adequately proven
by the migration cycle test plus the fact that the AP reversal
concurrency test (which queries by a similar pattern under load)
completes quickly; a separate `EXPLAIN ANALYZE` assertion was judged to
add test surface without adding confidence, given the existing coverage.

## 4. CI coverage for `deploy/tests/` — what shipped, what's deferred

**Evaluated all 73 tests in `deploy/tests/` (M13's production-topology
suite) before writing anything into CI**, per the explicit instruction
to validate before acting rather than force something in. Finding: most
of these tests assume infrastructure this sandbox has (nginx,
Prometheus, Alertmanager, node/postgres exporters, rclone, gnupg, age,
certbot, a **native** `postgres` OS user reachable via `sudo -u
postgres`) that a standard GitHub Actions `postgres:16` Docker service
container does not provide — `sudo -u postgres pg_dump` has no `postgres`
OS user to run as when Postgres is a Docker container rather than an
apt-installed native service, and `test_db_security.py` reads
`/var/log/postgresql/postgresql-16-main.log`, a path that only exists
for a native install.

**Shipped**: a new `deploy-infra` job in `.github/workflows/ci.yml` that
installs nginx (`apt-get install -y nginx`) alongside this repo's
existing Postgres-service-container setup, and runs the 22 tests across
5 files that only need `conftest.py`'s `production_stack` fixture
(real nginx + real uvicorn, no other external services):
`test_proxy_tls.py` (8), `test_rate_limiting.py` (5),
`test_connectivity_failure.py` (3), `test_observability_correlation.py`
(5), `test_performance_baseline.py` (1). **Validated locally in this
sandbox before committing**: all 22 passed
(`pytest deploy/tests/test_proxy_tls.py deploy/tests/test_rate_limiting.py
deploy/tests/test_connectivity_failure.py
deploy/tests/test_observability_correlation.py
deploy/tests/test_performance_baseline.py` → `22 passed in 85.71s`).

**Explicitly deferred, not silently dropped** — each needs a materially
different CI job than the one that could safely be added here:

- `test_monitoring.py` (M13 Phase 6) — needs a live
  Prometheus/Alertmanager/node_exporter/postgres_exporter stack on
  specific ports; standing this up safely in CI is its own project.
- `test_backup_offbox.py` and the rclone/`sudo -u postgres`-dependent
  parts of `test_adversarial_security_audit.py` and
  `test_failure_injection_disaster_recovery.py` — need a native
  Postgres install with OS-level `postgres` user access, which conflicts
  with the Docker-service-container Postgres the rest of this project's
  CI already depends on; switching the whole `deploy-infra` job to a
  native Postgres install was judged too large a change to make
  unreviewed alongside the actual M16 UI work.
- `test_deploy_procedure.py` — assumes a `backend/.venv/` virtualenv this
  repository's CI does not create (dependencies are installed globally).

**This is a genuine, documented production/CI prerequisite gap**, not a
claim that deploy-infra coverage is complete: a regression in the
monitoring/alerting stack, the off-box backup encryption/verification
path, or the native-Postgres-specific security controls would currently
only be caught by someone running `deploy/tests/` by hand, exactly as
before this milestone for those three areas.

## 5. Frontend tooling gap found and fixed during this milestone

`npx tsc --noEmit`, run repeatedly during early Reports/Users/Settings
UI development as this session's typecheck gate, was **silently
checking zero files** the entire time — `frontend/tsconfig.json` is a
project-references-only file (`"files": []`, references to
`tsconfig.app.json`/`tsconfig.node.json`), and `tsc --noEmit` without
`-b` or an explicit `-p` does not resolve those references. `ci.yml`'s
actual frontend job runs `npx tsc -b`, which does. Re-running the
correct command near the end of this milestone (before commit, not
after a CI failure) caught one real type error in `ReportsPage.tsx`: a
`.filter()` chained directly onto an array literal with an explicit
`{ id: MainTab; ... }[]` type annotation loses TypeScript's contextual
literal-type narrowing for the array literal itself (the filter call's
receiver is type-checked before the assignment context applies), so
`id: 'sales'` etc. widened to `string` and failed to satisfy `MainTab`.
Fixed by splitting the declaration (`const allTabs: {...}[] = [...]`
then `const tabs = allTabs.filter(...)`) so the array literal is
directly assigned under its annotation. Verified with `npx tsc -b`
(clean) and the full frontend suite (49/49) after the fix.

## 6. Mutation testing summary

Three live mutations, all caught by the existing test suite, all
reverted cleanly (see `M16_TESTING_SESSIONS.md` Session J for full
detail): the privilege-escalation guard, the store-settings
cross-store-write guard, and the AP-reversal idempotency-by-existence
check. In the AP-reversal case, the database's own CHECK constraint
(`ck_purchase_invoices_amount_paid_non_negative`) also caught the break
independently of the test assertion — a second line of defense, not
just the one the test happened to exercise.

## 7. Production prerequisites / remaining known gaps

- `deploy/tests/` CI coverage is partial (22/73) — see §4.
- No tax/payroll/currency/fiscal-authority/jurisdiction configuration
  exists anywhere in this milestone's Settings screen or backend, by
  design (explicit non-goal, restated in `M16_DESIGN.md` §14) — this is
  not a gap, it is a scope boundary, but is listed here so it is not
  mistaken for an oversight.
- Subset-store management / new roles / new permissions beyond
  `ap.reverse`, `store.settings.read`, `store.settings.write` were not
  introduced — no concrete backend requirement was found that needed
  them.
- No manual browser smoke test was performed for the three new UI
  screens against a live backend in this session (see
  `M16_TESTING_SESSIONS.md` Session E) — coverage is the mocked-fetch
  component test suite plus a clean production build, stated explicitly
  rather than presented as equivalent to interactive QA.
