# M18 Testing Sessions

Companion to `docs/M18_DISCOVERY.md` and `docs/M18_DESIGN.md`. Sessions
A–L as specified for this milestone. Several sessions are short because
Phase 1 discovery found the relevant capability already solid and
tested by prior milestones (M12/M13) — that is reported honestly, with
the existing test cited, rather than re-testing unchanged code for
appearance's sake, matching the discipline M17's own testing-sessions
doc already established.

## Session A — Baseline

Recorded before any M18 change (Phase 0): branch
`claude/grocery-erp-pos-architecture-h8a53g`, HEAD `2a0b600` (M17
final). `origin/main` at `052a250`. Working tree clean. Migration head:
single, `4a83c462dbff`. Backend `pytest -q` → 933 passed. Frontend
`npx vitest run` → 49 passed (14 files). Deploy-infra (5 CI-wired
files) → 22 passed. `deploy/tests/` total inventory: 73 tests across 12
files. Migration tests → 13 passed. `ruff check .` / `black --check .`
/ `mypy app` — all clean. `npx tsc -b` / `oxlint .` / `prettier --check
.` — all clean. Last CI run on this branch (35833592026, HEAD
`2a0b600`): `status=completed`, `conclusion=success` on all three jobs
— CI was already green on the baseline HEAD before any M18 work began.

## Session B — Backup/restore lifecycle (Phase 2)

Discovery (`M18_DISCOVERY.md` items 1–6) found this capability already
solid: `backend/tests/test_backup_restore.py` performs a real
`pg_dump`/`pg_restore` cycle with full integrity-snapshot comparison and
a post-restore functional sale, and `deploy/tests/test_backup_offbox.py`
performs a full encrypted off-box cycle (checksum verification,
corrupted-backup detection, tampered-manifest detection, retention
cleanup) — now running in CI for the first time this milestone (Phase 4,
`deploy-infra-native` job). No code defect was found in the backup/
restore path itself, so no new unit test was needed; the concrete new
work for this capability is the disaster-recovery exercise in Session K
below (a real, timed, end-to-end run against representative production-
scale data, not just the automated test's own fixture data).

## Session C — Migration safety (Phase 3)

The one genuine code defect this milestone found and fixed:
`8df037a45976` (M4 accounting core)'s downgrade dropped
`journal_entries`/`journal_lines`/`accounts` with no guard against real
posted data, unlike every later accounting migration. Fixed with the
same guard pattern already established elsewhere in the chain.
Regression: `test_m4_downgrade_refuses_when_journal_entries_exist`
(new code, new test) plus `test_m6_downgrade_refuses_when_ap_journal_entries_exist`
and `test_m14_downgrade_refuses_when_approval_data_exists` (existing
guard code, first-ever populated-data test). All three pass; the full
migration suite is 16 passed (13 + 3). Mutation-tested live (Session L).

## Session D — Deployment validation / CI coverage (Phase 4)

`deploy/tests/` CI coverage: 22/73 → 73/73. `test_deploy_procedure.py`'s
hardcoded `.venv` path fixed (`sys.executable` fallback) and wired into
the existing `deploy-infra` job (24/73 now). The remaining 49 tests
(`test_db_security.py`, `test_backup_offbox.py`, `test_monitoring.py`,
`test_adversarial_security_audit.py`,
`test_failure_injection_disaster_recovery.py`,
`test_integrated_production_session.py`) all need a native `postgres`
OS user — confirmed function-by-function, not assumed — and were **all
run locally against this sandbox's own native-Postgres/rclone/age/
Prometheus stack before being wired into the new `deploy-infra-native`
CI job**: 8 + 6 + 13 + 7 + 15 = 49 tests, all passing. Local totals this
session: `pytest -q deploy/tests/test_db_security.py deploy/tests/test_backup_offbox.py`
→ 8 passed (20.20s); `test_monitoring.py` → 6 passed (39.38s);
`test_adversarial_security_audit.py` → 13 passed (19.74s);
`test_failure_injection_disaster_recovery.py` → 7 passed (28.82s);
`test_integrated_production_session.py` → 15 passed (28.30s).

## Session E — Secrets/configuration audit (Phase 5)

`.env.prod.example` audited line by line: every value is either a real
non-secret setting or a clearly-marked `CHANGE_ME_...` placeholder — no
real credentials, no SMTP/fiscal secrets (the application has no such
features). The residual risk discovery's first pass flagged (every
fail-closed check in `backend/app/core/config.py` gated on
`ENVIRONMENT=production` being set) was resolved by direct evidence:
`docker-compose.prod.yml` hardcodes `ENVIRONMENT: production` into the
backend service's `environment:` block — not operator-settable, not
sourced from `.env.prod`. There is exactly one documented production
deployment path in this codebase (Compose; no `erp-backend.service`
exists under `deploy/systemd/`, only the monitoring components run
natively), and it cannot silently omit this variable. No code change
needed; this closed as verified, not partially verified.

## Session F — PostgreSQL production safety (Phase 6)

`deploy/postgres/PG_HBA_REQUIREMENTS.md` written per this milestone's
own instruction not to invent a distribution-specific `pg_hba.conf`.
Verified, not assumed: this sandbox's real native `pg_hba.conf` was read
directly (`peer` for local, `scram-sha-256` for host — never `trust`),
and the documented operator checklist query
(`SELECT count(*) FROM pg_hba_file_rules WHERE auth_method IN ('trust', 'password')`)
was run against it and confirmed to return `0`. `deploy/tests/test_db_security.py`'s
4 tests (network exposure, idle-transaction timeout, connection/
disconnection logging, connection-pool headroom) now run in CI for the
first time (Session D) — all 4 pass.

## Session G — Observability (Phase 7)

What an operator actually sees under each failure condition, verified
against real code and real tests (not inferred from configuration
existing):

- **Database unavailable**: `/health/db` returns 503 immediately;
  `erp_db_up` Prometheus gauge flips to 0 within one exporter poll cycle
  (5s); `ERPDatabaseDown` fires after 1 minute. Live-fire proven —
  `deploy/tests/test_monitoring.py::test_stopping_postgres_fires_erp_database_down_and_reaches_alertmanager`
  actually stops the real `postgresql` service and confirms the alert
  reaches Alertmanager end-to-end (now running in CI, Session D).
- **API process restart/crash**: `erp_api_up` gauge goes to 0;
  `ERPApiDown` fires; `ERPApiRecentlyDown` specifically flags a
  was-down-now-up transition so a silent crash-restart isn't missed by
  an operator only checking current state.
- **Disk fills**: `ERPDiskSpaceLow` fires at <10% free
  (`node_exporter`-sourced). `deploy/tests/test_failure_injection_disaster_recovery.py::test_backup_fails_cleanly_under_disk_pressure`
  (now in CI) proves the backup script itself fails cleanly and
  reports honestly under real disk pressure (a real `tmpfs` mount, not
  simulated).
- **Backup fails**: `deploy/tests/test_failure_injection_disaster_recovery.py::test_backup_script_failure_is_detected_and_reported_honestly`
  and `test_monitoring.py::test_a_failed_backup_status_file_fires_erp_backup_failed`
  (both now in CI) prove a real backup failure both reports honestly to
  the operator and fires `ERPBackupFailed` through the real Alertmanager.
- **Migration fails**: `/health/migration`'s schema-head comparison
  drives `ERPMigrationMismatch`; `deploy.sh` step 3 aborts before step 4
  ever runs (proven by `test_deploy_procedure.py`, now in CI).
- **Worker/background job dies**: no background-job mechanism exists in
  this codebase by design (confirmed in discovery, `M12_DESIGN.md`'s own
  explicit statement) — there is nothing to die, so this scenario does
  not apply. The single application process's own crash/restart is
  covered above.
- **Repeated 500s**: `erp_nginx_requests_total{status_class="5xx"}`
  drives `ERPElevatedRequestErrorRate` (>5% over 5 minutes).
- **Auth failures spike**: `erp_auth_failures_total` (polled directly
  from `audit_logs`) drives `ERPElevatedAuthFailureRate`;
  `erp_auth_reuse_detected_total` drives the critical
  `ERPRefreshTokenReuseDetected`.
- **Accounting posting fails**: rolls back completely (proven by
  `backend/tests/test_sale_finalization_failure_injection.py` and the
  AP/replenishment/transfer/returns equivalents) and surfaces as a safe
  generic error to the client, never a partial ledger state.

## Session H — Alerting review (Phase 8)

`deploy/prometheus/alertmanager.yml` and `alerts.yml` read directly, not
inferred: Alertmanager and Prometheus both bind `127.0.0.1` only (no
network exposure, confirmed via the real `ExecStart` lines in
`deploy/systemd/erp-*.service`), so no authentication is required at
that layer — an accepted, documented design choice, not an oversight.
12 alert rules across 5 groups, every one with a `severity` label and an
`action` annotation citing a runbook section (structurally verified by
`test_monitoring.py::test_every_alert_has_a_severity_and_an_actionable_annotation`).
**No external notification channel is configured** —
`alertmanager.yml`'s `webhook_configs` is a commented-out placeholder,
explicitly deferred to real-deployment configuration in the file's own
header. This is recorded as a production prerequisite in
`M18_HARDENING_AUDIT.md`, not silently left unstated.

## Session I — Multi-worker/concurrency (Phase 9)

Confirmed: the deployment is correctly single-worker today (no
`--workers`/`WEB_CONCURRENCY`/gunicorn anywhere), matching the rate
limiter's own already-documented single-process assumption. Idempotency
and business-critical mutation serialization (sales, AP, inventory) are
DB-backed (unique constraints + row locks), not in-memory — proven by a
real backend-process-restart test
(`test_duplicate_sale_retry_survives_a_backend_restart`, now in CI). No
runtime enforcement code was added (`M18_DESIGN.md` §7) — there is
currently no worker-count knob to guard against; the requirement is
recorded as documentation (this file, `M18_HARDENING_AUDIT.md`) rather
than speculative enforcement code for a scaling mode this codebase
doesn't support yet.

## Session J — Failure injection matrix (Phase 10)

All 12 required scenarios already have a real, passing test — this
session's work was mapping each to its exact test, not writing new
tests, since discovery found comprehensive prior coverage:

| # | Scenario | Test |
|---|---|---|
| 1 | DB unavailable | `test_monitoring.py::test_stopping_postgres_fires_erp_database_down_and_reaches_alertmanager`; `test_integrated_production_session.py::test_F_database_connectivity_failure` |
| 2 | DB failure mid-transaction | `backend/tests/test_sale_finalization_failure_injection.py` |
| 3 | DB failure during accounting posting | `test_sale_finalization_failure_injection.py::test_failure_during_sale_accounting_posting_leaves_no_partial_state` |
| 4 | DB failure during inventory mutation | `backend/tests/test_m8_failure_injection.py` (stock counts, transfers) |
| 5 | Migration failure | `deploy/tests/test_deploy_procedure.py::test_deploy_aborts_before_restarting_the_app_when_migration_fails`; `backend/tests/test_migrations.py` |
| 6 | Backup failure | `test_failure_injection_disaster_recovery.py::test_backup_script_failure_is_detected_and_reported_honestly` |
| 7 | Corrupted backup | `test_backup_offbox.py::test_a_corrupted_remote_backup_is_detected_and_restore_is_refused`, `::test_a_tampered_manifest_checksum_is_detected_and_restore_is_refused` |
| 8 | API process restart | `test_integrated_production_session.py::test_G_recovery_after_postgresql_restart`; `test_duplicate_sale_retry_survives_a_backend_restart` |
| 9 | Worker termination | N/A — no background-worker mechanism exists (Session G); the single app process's restart is scenario 8 |
| 10 | Reverse-proxy failure | `test_integrated_production_session.py::test_H_nginx_backend_failure_behavior`; `deploy/tests/test_connectivity_failure.py` |
| 11 | Dependency unavailable | Scenario 1 (Postgres is this application's only hard external dependency) |
| 12 | Disk/storage failure | `test_failure_injection_disaster_recovery.py::test_backup_fails_cleanly_under_disk_pressure` |

All 12 rows now run in CI (Session D's `deploy-infra-native` job plus
the pre-existing `backend` job).

## Session K — Disaster recovery exercise (Phase 11)

A real, timed, end-to-end exercise — explicitly labeled as a **local
simulation** (single host, same Postgres cluster; genuinely separate
off-box/physical-host recovery is `test_backup_offbox.py`'s own already-
tested and now CI-verified concern, not re-exercised here):

- **Source**: this development sandbox's own long-lived `erp_dev`
  database — 205 MB, real accumulated multi-module data (28,611 stores,
  9,458 users, 30,954 products, 8,257 sales, 34,482 journal entries,
  32,915 inventory movements, 11,988 purchase orders, 12,133 suppliers),
  not a synthetic seed.
- **Backup**: `backend/scripts/backup_database.sh` against `erp_dev`.
  1.53s wall-clock, produced an 11 MB `pg_dump -Fc` compressed dump.
- **Restore target**: a **freshly created separate database**
  (`erp_dr_exercise`), not `erp_dev` destroyed-in-place — a deliberate
  safety choice, not a limitation of the procedure: `erp_dev` is a
  shared, persistent resource this sandbox's other tests depend on, and
  `pg_restore`'s actual recovery mechanics are identical against a
  freshly emptied original database or a freshly created new one (the
  literal destroy-in-place cycle is exercised, on its own dedicated
  throwaway database, by `backend/tests/test_backup_restore.py`
  already). This sandbox's `erp_user` also lacks `CREATEDB` (unlike the
  `postgres:16` Docker image's admin-equivalent role, or the new
  `deploy-infra-native` CI job's own bootstrapped superuser `erp_user`)
  — the target database was created via the `postgres` OS superuser
  directly rather than through `restore_database.sh`'s own `createdb`
  line, an environment difference from a fresh production cluster, not
  a defect in the script.
- **Restore**: `pg_restore --clean --if-exists`. 3.21s wall-clock.
- **Verification** (all against the restored copy, compared to the
  original):
  - `alembic_version` = `4a83c462dbff` — matches the single head.
  - All 8 representative table row counts byte-identical to the source.
  - **The combined output of all 5 real reconciliation functions**
    (`inventory_reconciliation`, `ap_reconciliation`,
    `purchase_clearing_reconciliation`, `in_transit_reconciliation`,
    `payroll_gl_reconciliation` — the entire accounting subsystem's
    self-check surface) is **SHA-256 byte-identical** between the
    original and the restored database (hash
    `1eefcf9...9cf69e4` on both, 4,150,394 bytes of serialized result
    each). This proves the restore preserved every bit of financial
    state exactly, including whatever pre-existing discrepancies this
    long-lived dev database happens to carry from years of prior
    milestones' test runs — those discrepancies are pre-existing dev-
    data artifacts unrelated to this exercise, not something the
    restore introduced or was expected to fix.
  - A representative store-scoped query (per-store sales totals) and a
    cross-table FK integrity query (`users` → `stores`) both returned
    correct results against the restored copy.
- **Cleanup**: the throwaway database and local dump file were both
  removed after verification; `erp_dev` itself was never modified.

## Session L — Full regression and mutation testing (Phase 13, 15)

**Mutation testing** — targeted at the one operationally-relevant
control this milestone actually changed (per the explicit instruction
not to mutate unchanged code for ceremony): the new M4 migration
downgrade guard (`8df037a45976`). Replaced the guard's `IF EXISTS (...)`
condition with `IF FALSE THEN` (a plain `cp`/restore backup, not a git
command, for the revert — this milestone's own established mutation-
testing safety pattern from M16/M17). Result: the downgrade proceeded
silently, and `test_m4_downgrade_refuses_when_journal_entries_exist`
failed exactly as expected (no exception raised where one should have
been). Reverted; `diff` against the pre-mutation backup confirmed a
byte-identical restore; the full migration suite (16 tests) passed
again immediately after.

**Not re-mutated, per the same discipline**: production-secret
validation, `/health/migration`'s schema-mismatch detection, and
authorization on operational endpoints were all mutation-tested during
**M12** (`docs/M12_HARDENING_AUDIT.md` §19, 15 targeted mutations) and
rate limiting during **M13** — M18 changed none of that logic, so
re-mutating it here would test nothing new; those results are preserved
by reference. The Docker healthcheck, compose resource limits, and CI
workflow changes this milestone made are declarative configuration, not
executable logic — not mutation-testable in the traditional sense; their
correctness was instead proven by actually running what they configure
(the local dry-run of all 49 newly-CI-wired tests in Session D, and the
real GitHub Actions run this milestone's final commits trigger).

**Full regression, backend**: `pytest -q` → 936 passed (933 + 3
migration tests). `ruff check .` / `black --check .` / `mypy app` — all
clean. **Frontend**: unchanged from Session A (no frontend file touched
by M18) — 49 passed. **Deploy-infra**: 73/73 (Session D), up from 22/73.
**Migration state**: single head, still `4a83c462dbff` — M18 modified
one existing migration's `downgrade()` (not a schema change; `upgrade()`
is untouched, so the head revision itself does not change) rather than
adding a new migration.
