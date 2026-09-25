# M18 Hardening Audit

Companion to `docs/M18_DISCOVERY.md`, `docs/M18_DESIGN.md`, and
`docs/M18_TESTING_SESSIONS.md`. Distinguishes fixed, intentionally
unchanged, deferred, unknown, and production-prerequisite items across
all 28 discovery items, per the explicit instruction not to claim
something is "verified" unless it was actually tested, and not to call
a local result "CI verified" until GitHub Actions itself confirms it.

## 1. Fixed, with evidence, root cause, and regression coverage

| # | Item | Evidence | Root cause | Fix | Regression test | Financial impact |
|---|---|---|---|---|---|---|
| 1 | M4 accounting-core migration downgrade had no guard against real posted data | `M18_DISCOVERY.md` item 8: `8df037a45976`'s `downgrade()` unconditionally dropped `journal_entries`/`journal_lines`/`accounts` | Every *later* accounting migration (`581d2a07f38c`, M6, M14) was given this guard when written; the original M4 migration itself, written before that discipline existed, was never retrofitted | Added the same `RAISE EXCEPTION`-on-existing-data guard, checking `journal_entries` for any row (sufficient to cover `journal_lines` too via FK) | `test_m4_downgrade_refuses_when_journal_entries_exist` (new); mutation-tested live (`IF FALSE THEN` in place of the real condition let the downgrade proceed silently, test caught it, reverted, diff-confirmed byte-identical) | Closes a real path to silently destroying the entire general ledger via `alembic downgrade` against a database with any real financial activity |
| 2 | M6/M14 downgrade guards existed but were never proven against populated data | `M18_DISCOVERY.md` item 8: only the empty-DB full-chain test exercised them | A test-coverage gap, not a code defect — both guards were already correct | None needed — the two new tests confirm the existing guards were already correct, by construction | `test_m6_downgrade_refuses_when_ap_journal_entries_exist`, `test_m14_downgrade_refuses_when_approval_data_exists` — both pass | None — proves an existing invariant holds, does not change it |
| 3 | `backend` had no Docker healthcheck; nginx's `depends_on` was ordering-only | `M18_DISCOVERY.md` item 10 | Never added when the compose files were written | `HEALTHCHECK` in `backend/Dockerfile` against `GET /health`; nginx's `depends_on` now `condition: service_healthy` | Verified via `docker compose ... config` resolution (daemon-level `docker compose up` verification is out of scope — this sandbox's Docker daemon cannot start, same limitation `docker-compose.prod.yml`'s own header already documents) | None — deployment-topology fix, no financial-data path |
| 4 | No resource limits anywhere in `docker-compose.prod.yml` | `M18_DISCOVERY.md` item 10 | Never added | `deploy.resources.limits` on all three services, documented as conservative single-VPS starting values, not a load-tested recommendation | Config-parse-verified | None |
| 5 | `deploy/tests/` CI coverage stuck at 22/73 (M16 finding, not re-accepted) | `M18_DISCOVERY.md` item 27 | The 51 uncovered tests genuinely needed infrastructure the existing Docker-container-Postgres jobs don't provide (native `postgres` OS user, rclone/age, full Prometheus stack) — a real gap, not neglect | `test_deploy_procedure.py`'s hardcoded `.venv` path fixed (`sys.executable` fallback), wired into the existing `deploy-infra` job; new `deploy-infra-native` job (native `postgresql-16` + `nginx` + `rclone` + `age` + the full Prometheus/Alertmanager/node_exporter/postgres_exporter stack, all apt packages) covers the remaining 49 | All 49 tests run locally against this sandbox's own equivalent native stack before being wired in; all pass. First real CI run of the new job found and fixed two real environment-specific issues (see item 6) | None — test-infrastructure fix, no application code touched |
| 6 | First CI run of the new `deploy-infra-native` job failed (2 failed, 13 errors) | GitHub Actions run 35839753283 | (a) `sudo -u postgres psql -f <repo file>` failed with "Permission denied" — GitHub's runner-owned checkout directory tree is not world-readable, so the `postgres` OS user couldn't traverse into it; (b) `test_F_database_connectivity_failure` asserted `GET /health/db` returned 503 on the very next request after `service postgresql stop` returned success — true in this project's own development sandbox, not necessarily on the instant the GitHub runner's stop command returns | (a) A new CI step grants `o+rx` up the full path to `$GITHUB_WORKSPACE` and `o+rX` recursively within it, right after checkout; (b) the assertion now polls briefly (matching `test_monitoring.py`'s own DB-down test's existing tolerance, `_wait_for_alert_state(..., timeout=40.0)`) instead of a single immediate check | Verified locally (15/15 `test_integrated_production_session.py`, including F/G); final confirmation is GitHub Actions itself re-running against the fix commit (see §5 below for the run ID) | None — CI/test-robustness fix only |
| 7 | `pg_hba.conf`/PostgreSQL client auth had no project-owned requirement documented | `M18_DISCOVERY.md` item 13 | Genuinely absent from every prior milestone's docs | `deploy/postgres/PG_HBA_REQUIREMENTS.md` — the exact behavioral requirement (never `trust`, `scram-sha-256` minimum), verified against both real deployment models' actual defaults, with a verified one-line operator checklist query | The checklist query (`SELECT count(*) FROM pg_hba_file_rules WHERE auth_method IN ('trust', 'password')`) was run against this sandbox's real cluster and confirmed to return `0` | None — documentation, per this milestone's own instruction not to invent a distribution-specific file |

## 2. Investigated and found already correct — no code change

| Item | Finding |
|---|---|
| `ENVIRONMENT=production` enforcement (discovery's initial "residual risk" on item 12) | `docker-compose.prod.yml` hardcodes `ENVIRONMENT: production` into the backend service's `environment:` block — not operator-settable, not sourced from `.env.prod`. There is exactly one documented production deployment path in this codebase, and it cannot omit this variable. Closed as verified, not partially verified. |
| `.env.prod.example` secret hygiene | Every value audited line by line: real non-secret settings or clearly-marked `CHANGE_ME_...` placeholders; no SMTP/fiscal credentials exist (no such feature); backup key is public-key-only by design. |
| Multi-worker / rate-limiter single-process assumption | Confirmed correct today (no `--workers` anywhere) and already explicitly documented in `backend/app/core/rate_limit.py` and `docs/M13_DESIGN.md`. No enforcement code added — there is no worker-count knob in this codebase to guard against yet; building one now would be speculative production-safety infrastructure for a scaling mode nothing else in this codebase supports (`M18_DESIGN.md` §7). |
| Monitoring/alerting network exposure | Alertmanager and Prometheus both bind `127.0.0.1` only, confirmed via the real `ExecStart` lines in `deploy/systemd/erp-*.service` — a deliberate, already-correct design choice, not an oversight requiring authentication to be bolted on. |
| Failure recovery for application-raised mid-transaction failures | Comprehensive existing coverage across sales/AP/inventory/returns/replenishment/reports, all rolling back completely — re-confirmed, not re-built. |

## 3. Deferred / unknown / production prerequisites — reported, not built

| Item | Classification | Why not built |
|---|---|---|
| Point-in-time recovery (WAL archiving) | **Missing, previously unacknowledged** — now documented for the first time (`M18_DISCOVERY.md` item 7, `M18_DESIGN.md` §8) | No prior milestone ever specified this as a requirement; this codebase's entire backup design is built around full logical `pg_dump` snapshots. Building a WAL-archiving pipeline unprompted is exactly the speculative production infrastructure this milestone's own instructions forbid. **Production prerequisite** for any deployment whose recovery-point objective is tighter than "since the last full backup." |
| Off-box backup's genuine physical separation | **Partially verified, unchanged from M13** | Off-box transport (`rclone`+`age`) is tested against a local-filesystem `rclone` remote, not a real network-separated host — this sandbox (and GitHub Actions) has no real S3/object-storage network path. The transport mechanism is proven; physical off-box-ness is not. Explicitly labeled as such since M13; not re-litigated here. |
| No external Alertmanager notification channel | **Intentional limitation, explicitly documented** | `alertmanager.yml`'s `webhook_configs` is a commented-out placeholder by design (its own header states this is deferred to real-deployment configuration). Alerts are visible in Alertmanager's own UI/API but page no one off-box by default. **Production prerequisite**: an operator must fill in a real receiver before this actually notifies anyone. |
| Credential-rotation runbook | **Gap, closed this milestone** | `docs/M13_PRODUCTION_RUNBOOKS.md` covered 16 scenarios but never infra-credential rotation specifically (only compromised-*user*-credential revocation). Closed in `docs/M18_OPERATIONS_RUNBOOK.md`. |
| Audit-log unbounded growth / long-term capacity planning | **Intentional limitation, explicitly documented since M12** | `docs/M12_DESIGN.md`: "no new retention policy is invented — that's a business decision, not an engineering one." Still true; not re-litigated. **Production prerequisite** for long-term operation at scale. |
| Disaster-recovery RTO/RPO measurement | **Closed this milestone** | No prior milestone recorded exact backup/restore timing against representative data. `M18_TESTING_SESSIONS.md` Session K now records it: 1.53s backup / 3.21s restore against a 205 MB, ~150k-row representative database — explicitly labeled a local simulation (same-host, not a genuinely separate physical host, consistent with the off-box limitation above). |

## 4. False-positive findings

None. Every item discovery flagged as worth investigating (the full
28-item matrix in `M18_DISCOVERY.md`) resolved to one of:
solid-and-verified, a confirmed narrow gap (now fixed), or confirmed
intentional/documented design — none were false alarms requiring
retraction.

## 5. Exact final counts

- Backend: **936 passed** (`pytest -q`; 933 + 3 new migration tests).
- Frontend: **49 passed** (14 files) — unchanged by M18.
- `deploy/tests/`: **73/73** now wired into CI (24 in `deploy-infra`,
  49 in the new `deploy-infra-native` job) — up from 22/73.
- Migration tests: **16 passed** (13 + 3 new); single head, still
  `4a83c462dbff` — M18 modified one migration's `downgrade()` (a
  guard, not a schema change), so the head revision is unchanged.
- `ruff check .` / `black --check .` / `mypy app` — all clean.
- `npx tsc -b` / `oxlint .` / `prettier --check .` — all clean (no
  frontend files changed).
- CI: run 35839753283 (first push of the new native job) found 2
  real environment-specific issues in the new job; fixed in commit
  `8e1450e` and re-verified — final run ID and conclusion recorded in
  the completion report once confirmed green.

## 6. Production prerequisites (consolidated)

1. PITR/WAL archiving — build before any deployment whose RPO is
   tighter than "since the last full backup" (§3).
2. Genuine off-box backup transport — configure a real `rclone` remote
   pointing at physically separate storage before relying on
   `backup_offbox.sh` for actual disaster recovery, not just its
   already-proven encryption/checksum/retention mechanics (§3).
3. Alertmanager external notification — fill in a real receiver in
   `deploy/prometheus/alertmanager.yml` before relying on alerts to
   page anyone off-box (§3).
4. Audit-log/long-term data volume capacity planning — a business
   decision this codebase deliberately defers (§3).
5. `pg_hba.conf` — confirm the documented requirement
   (`deploy/postgres/PG_HBA_REQUIREMENTS.md`) against whatever the
   actual production Postgres instance is (managed cloud Postgres, a
   different base image, etc.) if it differs from this project's two
   verified deployment models.
6. Compose resource limits (`docker-compose.prod.yml`) are
   conservative starting values for the single-VPS scale this project
   targets — re-tune for a larger deployment rather than assuming
   they're sized for one.

## 7. Remaining limitations (carried forward, not new)

- No stock-adjustment correction workflow exists (M17 finding,
  unrelated to production readiness, unchanged).
- No shift-close correction/reopen workflow exists (M15/M17 finding,
  unchanged).
- App-level rate limiting and the rest of the in-memory single-process
  assumptions remain correct only as long as the deployment stays
  single-worker (§2) — this is a hard requirement, not merely a
  current default.
