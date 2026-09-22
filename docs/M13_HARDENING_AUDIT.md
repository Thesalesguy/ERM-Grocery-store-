# M13 Hardening Audit

Companion to `docs/M13_TESTING_SESSIONS.md` (the test-session
write-ups) and `docs/M13_DESIGN.md` (the architecture and intent).
This document is the audit: every finding, its classification, the
mutation-testing results, the 20-question final self-audit, and the
final verdict.

---

## 1. Result summary

| Gate | Result |
|---|---|
| Backend test suite | 823 passed |
| Frontend test suite | 36 passed |
| Frontend lint (oxlint) | clean |
| Frontend format (prettier) | clean |
| Frontend build (`tsc -b && vite build`) | succeeds |
| `deploy/tests/` infrastructure suite | 73 passed |
| Backend `ruff check` | clean |
| Backend `black --check` | clean |
| Backend `mypy app` | clean, 90 files |
| Migration head | `17fb9afe8d39` (unchanged from M12 — M13 added zero migrations) |
| Failure-injection scenarios | 15/15 (8 cited to existing proof, 7 new) |
| Adversarial attack vectors | 24/24 (13 cited, 11 new tests across 6 genuine + 5 partial gaps) |
| Mutation testing | 15/15 mutations proven detected |

## 2. Defects found and fixed

### 2.1 Backup/restore artifacts were world-readable (Phase 18)

`pg_dump`'s and `age`'s output inherited the invoking process's
default umask — confirmed live (`-rw-rw-r--`, mode 0664) — meaning the
full plaintext database dump (every table, unencrypted, before
`backup_offbox.sh`'s own encryption step) was readable by any OS user
on the host, not just whoever owns the backup process. Fixed with
`chmod 600` immediately after creation in
`backend/scripts/backup_database.sh`, the encrypted artifact and
manifest in `deploy/scripts/backup_offbox.sh`, and the decrypted
restore output in `deploy/scripts/restore_offbox.sh`. Regression test:
`deploy/tests/test_adversarial_security_audit.py::test_backup_and_restore_artifacts_are_not_world_or_group_readable`
runs a real backup/restore cycle and checks the actual file mode bits.

### 2.2 Monitoring had no alert for its own exporter dying (Phase 17)

Every alert in `deploy/prometheus/alerts.yml` up to this point computed
its value *from* `erp_exporter`'s own scrape — if the exporter itself
crashed, those alerts didn't fire "down," they simply stopped producing
fresh samples, and an operator watching only the alert list saw
nothing. Found while writing Phase 17's monitoring-unavailable test.
Fixed by adding `ERPMonitoringTargetDown` (`expr: up{job!="prometheus"}
== 0`) and Section 16 of `docs/M13_PRODUCTION_RUNBOOKS.md`. Regression
test: `test_monitoring_exporter_itself_going_down_fires_an_alert` kills
the real exporter process and proves the new alert fires and clears.

### 2.3 A restore-time race in already-committed Phase 17 test code (Phase 18→19)

`test_monitoring_exporter_itself_going_down_fires_an_alert`'s own
polling helper (`_target_up`, and two similar loops later in the same
test) called `resp.json()` on Prometheus's API unconditionally.
Prometheus returns a plain-text `503 Service Unavailable` (not JSON)
on every endpoint while still loading its TSDB at startup — under the
lower load of running that one file alone this window was never hit,
but once Phase 18 added a sixth parametrized case to a loopback-binding
test that also stands up a full monitoring stack, the added concurrent
load made the race reproducible at full-`deploy/tests/`-suite scale
(reproduced twice: 1 failed/71 passed both times). Fixed by checking
`resp.status_code == 200` before every `.json()` call in that function.
Confirmed by two clean full-suite runs after the fix (72 passed both
times) versus the two reproducible failures before it. This was a
latent bug in already-committed code, not something the new Phase 18
file directly caused — Phase 18 only raised the concurrency enough to
expose a pre-existing gap.

### 2.4 `backend/scripts/`, several `backend/tests/*.py` files were failing CI's real format gate undetected (Phase 20)

CI's actual gate (`.github/workflows/ci.yml`, `working-directory:
backend`) runs `black --check .`, not `ruff format --check`. Earlier
M13 phases (this session's own Phase 15 ruff-version-drift fix in
particular) reformatted several files with `ruff format` instead —
the two formatters mostly agree but diverge on some edge cases (e.g.
how an inline `# noqa` comment factors into a line-length wrap
decision) — leaving 6 `backend/tests/*.py` files silently failing the
real CI gate. Found during Phase 20's own closure re-verification of
the project's actual configured gates. Fixed by running `black .`
across `backend/`. Purely a formatting change, confirmed via a full
backend regression run (823 passed) that nothing behavioral changed.

### 2.5 A pre-existing `AdminShutdown` race, found and fixed while building Phase 16's test session

A SQLAlchemy `Session` that never calls `.commit()`/`.close()`/
`.rollback()` between operations holds its connection checked out
continuously; `pool_pre_ping=True` only validates a connection at pool
*checkout*, so a connection held across a deliberate PostgreSQL
restart (Phase 16 steps F/G) becomes a dead `AdminShutdown` on next
use regardless of `pool_pre_ping`. Fixed by explicitly `.close()`ing
the session at the end of step E, the last step to touch it before
F/G. Documented in full in `docs/M13_TESTING_SESSIONS.md` Session 1.

### 2.6 An orphaned-process leak in the shared `production_stack` fixture

Several test files kill and restart the backend mid-module (via
`pgrep`/`kill`) to simulate a real crash — the replacement process is
a fresh, untracked `subprocess.Popen` that the fixture's own tracked
handle never has a reference to, so terminating only that original
handle could leave the replacement running past the end of the test
module (observed directly: a leftover uvicorn process survived a full
pytest invocation and collided with the next module's own port-8000
bind). Fixed with a `pgrep`-based safety-net sweep in
`conftest.py`'s `production_stack` teardown, in addition to the
tracked handle's own `.terminate()`.

## 3. Tested capability — pointers, not duplicated evidence

Per-capability detail already lives in `docs/M13_TESTING_SESSIONS.md`
(sessions 1–4) and each phase's own commit message. Not repeated here.

## 4. Adversarial security audit — 24 attack vectors

| # | Vector | Status | Classification |
|---|---|---|---|
| 1 | Spoofed X-Forwarded-For | Proven — `test_proxy_tls.py::test_a_spoofed_forwarded_for_header_is_not_trusted` | N/A (defended) |
| 2 | Spoofed X-Forwarded-Proto | Proven (new) — `test_adversarial_security_audit.py::test_spoofed_forwarded_proto_header_has_no_effect` | N/A (defended) |
| 3 | Spoofed X-Request-ID | Proven — treated as an untrusted label by design | N/A (defended) |
| 4 | Direct backend access bypassing nginx | Proven — `test_proxy_tls.py::test_backend_port_is_not_reachable_except_via_loopback_binding` | N/A (defended) |
| 5 | Direct PostgreSQL access, unauthorized network | Proven (new live socket check) + config-value guard | N/A (defended) |
| 6 | Access to internal exporter ports | Proven (new) — live `ss -tlnp` check, real `monitoring_stack` | N/A (defended) |
| 7 | Access to Alertmanager internals | Proven loopback-only binding | ACCEPTED RISK — Alertmanager has no auth of its own; mitigation is network isolation, which is proven. If ever exposed beyond loopback, needs an authenticating reverse proxy first. |
| 8 | Secret leakage via env/config | Proven — `test_production_secret_management.py` | N/A (defended) |
| 9 | Secret leakage via error responses | Proven — failure-injection tests forcing a real exception | N/A (defended) |
| 10 | Secret leakage via logs | Proven (nginx/app logs) + new backup-script-stdout check | N/A (defended) |
| 11 | Audit-log overexposure | Proven — `test_hr_service.py::test_compensation_rate_never_appears_in_audit_log` | N/A (defended) |
| 12 | Cross-store audit-log access | Proven — `test_audit_log_endpoint.py` | N/A (defended) |
| 13 | Unauthorized audit-log access | Proven — `test_audit_log_endpoint.py` | N/A (defended) |
| 14 | Pagination bypass | Proven (store-scope, generic 422 cap, and new audit-log-specific 422) | N/A (defended) |
| 15 | Oversized request body | Proven — nginx + app layer | N/A (defended) |
| 16 | Unbounded query parameters | Proven (new) — ~27KB query string, 400/414/431 | N/A (defended) |
| 17 | Rate-limit bypass via forwarded headers | Proven (new) — rotated X-Forwarded-For, 429 still fires | N/A (defended) |
| 18 | Multiple-worker rate-limit weaknesses | Not applicable today | ACCEPTED RISK — nginx's `limit_req_zone` is structurally independent of backend worker count, and the current deployment runs a single backend process; re-verify if that ever changes. |
| 19 | Health endpoint leakage | Proven — `test_health_and_observability.py` | N/A (defended) |
| 20 | Migration-health leakage | Proven — `test_health_and_observability.py` | N/A (defended) |
| 21 | Runtime DB role privilege escalation | Proven (new) — real `ALTER ROLE`/`CREATE ROLE`/self-`GRANT` attempts, all denied or proven no-op | N/A (defended) |
| 22 | erp_app mutating migration metadata | Proven — `test_erp_app_can_read_but_not_write_alembic_version` | N/A (defended) |
| 23 | erp_app mutating immutable ledger tables | Proven — `test_constraints.py`, `test_accounting_concurrency.py` | N/A (defended) |
| 24 | Backup artifact access by unauthorized OS users | **Real defect found and fixed** (Section 2.1) | HIGH (fixed) — plaintext financial/PII data was world-readable before encryption |

## 5. Mutation testing

15/15 mutations proven detected by the existing suite; full detail
(target, mutation, catching test, revert confirmation) in
`docs/M13_TESTING_SESSIONS.md` Session 4 and commit `1aac47d`'s own
message. Two mutations surfaced real findings rather than simple
pass/fail: sale-finalization idempotency's fast path is provably
redundant with its DB-level enforcement (a strength, not a gap), and
`restore_offbox.sh`'s manual checksum comparison needed a new
permanent test isolating it from `age`'s own authenticated-encryption
failure path (added: `test_a_tampered_manifest_checksum_is_detected_and_restore_is_refused`).

Two planned mutations (erp_app self-escalating `alembic_version`
privileges; erp_app mutating immutable ledger tables) were not
attempted via live `GRANT`/`REVOKE` against the shared `erp_dev`
database — blocked by this session's own safety classifier as
privilege-escalation-shaped, and an Alembic-based workaround would
have the identical effect and defeat the intent of that block. Verified
instead by the already-passing static suite. Documented limitation,
not a hidden gap.

## 6. Final self-audit (20 questions)

1. **Can an attacker bypass nginx and reach the backend?** No —
   verified live via the OS's own listening-socket table (uvicorn
   binds `127.0.0.1:8000` only).
2. **Can a client spoof its real source IP?** No — nginx always sets
   `X-Forwarded-For`/`X-Real-IP` from its own observed `$remote_addr`,
   never appending to or trusting a client-supplied value; mutation-
   tested (mutation 1).
3. **Can a client forge trusted proxy headers?** No for
   X-Forwarded-For/Proto (both structurally overwritten by nginx,
   mutation-tested). X-Request-ID is deliberately trusted only as an
   opaque, length-bounded label, never a security decision.
4. **Can a runtime DB user alter migration metadata?** No —
   `permission denied`, live-tested.
5. **Can a runtime DB user alter immutable accounting data?** No —
   `permission denied` on UPDATE/DELETE for `audit_logs`,
   `inventory_movements`, `journal_entries`/`journal_lines`,
   live-tested.
6. **Can an operator restore a backup and prove it is usable?** Yes —
   Phase 17's restore-then-boot-the-real-app test proves the restored
   data is reachable through the running application, not just via a
   direct query.
7. **Can the system detect a failed backup?** Yes — the script's own
   `on_failure` trap writes a real failure status, proven by triggering
   an actual unreachable-remote failure (not a hand-written status
   file).
8. **Can the system detect PostgreSQL failure?** Yes —
   `ERPDatabaseDown`, live-tested by actually stopping PostgreSQL.
9. **Can the system detect migration failure?** Yes —
   `/health/migration` reports `schema_mismatch` against a real
   one-revision-behind database; `ERPMigrationMismatch` alert.
10. **Can a replayed POS transaction create duplicate financial
    effects?** No — proven under sequential retry, under a process
    restart in between, and under true concurrency (12 simultaneous
    distinct requests during a backend restart, zero duplicate/partial
    effects).
11. **Can a service restart cause duplicate financial effects?** No —
    same evidence as (10); the DB-level `UNIQUE` constraint is genuine
    defense-in-depth, proven by mutation testing that disabling only
    the fast-path optimization does not break correctness.
12. **Can a cross-store request expose another store's audit data?**
    No — `test_audit_log_endpoint.py`, plus Phase 16 step K creates a
    real cross-store sale and proves it.
13. **Can audit data expose sensitive payroll/financial information
    unnecessarily?** No — compensation rates are never written into
    `audit_logs` at all (M10 decision, re-verified).
14. **Can rate limiting be trivially bypassed?** No —
    mutation-tested (widening the zone was caught by the test that
    distinguishes nginx's own rejection from the app-level one), and
    header-rotation was tested directly and found ineffective.
15. **Are monitoring credentials least privilege?** Yes — `erp_monitor`
    is a dedicated `pg_monitor`-scoped role, not `erp_user`/`erp_app`.
16. **Are production secrets rejected when missing/unsafe?** Yes —
    `_forbid_insecure_production_config`, mutation-tested (disabling it
    was caught by 8 separate tests), and re-proven by a real process
    boot refusing to start with an insecure `SECRET_KEY`.
17. **Can a deployment be rolled back safely?** Application-level
    rollback is safe when paired with the matching schema downgrade;
    not safe in general across an independent schema migration — this
    is a documented, known limitation (`docs/M13_DESIGN.md` Section
    11), not a gap hidden by this audit.
18. **Are all operational procedures documented sufficiently for
    another engineer to execute without relying on your memory?**
    16 incident runbooks in `docs/M13_PRODUCTION_RUNBOOKS.md`, each
    with symptoms/checks/safe actions/dangerous actions/recovery/
    verification. `deploy/scripts/deploy.sh`'s own 7-step procedure is
    the deployment runbook.
19. **Did any test modify the shared `erp_dev` database in a way that
    cannot be cleaned safely?** No destructive test ever runs against
    `erp_dev`/`erp_test` directly — every backup/restore/migration
    test creates and drops its own disposable database. `erp_dev`
    itself accumulates ordinary test rows (stores, sales, users) the
    same way it always has across this project's entire test history
    (confirmed: `store_count` in the millions-of-test-runs range,
    long predating M13) — this is expected, shared-sandbox behavior,
    not damage M13 caused, and every row created is exactly the kind
    of row this application's own service layer is designed to create.
20. **Are there any known defects that this final report could
    accidentally hide by calling them "limitations"?** The two
    intentionally-skipped mutation-testing scenarios (item 5 above)
    are limitations of *this testing session* (a safety-classifier
    boundary), not defects in the product — the mechanisms they'd
    have tested are independently verified by the static suite. The
    `pg_hba.conf` gap (`docs/M13_DESIGN.md` Section 17 item 7) is
    called an accepted risk because the property it would additionally
    guarantee (password auth required) is already proven true in
    practice, just not pinned down by a shipped config file. Neither
    is a defect being relabeled to look smaller than it is.

## 7. What M13 deliberately did not build

Unchanged from `docs/M13_DESIGN.md` Section 17's own list, stated
there up front rather than discovered here: Docker Compose is
config-validated, not daemon-tested; real Let's Encrypt issuance is
documented procedure, not tested; off-box transport is proven against
a local `rclone` remote, not a physically separate host; no
zero-downtime deployment; no Grafana/dashboarding layer; no shipped
`pg_hba.conf`.

## 8. Exact files changed (M13 Phases 16–20)

New: `deploy/tests/test_integrated_production_session.py`,
`deploy/tests/test_failure_injection_disaster_recovery.py`,
`deploy/tests/test_adversarial_security_audit.py`,
`docs/M13_TESTING_SESSIONS.md`, `docs/M13_HARDENING_AUDIT.md`.

Modified: `deploy/prometheus/alerts.yml` (`ERPMonitoringTargetDown`),
`docs/M13_PRODUCTION_RUNBOOKS.md` (renamed from `docs/RUNBOOKS.md`,
Section 16 added), `deploy/tests/conftest.py` (orphaned-process
teardown sweep), `backend/scripts/backup_database.sh`,
`deploy/scripts/backup_offbox.sh`, `deploy/scripts/restore_offbox.sh`
(`chmod 600`), `deploy/tests/test_backup_offbox.py` (new tampered-
manifest test), 6 `backend/tests/*.py` files (black reformatting, no
behavioral change), `docs/M13_DESIGN.md` (Section 17 item 7, Section
18 added).

## 9. Final verdict

**PASS WITH CONDITIONS.**

All 20 self-audit questions above resolve to either a defended
property with live proof, or an explicitly stated, honestly-classified
accepted risk / known limitation — none conceal a defect. Every real
defect found during Phases 16–20 (Section 2) was fixed and has a
permanent regression test. The one HIGH-severity finding (backup
artifact world-readability, Section 4 item 24) is fixed and verified.
The conditions are the accepted risks and known limitations listed in
Sections 4, 6, and 7 — real production deployment requires the
operator to accept or address each one explicitly (most notably: ship
a `pg_hba.conf`, budget for genuinely off-box backup storage, and
accept that Docker Compose itself was never daemon-tested in this
sandbox) — not defects this milestone is unaware of.
