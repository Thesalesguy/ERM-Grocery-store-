# M18 Design — Production Readiness & Operational Resilience

Companion to `docs/M18_DISCOVERY.md`. Covers every design decision this
milestone makes, in the order discovery raised them. Per this
milestone's own instruction, several items resolve to "document the
exact requirement, do not build new infrastructure" — that is a
deliberate design decision, not a skipped one, and is recorded as such
below rather than silently omitted.

## 1. M4 accounting-core migration downgrade guard (Phase 3)

**Problem** (discovery item 8): `8df037a45976` (M4 accounting core) was
the one migration in the entire chain whose `downgrade()` drops
`journal_entries`/`journal_lines`/`accounts` — the general ledger
itself — with no guard, while every *later* accounting migration
(`581d2a07f38c`, M6, M14) already refuses to downgrade past real posted
data.

**Design**: add the same guard pattern already established by those
three migrations — a `DO $$ ... RAISE EXCEPTION ... $$` block that
checks `journal_entries` for any row before any destructive step.
`journal_lines` needs no separate check: it cannot have a row without a
parent `journal_entries` row (FK), so the single check covers both
tables, matching the simpler of the two checks M6 already uses for its
own two-part guard.

**Alternative considered and rejected**: mirroring M6's two-check
pattern (a `journal_lines`-joined-to-`accounts` check *and* a
`journal_entries.source_type` check) would be needed if M4 only cared
about *specific* accounts or source types — it doesn't; M4's downgrade
drops the entire ledger schema, so *any* posted entry from *any* source
blocks it. One check is both sufficient and correct.

**Regression coverage**: a new populated-data test
(`test_m4_downgrade_refuses_when_journal_entries_exist`) proves the
guard fires against a real inserted row, mirroring
`test_m7_downgrade_refuses_when_credit_note_data_exists`'s own
discipline. Mutation-tested live: replacing the guard's condition with
`IF FALSE THEN` let the downgrade proceed silently and the test caught
it; reverted, confirmed a byte-identical restore via `diff`.

**Also added** (same discovery gap, lower severity — existing guard
code, no test): `test_m6_downgrade_refuses_when_ap_journal_entries_exist`
and `test_m14_downgrade_refuses_when_approval_data_exists`, proving M6's
and M14's *already-correct* guards against real populated data for the
first time (previously only exercised against an empty database by the
full upgrade/downgrade/upgrade cycle test).

**Non-goal**: M5's downgrade drops columns
(`quantity_returned`/`*_refunded`) it added — this is the expected,
inherent semantics of reverting to a schema that never had those
columns, not a guardable "orphan the ledger" failure mode like M4/M6/M14.
No guard was added there; inventing one would defend against nothing
real. M12's downgrade only touches role privileges (no row data), and
is already directly tested.

## 2. Backend Docker healthcheck (Phase 6/10)

**Problem** (discovery item 10): `backend` was the only service in
`docker-compose.prod.yml` with no `healthcheck`, and nginx's
`depends_on: - backend` was a plain ordering dependency — nginx could
start proxying before uvicorn was actually accepting connections.

**Design**: add a `HEALTHCHECK` to `backend/Dockerfile` hitting the
existing `GET /health` endpoint via `python -c "urllib.request..."` —
Python is already in the image; no new package (`curl`/`wget`) is
worth adding just for this. Change nginx's `depends_on` to
`backend: condition: service_healthy`. Verified via
`docker compose ... config` (parses/resolves correctly; this sandbox's
Docker daemon cannot start a container to prove the runtime behavior,
same limitation `docker-compose.prod.yml`'s own header comment already
documents for every other daemon-level check in this project).

## 3. Compose resource limits (Phase 6)

**Problem** (discovery item 10): no `mem_limit`/`cpus`/`deploy.resources`
anywhere — nothing stops one runaway container from starving the host.

**Design**: `deploy.resources.limits` (memory + cpus) on all three
`docker-compose.prod.yml` services (`db: 1g/1.0`, `backend: 512m/1.0`,
`nginx: 128m/0.5`), sized conservatively for the single-small-VPS
deployment `docs/TECHNICAL_BLUEPRINT.md` itself targets — explicitly
documented in-file as a starting point for that scale, not a load-tested
sizing recommendation, so a larger deployment isn't silently capped
without the operator realizing why. `deploy.resources.limits` (not the
legacy `mem_limit`/`cpus` top-level keys) because Compose V2 (what this
project's own comments already assume — `docker compose`, not
`docker-compose`) applies `deploy.resources` on a plain `up`, without
needing Swarm mode.

## 4. `deploy/tests/` CI coverage expansion (Phase 4)

**Problem** (discovery item 27): 22 of 73 deploy tests ran in CI;
`docs/M16_HARDENING_AUDIT.md` had documented this as needing three
things a Docker-container Postgres service can't provide: a native
`postgres` OS user via `sudo -u postgres`, `rclone`+`age`, and a full
Prometheus/Alertmanager/exporter stack. M18's instruction was explicit:
re-investigate, don't just re-accept the prior number.

**Design decisions, each backed by direct local evidence before being
wired into CI** (never wired something in on faith that it would pass):

1. `test_deploy_procedure.py`'s only actual blocker was a hardcoded
   `backend/.venv/bin/python` path (not the venv's *existence* as a
   concept — `PYTHON_BIN` is just an interpreter to shell out to).
   Fixed with a `sys.executable` fallback, the exact pattern already
   used in `backend/tests/test_backup_restore.py`. Verified locally (2/2
   pass), then wired into the *existing* `deploy-infra` job (which
   already has the `erp_dev` database this file needs; only needed
   adding `erp_test`, mirroring the `backend` job's own bootstrap step).

2. The remaining 49 tests (`test_db_security.py`, `test_backup_offbox.py`,
   `test_monitoring.py`, `test_adversarial_security_audit.py`,
   `test_failure_injection_disaster_recovery.py`,
   `test_integrated_production_session.py`) all need a genuinely
   different Postgres deployment model (native, not Docker-container) —
   confirmed function-by-function via a dedicated investigation, not
   assumed from the file names. This development sandbox happens to
   already run PostgreSQL natively with `rclone`/`age`/the full
   Prometheus/Alertmanager/node_exporter/postgres_exporter stack all
   pre-installed — direct, empirical evidence a native-Postgres CI job
   is feasible, not a guess. **All 49 tests were run locally against
   that native stack before writing a single line of CI config**, and
   all 49 passed.

   **New CI job**: `deploy-infra-native`, additive to (not replacing)
   the existing `deploy-infra` job — that job's Docker-container
   Postgres pattern stays lighter and untouched, matching the `backend`
   job's own pattern; there's no reason to make its already-proven tests
   depend on a heavier install. The new job installs `postgresql-16`
   (pinned to match the `postgres:16` image the other two jobs use, for
   behavioral parity) + `nginx` + `rclone` + `age` +
   `prometheus`/`prometheus-alertmanager`/`prometheus-node-exporter`/
   `prometheus-postgres-exporter` — all standard Ubuntu apt packages,
   the same ones already present in this development sandbox. Bootstrap
   recreates the same starting state the `postgres:16` Docker image's
   own entrypoint provides automatically (a superuser `erp_user` role +
   `erp_dev`/`erp_test` databases), since a native install starts with
   only the OS-level `postgres` superuser.

**Result**: 73/73 deploy tests now run in CI (24 in the existing job +
49 in the new one), up from 22/73 — the entire suite, not a partial
improvement. Nothing was mocked, stubbed, or faked to get there.

**Non-goal / explicitly not done**: merging the two jobs into one. Kept
separate so the faster, already-proven job's risk profile doesn't
change, and so a failure in the new (heavier, more moving parts) job is
immediately attributable rather than mixed into the existing job's
signal.

## 5. Secrets/configuration audit (Phase 5)

**Finding, refining discovery item 12**: discovery flagged that every
fail-closed secret/CORS/docs-exposure check in
`backend/app/core/config.py` is gated on `ENVIRONMENT=production` being
set, and asked whether that's actually enforced at deploy time rather
than left to operator discretion. Direct read of
`docker-compose.prod.yml` (the one real production deployment path this
project documents — there is no `erp-backend.service` under
`deploy/systemd/`, only the monitoring components run natively) shows
`ENVIRONMENT: production` is **hardcoded into the backend service's
`environment:` block**, not sourced from `.env.prod` and not
operator-settable. This closes the residual risk directly: there is no
deploy path in this codebase where `ENVIRONMENT` could accidentally stay
at its `"development"` default.

**`.env.prod.example` audit**: every value is either a real (non-secret)
setting or a clearly-marked `CHANGE_ME_...` placeholder; no SMTP/fiscal
credentials exist because the application has no such features (nothing
to leak); the backup encryption key is documented as public-key-only
(the matching private key is explicitly instructed to be generated
off-host). No code change needed — this phase closes as **verified**,
not **partially verified** as discovery's initial pass suggested before
this follow-up read.

## 6. `pg_hba.conf` / PostgreSQL auth requirement (Phase 6)

**Problem** (discovery item 13): no project-owned `pg_hba.conf` exists.

**Design decision, per this milestone's own explicit instruction**: do
NOT invent a distribution-specific `pg_hba.conf` file (its real location
and format differ between the Docker-image deployment path and a native
install, and a single checked-in file would be wrong for one of them).
Instead, `deploy/postgres/PG_HBA_REQUIREMENTS.md` documents the exact
behavioral requirement (`scram-sha-256` or stronger for every host
connection, never `trust`), verifies what each of this project's two
real deployment models actually does by default today (both already
correct, confirmed directly — the `postgres:16` image's own long-standing
default, and this sandbox's own native `postgresql-16` apt package,
whose real `pg_hba.conf` was read directly as evidence), and gives a
verified one-line operator checklist command
(`SELECT count(*) FROM pg_hba_file_rules WHERE auth_method IN ('trust', 'password')`
— run against this sandbox and confirmed to return `0`).

## 7. Multi-worker / rate-limiter single-process requirement (Phase 9)

**Finding**: discovery confirmed the deployment is correctly
single-worker today (no `--workers` flag anywhere, matching the rate
limiter's own already-documented single-process assumption in
`backend/app/core/rate_limit.py` and `docs/M13_DESIGN.md`), but nothing
*enforces* that it stays single-worker.

**Design decision**: do not add a runtime guard. There is currently no
environment variable or config surface that controls worker count at
all (no `WEB_CONCURRENCY`, no gunicorn) — building a check against a
knob that doesn't exist yet would be exactly the speculative
infrastructure this milestone's own instructions forbid ("do not add
speculative business features" extends naturally to speculative
production-safety features for a scaling mode this codebase doesn't
support). If multi-worker deployment is ever genuinely planned, the
correct fix is migrating the rate limiter's in-memory state to a shared
store (Redis, or the database) *before* adding a `--workers` flag — not
adding an enforcement check now for a flag nobody can set yet. This is
recorded here as the honest "single-worker is a hard requirement, not
merely a default" statement the milestone's Phase 9 instruction asks
for, satisfied by documentation (this file, plus
`M18_HARDENING_AUDIT.md`) rather than by inventing enforcement code.

## 8. Point-in-time recovery (Phase 1/14)

**Design decision**: out of scope to build. No prior milestone (M1
through M17) ever specified continuous WAL archiving as a requirement,
and this codebase's entire backup/restore design — tested, verified,
and load-bearing across M12/M13/M18 — is built around full logical
`pg_dump` snapshots. Inventing a WAL-archiving pipeline now, unprompted,
is precisely the speculative production infrastructure this milestone's
own instructions forbid. The correct action is what discovery already
did: surface this as a previously-*unacknowledged* gap (unlike the
other "intentional limitation" items, no prior doc ever stated this
limitation explicitly) and document it plainly in
`M18_HARDENING_AUDIT.md` as a production prerequisite for any deployment
whose recovery-point objective is tighter than "since the last full
backup."

## Non-goals (repo-wide, this milestone)

No new business feature, endpoint, or schema-affecting change beyond
the one migration-safety fix in §1 (which fixes a latent defect, not a
feature). No redesign of stable financial logic — none of M18's
findings implicated financial *logic*, only migration-safety and
deployment-topology gaps around it. No fabricated production-readiness
claims: every "verified" classification in `M18_DISCOVERY.md` and every
design decision above is backed by a test that was actually run, not by
a configuration file's mere existence.
