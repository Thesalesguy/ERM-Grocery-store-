# M18 Discovery — Production Readiness & Operational Resilience

Read-only discovery for M18. The question this milestone answers is not
"does the application work" (M1–M17 already established that) but "can
this system be safely deployed, backed up, restored, monitored, upgraded,
and operated when something goes wrong." Findings below are tagged FACT
(directly read in code/config/tests), INFERENCE (reasonable but not
directly confirmed), or UNKNOWN (no evidence found either way), each with
file:line citations gathered via six parallel read-only investigations
across the actual repository (not assumed from milestone names — M12/M13
already built a great deal of this infrastructure, and this discovery
re-verifies what is actually there today, on this HEAD).

## 0. Baseline (Phase 0, recorded before any discovery work)

- Branch: `claude/grocery-erp-pos-architecture-h8a53g`, HEAD `2a0b600` (M17 final).
- `origin/main` at `052a250` (merge of an earlier PR from this branch).
- Working tree: clean.
- Migration head: single, `4a83c462dbff`.
- Backend: `pytest -q` → **933 passed**.
- Frontend: `npx vitest run` → **49 passed** (14 files).
- Deploy-infra (5 CI-wired files): **22 passed**.
- `deploy/tests/` total inventory: **73 tests collected across 12 files**.
- Migration tests: **13 passed**.
- `ruff check .` / `black --check .` / `mypy app` — all clean.
- `npx tsc -b` / `oxlint .` / `prettier --check .` — all clean.
- Last CI run on this branch (35833592026, HEAD `2a0b600`): `status=completed`, `conclusion=success` on all three jobs — CI is already green on the current baseline HEAD.

## 1. Method

Six parallel read-only investigations (Explore agents, isolated worktrees,
no code changes) covered the 28 items below by reading actual scripts,
configs, Dockerfiles, CI workflow, migration files, and existing tests —
not by trusting filenames, docstrings, or prior milestones' own claims.
Each item's classification follows the vocabulary the milestone spec
requires: **verified** / **partially verified** / **unverified** /
**missing** / **intentional limitation** / **production prerequisite** /
**out of scope**. "A configuration file is not proof of a working
operational process" — every "verified" below is backed by a test that
actually exercises the behavior, not just a config that looks right.

## 2. The 28-item matrix

### Backup & recovery (items 1–7)

| # | Item | Finding | Classification |
|---|---|---|---|
| 1 | Database backup | `backend/scripts/backup_database.sh` runs a real `pg_dump -Fc` full dump, `chmod 600`'d. End-to-end tested by `backend/tests/test_backup_restore.py` (real `pg_dump`/`pg_restore` cycle, multi-module seeded data, full integrity-snapshot diff, post-restore functional re-exercise) — runs in CI's `backend` job by construction (`pytest -q`, no path filter). | **Verified** |
| 2 | Restore | `backend/scripts/restore_database.sh` (`dropdb --force`+`createdb`+`pg_restore --clean --if-exists`), tested by the same suite as #1. | **Verified** |
| 3 | Off-box backup | `deploy/scripts/backup_offbox.sh` (age-encrypt + `rclone copy`/`check`/`delete`). CI-excluded (`.github/workflows/ci.yml:112-121`, needs `rclone` + native `postgres` OS user). Tested only against a local-filesystem `rclone` remote, explicitly labeled a simulation in the test file's own docstring and in `docs/M13_DESIGN.md`. | **Partially verified** (mechanism proven; real off-box transport unverified; not in CI at all) |
| 4 | Backup encryption/security | `age` asymmetric encryption, public key via `BACKUP_AGE_PUBLIC_KEY`, private key documented to never live on the DB host. Dump and decrypted-restore artifacts `chmod 600`'d (a real M13 bug fix). | **Verified** (mechanism); key custody in a real prod deploy is **INFERENCE/documented-only** |
| 5 | Backup retention | Off-box retention is scripted (`rclone delete --min-age`) and tested (purges a seeded 30-day-old file). Local-directory retention is explicitly not automated — documented as operator-managed by design. | Off-box: **verified**. Local: **intentional limitation** |
| 6 | Backup verification | SHA-256 manifest checksum re-verified at restore time; `rclone check --checksum` post-upload. Adversarially tested: corrupted remote backup detected + refused, tampered manifest checksum detected + refused. Routine `backup_offbox.sh` itself does not perform an automatic test-restore (only checksum/transport verification) — that only happens in the test suites. | **Verified**, with one **partial gap** (no automatic restore-and-verify step in the production backup script itself) |
| 7 | Point-in-time recovery | No WAL archiving / `archive_command` / `pg_basebackup` / PITR configuration exists anywhere in the repo. Not even listed as a known limitation in M12/M13's own "what we didn't build" sections. Only full logical `pg_dump` snapshots are supported — recovery granularity is bounded by the last full backup. | **Missing**, and previously **unacknowledged** — this discovery is the first time it is documented as a gap |

### Migration & deployment (items 8–10)

| # | Item | Finding | Classification |
|---|---|---|---|
| 8 | Migration safety | 21 files, confirmed single unbranched chain to head `e0d2359bb08a` (corroborated by `test_migrations.py`'s own head-revision constant). Financial/inventory downgrades (M6 AP, M7 credit notes) guard with a `RAISE EXCEPTION` if dependent data exists rather than silently dropping. NOT NULL/unique-constraint additions follow nullable→backfill→constrain (verified in M3, M5 spot checks). Populated-data downgrade-guard tests exist for **M7–M11 only** — no equivalent test exists for M4 (accounting core), M5, M6 (AP/vendor accounting), M12, or M14, despite M6 already having guard *code*. | **Partially verified** — mechanism is sound where tested; **coverage gap** on M4/M6 specifically (both financially material) is a concrete M18 action item |
| 9 | Migration rollback | `deploy/scripts/deploy.sh`'s `_print_rollback_guidance()` documents the decision tree (migration failure → DB untouched, transactional DDL; post-migration failure → fix-forward or `alembic downgrade` **paired with** the matching previous app version, never independently). `docs/M13_DESIGN.md` explicitly states pure app-code rollback across a schema change is "not safe in general" — an honest, stated limitation, not a false safety claim. The schema-downgrade-only path is tested (`test_migrations.py`); the full paired procedure (schema downgrade + actual previous-version app redeploy) is documented but not end-to-end tested. | **Partially verified** (documented honestly; not fully proceduralized as an automated test) |
| 10 | Docker/Compose deployment | `docker-compose.yml` (dev) + `docker-compose.prod.yml` (prod overlay) exist. `db` and `nginx` have Docker `healthcheck` blocks; **`backend` has none** (no `HEALTHCHECK` in `backend/Dockerfile`, no `healthcheck:` block in either compose file), and nginx's `depends_on: backend` is a plain ordering dependency, not `condition: service_healthy` — nginx can start proxying before the backend is actually ready. `restart: always`/`unless-stopped` set on every service. **No resource limits** (`mem_limit`/`cpus`/`deploy.resources`) anywhere. | **Partially verified** — restart policies verified; backend healthcheck **missing** (production prerequisite, cheaply fixable); resource limits **missing** (production prerequisite) |

### Network, secrets, and access (items 11–13)

| # | Item | Finding | Classification |
|---|---|---|---|
| 11 | Reverse proxy / TLS | nginx config genuinely terminates TLS (`ssl_certificate`, TLSv1.2/1.3, HIGH ciphers, HTTP→HTTPS redirect). Certificate is self-signed only — script header explicitly states a real deployment needs `certbot --nginx` against a public domain, not exercised here. `test_proxy_tls.py` tests real nginx+uvicorn processes for redirect/headers/body-limits/loopback-binding/XFF-spoof-resistance, with cert validation deliberately disabled (`verify=False`) since there's no public domain in this sandbox. | TLS termination mechanism: **verified**. CA-validated production cert: **unverified / production prerequisite** |
| 12 | Secrets management | Production secrets via git-ignored `.env.prod` (templated by `.env.prod.example`), loaded per-service. `backend/app/core/config.py` has a fail-closed validator (`_forbid_insecure_production_config`) that raises if `ENVIRONMENT=production` and `SECRET_KEY`/`DATABASE_URL` are known-insecure/dev values or `CORS_ORIGINS` contains `*`. Compose's `${VAR:?...}` syntax independently forces required vars. **Residual risk**: every one of these checks is gated on `ENVIRONMENT` actually being set to `"production"` — if a real deployment left it at the `"development"` default, all of these fail-closed checks would be silently bypassed. Whether `deploy/scripts/deploy.sh` actually forces/verifies this was not confirmed in this pass. | **Partially verified** — strong defense-in-depth, but the single point all of it hinges on (`ENVIRONMENT` being set correctly at deploy time) needs direct confirmation |
| 13 | pg_hba.conf / PostgreSQL auth | No project-owned `pg_hba.conf` or equivalent client-auth config exists anywhere in the repo (exhaustive search, zero results). `docker-compose.yml` uses the stock `postgres:16` image with no auth-method override — client auth is left entirely to the upstream image's defaults. `deploy/tests/test_db_security.py` tests network exposure, idle-transaction timeouts, connection logging, and pool headroom, but nothing about the actual auth method (trust/md5/scram-sha-256). Network-level isolation (no published Postgres port in prod, `listen_addresses='localhost'`) is a real compensating control, but the auth method itself is unreviewed and unpinned by this project. | **Missing / production prerequisite** — per the milestone's own instruction, the correct M18 action is to document the exact production requirement, not invent a distribution-specific `pg_hba.conf` |

### Health, observability, and alerting (items 14–19)

| # | Item | Finding | Classification |
|---|---|---|---|
| 14 | Application health checks | `GET /health` (liveness only), `GET /health/db` (`SELECT 1`, 503 on failure), `GET /health/migration` (compares on-disk Alembic head vs. DB's `alembic_version`, 503 on mismatch/unreadable) — a genuine three-tier model, all in `backend/app/api/v1/endpoints/health.py`. | **Verified** |
| 15 | Database health checks | Same `/health/db` as #14, independently polled every 5s by the Prometheus exporter, exposed as `erp_db_up`. | **Verified** |
| 16 | Monitoring | No in-app `/metrics`; a standalone `deploy/prometheus/erp_exporter.py` (systemd unit) exposes `erp_api_up`/`erp_db_up`/`erp_migration_current`, nginx request-status-class counters (tailed from nginx's JSON access log), and auth-failure/token-reuse counters (polled from `audit_logs`). Standard `node_exporter`/`postgres_exporter` cover host/DB resource metrics. `deploy/tests/test_monitoring.py` runs the **real** Prometheus/Alertmanager/exporter stack as live subprocesses and proves `ERPDatabaseDown` and `ERPBackupFailed` actually fire end-to-end. No app-level request-latency histograms or SQLAlchemy pool metrics exist (explicitly scoped out by the exporter's own docstring as "a small custom exporter, not a new platform"). | **Verified** for what's built; missing latency/pool metrics is an **intentional limitation** |
| 17 | Alerting | `deploy/prometheus/alerts.yml` defines 12 rules across 5 groups (availability, backup, resources, security, monitoring-self), every rule has `severity` + an actionable annotation citing a runbook section. Alertmanager and Prometheus both bind `127.0.0.1` only (loopback, no network exposure, no auth needed as a result) — this is stated as a deliberate design choice in the systemd unit files. **No external notification channel is configured** (`alertmanager.yml`'s `webhook_configs` is a commented-out placeholder) — alerts are visible in Alertmanager's own UI but page no one off-box by default; the file's own header states this is deferred to real-deployment configuration. Only 2 of 12 rules (`ERPDatabaseDown`, `ERPBackupFailed`) were found live-fire-tested end-to-end; the rest rely on `promtool check rules` syntax validation plus a structural "has severity + actionable annotation" test. | Rule correctness & loopback-only binding: **verified**. External notification: **intentional limitation** (documented, not wired). Live-fire coverage: **partially verified** |
| 18 | Logging | JSON structured logging (`pythonjsonlogger`) with a `RedactSensitiveFieldsFilter` stripping password/secret/token/etc. from log payloads. Per-request correlation ID (`X-Request-ID`, `contextvars`-based), stamped on every log line. `deploy/tests/test_observability_correlation.py` verifies this against the real nginx+uvicorn stack: client-supplied ID survives the full round trip, server-generated ID matches between response header and nginx's own access log, 15 concurrent requests get distinguishable IDs, a real login is traceable end-to-end to its audit-log row, and a real failed login leaks no plaintext password into any log. | **Verified** |
| 19 | Audit-log retention | `audit_logs` is explicitly append-only by design — the docstring states application code must never UPDATE/DELETE it, and UPDATE/DELETE are revoked at the database-role level by migration. No pruning/retention script or scheduled job exists; `docs/M12_DESIGN.md` explicitly states retention policy is "a business decision, not an engineering one" and was deliberately not invented. Only *backups* have a retention policy (14-day) — live audit-log rows grow unbounded forever. | **Intentional limitation**, but also a genuine **production prerequisite** — an unbounded JSON-column table will eventually need a capacity-planning decision that nothing in this repo currently owns |

### Concurrency & operations (items 20–24)

| # | Item | Finding | Classification |
|---|---|---|---|
| 20 | Rate limiting | Two layers: app-level (`backend/app/core/rate_limit.py`, in-process `dict`+`threading.Lock` — the module's own docstring already documents this as breaking under horizontal scaling) and nginx-level (`limit_req_zone`/`limit_conn_zone`, shared across nginx's own worker processes within one nginx instance). The deployment model runs exactly one nginx instance, so nginx-level limiting is authoritative for the current topology. `test_rate_limiting.py`'s own docstring states the assumed topology explicitly ("a single VPS, one nginx process, one backend process") and names the risk if that ever changes. | **Verified for the documented single-instance topology; intentional limitation** beyond it (already documented, not silently broken) |
| 21 | Multi-worker behavior | `backend/Dockerfile` and `docker-compose.prod.yml` both run bare `uvicorn ... --host 0.0.0.0 --port 8000` with no `--workers` flag (defaults to 1). No gunicorn, no `WEB_CONCURRENCY`, no Procfile anywhere. This matches the rate limiter's own documented single-process assumption. However, single-worker is established only by *absence* of a `--workers` flag, not by an explicit pin or a startup guard — nothing in code would stop someone from later adding `--workers N`. | **Verified** as the current deployment shape; **production prerequisite** — no enforcement exists to keep it that way |
| 22 | Scheduled/background jobs | No APScheduler/Celery/cron-in-app exists anywhere (explicitly confirmed absent, and explicitly stated as intentional in `docs/M12_DESIGN.md`: "None exist today... backups are documented as an operator-triggered cron entry"). No crontab/systemd-timer file is actually checked into the repo for the backup job — only documented as an operator setup step in a README. `ERPBackupStale` exists specifically to detect if that assumed-but-unenforced cron job stops running. | **Intentional limitation** (no background-job mechanism by design); the backup cron entry itself is a **production prerequisite** not captured as code/IaC |
| 23 | Failure recovery | Strong coverage for application-raised mid-transaction failures: `test_sale_finalization_failure_injection.py` proves a monkeypatched failure during accounting posting leaves zero rows across Sale/SaleItem/Payment/InventoryMovement/AuditLog and leaves `current_qty_on_hand` untouched. Similar dedicated failure-injection test files exist for replenishment, AP, stock counts/transfers, sales returns, and reports. Real backend **process** restart mid-retry is also tested (`test_failure_injection_disaster_recovery.py`, kill+restart uvicorn, confirms exactly-once idempotency survives). **Not covered**: actual PostgreSQL connection severance/crash mid-commit — explicitly and reasonably excluded as "inherently racy, no test can control the timing deterministically" (`test_connectivity_failure.py`'s own docstring), relying instead on Postgres's own transactional guarantees plus the idempotency-key retry path. | **Verified** for application-level failure injection; **intentional limitation** (reasoned, not an oversight) for literal connection-loss injection |
| 24 | Disk/database capacity risks | `ERPDiskSpaceLow` (<10% free) and `ERPDatabaseConnectionsNearExhaustion` alerts exist — monitoring, not capacity planning. No table partitioning, archival, or data-deletion path exists anywhere; `docs/M12_DESIGN.md` explicitly states this is a deferred business decision, not an oversight. List endpoints are capped (`le=500`) which bounds single-query memory but not overall table growth. | **Missing**, explicitly acknowledged as out of scope by prior design docs — still a genuine **production prerequisite** for long-term operation |

### Documentation & process (items 25–28)

| # | Item | Finding | Classification |
|---|---|---|---|
| 25 | Operational runbooks | `docs/M13_PRODUCTION_RUNBOOKS.md` covers 16 real scenarios (app/DB unavailable, disk full, backup failure, restore, migration failure, auth incident, duplicate transaction, accounting/inventory/AP/payroll reconciliation, cross-store access, expired TLS cert, high error rate, monitoring-component-down), each command cited against a real test or script rather than written aspirationally. **No dedicated credential-rotation runbook** exists (the closest is compromised-*user*-credential revocation, not infra credential rotation). | **Verified** for what exists; credential rotation is a **gap** to close in M18's own runbook |
| 26 | Deployment rollback | See item 9 — same finding, documented here as the operational-process view: app-only rollback across a schema change is explicitly documented as unsafe (not falsely claimed safe); schema-downgrade-paired-with-matching-app-version is the tested-safe path. | **Partially verified** (see item 9) |
| 27 | CI coverage of production-critical tests | `backend/tests/` is covered completely by construction (`pytest -q`, no path filter, ~90 files). `deploy/tests/` — only 5 of 12 files / 22 of 73 tests run in CI; the other 7 files (`test_integrated_production_session.py`, `test_adversarial_security_audit.py`, `test_deploy_procedure.py`, `test_backup_offbox.py`, `test_failure_injection_disaster_recovery.py`, `test_db_security.py`, `test_monitoring.py`) don't run in CI at all, for reasons already partially documented in `docs/M16_HARDENING_AUDIT.md` §4 (need nginx+Prometheus+Alertmanager+exporters, rclone, gnupg/age, certbot, or a native `postgres` OS user reachable via `sudo -u postgres`, or a `backend/.venv` this CI doesn't create). | Backend: **verified**. Deploy-infra: **partially verified** — a real, previously-documented gap that M18 Phase 4 must re-investigate rather than re-accept |
| 28 | Disaster recovery assumptions | `test_backup_restore.py` performs a genuine local backup→destroy(recreate empty DB)→restore→verify cycle with full integrity-snapshot diff and a post-restore functional sale — no recovery-time (RTO) measurement was found anywhere. The off-box path (`test_backup_offbox.py`) restores into a differently-named DB (not destroy-in-place) and is explicitly and repeatedly labeled a local simulation in its own docstring and in `docs/M13_DESIGN.md`/`M13_HARDENING_AUDIT.md` — "the transport mechanism is proven, physical off-box-ness is not." | Local destroy/restore/verify: **verified** (no timing recorded — a gap). Off-box: **partially verified**, honestly labeled a simulation |

## 3. Summary — confirmed gaps mapped to M18 actions

| Gap | Confirmed by | M18 phase / action |
|---|---|---|
| PITR/WAL archiving entirely absent and previously unacknowledged | Item 7 | Document as a known limitation with the exact production requirement (Phase 1/14); out of scope to build a full WAL-archiving pipeline this milestone — no prior design ever specified it as a requirement, and inventing one now would be exactly the speculative infrastructure M18's own instructions forbid |
| Populated-data migration downgrade-guard tests missing for M4 (accounting core) and M6 (AP), despite M6 already having guard code | Item 8 | Phase 3: add populated-data downgrade tests for the two financially material gaps |
| Backend container has no Docker healthcheck; nginx depends_on is ordering-only | Item 10 | Phase 6/10: add `HEALTHCHECK` to `backend/Dockerfile` targeting `/health`, and `condition: service_healthy` on nginx's `depends_on` |
| No resource limits in compose files | Item 10 | Phase 6: document/add reasonable `mem_limit`/`cpus` defaults |
| No project-owned pg_hba.conf; auth method unpinned | Item 13 | Phase 6: document the exact production requirement (per the milestone's own instruction, do not invent a distribution-specific file) |
| `ENVIRONMENT=production` is the single point all secret/CORS/docs fail-closed checks hinge on — not yet confirmed enforced at deploy time | Item 12 | Phase 5: read `deploy/scripts/deploy.sh` in full and confirm/harden this |
| No credential-rotation runbook | Item 25 | Phase 14: add to `M18_OPERATIONS_RUNBOOK.md` |
| `deploy/tests/` CI coverage still 22/73, not simply re-acceptable | Item 27 | Phase 4: re-investigate each of the 7 uncovered files; this sandbox's own native Postgres (confirmed working via `sudo -u postgres` earlier this session) is direct evidence a native-postgres CI job is feasible for at least the `sudo -u postgres`-dependent subset |
| No worker-count enforcement — single-worker is correct today but unenforced | Item 21 | Phase 9: consider a fail-closed startup guard consistent with the existing `_forbid_insecure_production_config` pattern, or document explicitly if a guard isn't warranted |
| No RTO timing recorded for local disaster-recovery cycle | Item 28 | Phase 11: record exact backup/restore duration and DB size during the disaster-recovery exercise |
| Off-box backup and 6 other deploy test files never run in CI, so their pass/fail status is not continuously verified | Items 3, 27 | Phase 4 (CI expansion) + Phase 15 (final CI verification) |

## 4. Non-findings — investigated and confirmed already solid

Idempotency and business-critical mutation serialization (sales, AP,
inventory) are DB-backed (unique constraints + row locks), not
in-memory — proven by a real backend-process-restart test, not just
code reading. Application-raised mid-transaction failures roll back
completely for accounting and inventory writes. Structured logging with
correlation IDs and password redaction is genuinely verified end-to-end
against the real nginx+uvicorn stack. TLS termination, health checks,
and the core Prometheus/Alertmanager pipeline are real, not merely
configured. None of these need further M18 work beyond carrying their
verified status forward.
