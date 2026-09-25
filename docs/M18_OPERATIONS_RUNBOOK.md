# M18 Operations Runbook

Every command below was either read directly from a script this
repository actually ships, or run and confirmed during this milestone
(see `docs/M18_TESTING_SESSIONS.md` for the exact evidence). Nothing
here is aspirational. Where a procedure has a known limitation, it is
stated, not hidden. This runbook complements, not replaces,
`docs/M13_PRODUCTION_RUNBOOKS.md` (16 scenarios: app/DB unavailable,
disk full, backup failure, restore, migration failure, auth incident,
duplicate transaction, accounting/inventory/AP/payroll reconciliation,
cross-store access, expired TLS cert, high error rate, monitoring-
component-down) — read that first for incident response; this document
adds deployment, credential rotation, and the production prerequisites
this milestone's discovery surfaced.

## 1. Deployment

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.prod up -d --build db backend nginx
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.prod run --rm frontend-builder
```

Or, the full seven-step procedure (backup → pre-flight health check →
migration → app deployment → smoke tests → verification → rollback
guidance on any failure): `bash deploy/scripts/deploy.sh`. Verified this
milestone via `deploy/tests/test_deploy_procedure.py` (now running in
CI, not just locally) — a real subprocess run against a real backend
process and real databases, both the success path and a migration-
failure path.

`backend/Dockerfile` now has a `HEALTHCHECK` against `GET /health`, and
`docker-compose.prod.yml`'s nginx service waits for it
(`condition: service_healthy`) before starting — added this milestone;
previously nginx could start proxying before the backend was ready.

## 2. Rollback

- **Migration step failed**: the database is unchanged (Alembic's
  transactional DDL) — no rollback needed, fix the migration and
  redeploy.
- **A step after a successful migration failed**: the schema is already
  at the new head. This codebase does not maintain N-1 schema
  compatibility (`docs/M13_DESIGN.md` §11) — redeploying the *previous
  application version* against the new schema is **not safe in
  general**. Two real options:
  - Fix forward: resolve the application problem, redeploy the new
    version (the schema is already correct for it).
  - Full rollback: `cd backend && alembic downgrade <previous_revision>`
    **and** deploy the exact previous application version together,
    never one without the other.
- `deploy/scripts/deploy.sh` prints this exact guidance automatically on
  any failure — it never attempts either option for you.

## 3. Backup

```
bash backend/scripts/backup_database.sh <output_dir>
```

Real `pg_dump -Fc` full dump, `chmod 600`'d. Verified this milestone: a
205 MB representative database backed up in 1.53s, producing an 11 MB
compressed dump (`M18_TESTING_SESSIONS.md` Session K).

Off-box (encrypted, checksummed, retention-managed):

```
BACKUP_AGE_PUBLIC_KEY=<age1...> BACKUP_RCLONE_REMOTE=<remote> \
  bash deploy/scripts/backup_offbox.sh
```

Local backup-directory retention is operator-managed (no automatic
rotation) — off-box retention is automatic
(`rclone delete ... --min-age <days>d`, tested).

## 4. Restore

```
bash backend/scripts/restore_database.sh <dump_file> <target_db_name>
```

**Destructive** — drops and recreates `<target_db_name>`; requires
typing the database name to confirm. Never run against a database
anyone still needs. Verified this milestone: a full backup→restore
cycle against representative data completed in 3.21s
(`M18_TESTING_SESSIONS.md` Session K). If the target cluster is
genuinely new, run `backend/scripts/bootstrap_db_roles.sql` *first* (the
dump's GRANT/REVOKE statements need `erp_app` to already exist).

After restoring, **verify before trusting it**:

```
python -m scripts.integrity_snapshot postgresql+psycopg://erp_user:<password>@localhost:5432/<target_db>
```

and compare against a pre-backup snapshot, or (stronger, this
milestone's own method) run the actual reconciliation functions
(`inventory_reconciliation`, `ap_reconciliation`,
`purchase_clearing_reconciliation`, `in_transit_reconciliation`,
`payroll_gl_reconciliation`) against both the source and the restored
database and confirm identical output.

Off-box restore: `bash deploy/scripts/restore_offbox.sh` — refuses to
proceed if the decrypted checksum doesn't match the manifest (tested,
`deploy/tests/test_backup_offbox.py`).

## 5. Migration

```
cd backend && alembic upgrade head
```

Verify: `alembic heads` must show exactly one head. `GET /health/migration`
independently confirms the deployed code's migration head matches the
database's applied version (`backend/app/api/v1/endpoints/health.py`).

**Downgrade safety** (M18 finding): every accounting-adjacent migration
now refuses (`RAISE EXCEPTION`) rather than silently destroying data if
real posted transactions exist — including `8df037a45976` (M4
accounting core), fixed this milestone. `alembic downgrade <revision>`
against a database with real financial activity in a table a target
migration would drop or narrow will fail loudly with an explanatory
message rather than corrupt or lose history.

## 6. Health verification

```
curl -f http://<host>:8000/health          # liveness only
curl -f http://<host>:8000/health/db        # DB connectivity (503 if unreachable)
curl -f http://<host>:8000/health/migration # schema-vs-code drift (503 if mismatched)
```

All three are real checks, not stubs — `/health/db` runs `SELECT 1`;
`/health/migration` re-derives the code's migration head from the
deployed `alembic/versions/` directory and compares it against the
database's actual `alembic_version` row.

## 7. Log inspection

- Application logs: structured JSON to stdout
  (`backend/app/core/logging.py`), every line stamped with a
  `request_id` matching the `X-Request-ID` response header —
  correlate a specific user report to its exact log lines by that ID.
  Sensitive fields (`password`, `secret`, `token`, `authorization`,
  `api_key`) are redacted before logging.
- nginx access log: JSON format, includes the same `X-Request-ID`.
- PostgreSQL connection/disconnection log:
  `/var/log/postgresql/postgresql-16-main.log` (native install) —
  independent of this application's own `audit_logs` table, so it
  cannot be suppressed by a compromised `erp_app` credential.
- Audit trail (business events — logins, financial postings, etc.):
  `GET /api/v1/audit-logs` (see `docs/M13_PRODUCTION_RUNBOOKS.md` for
  the read endpoint's authorization model). Append-only at the database
  level (`UPDATE`/`DELETE` revoked from `erp_app`) — grows unbounded by
  deliberate design (`docs/M12_DESIGN.md`), so long-term storage
  planning for this table is a real, currently-unaddressed production
  prerequisite (see §9).

## 8. Incident response

See `docs/M13_PRODUCTION_RUNBOOKS.md` for the full 16-scenario
playbook. This milestone's own failure-injection matrix
(`M18_TESTING_SESSIONS.md` Session J) confirms all 12 required
production-readiness failure scenarios have a real, CI-verified test
behind them — consult that table to find the exact test proving the
expected behavior for a given failure type before assuming it's
untested.

## 9. Database recovery (disaster recovery)

Full procedure, timed and verified this milestone against representative
data (`M18_TESTING_SESSIONS.md` Session K — explicitly a **local
simulation**: same host, same Postgres cluster; genuine off-box/
physical-host recovery is a separate, already-tested concern, §3/§4
above):

1. `bash backend/scripts/backup_database.sh <dir>` — real dump.
2. `bash backend/scripts/restore_database.sh <dump> <target_db>` — real
   restore into a fresh/recovery target.
3. Verify: `alembic_version` matches; row counts match; reconciliation
   function output matches between source and restored copy (§4).
4. If recovering the *actual* production database (not a drill),
   restart the application pointed at the recovered database and run
   the same health checks as §6.

**Production prerequisite**: this codebase supports only full-snapshot
recovery (recovery point = time of last backup). No point-in-time
recovery (WAL archiving) exists — build this before deploying anywhere
whose recovery-point objective is tighter than "since the last full
backup" (`M18_HARDENING_AUDIT.md` §3).

## 10. Credential rotation

Not covered by any prior runbook — closed this milestone.

- **`erp_app` (application runtime role) password**: rotate via
  `ALTER ROLE erp_app WITH PASSWORD '<new>';` as a role with sufficient
  privilege, then update `DATABASE_URL` in `.env.prod` and redeploy the
  `backend` service (`docker compose ... up -d --build backend`). No
  migration or schema change involved — this is a cluster-level role,
  not application data.
- **`erp_user` (migration/owner role) password**: same mechanism,
  update `MIGRATIONS_DATABASE_URL` and `POSTGRES_PASSWORD` in
  `.env.prod`. Rotate independently from `erp_app` — the two must never
  share a password (`backend/scripts/bootstrap_db_roles.sql`'s own
  design rationale: ownership bypasses ACLs entirely, so the two roles'
  privilege separation only means something if their credentials are
  genuinely separate).
- **`SECRET_KEY` (JWT signing)**: rotating this invalidates every
  currently-issued access/refresh token immediately — every logged-in
  session is forced to re-authenticate. Update in `.env.prod`, redeploy
  `backend`. `backend/app/core/config.py`'s fail-closed validator
  refuses to boot in production with a short or known-insecure value,
  so a rotation to an insufficiently random value is caught at startup,
  not silently accepted.
- **`BACKUP_AGE_PUBLIC_KEY`**: generate a new keypair with
  `age-keygen` **off-host** (the private key must never live on the
  database host it protects — `deploy/scripts/restore_offbox.sh`'s own
  design rationale). Update `.env.prod`'s public key; every *future*
  backup uses the new key. Existing backups remain decryptable only
  with the *old* private key — retain it until every backup encrypted
  under it has aged out of retention.
- **`erp_monitor` (postgres_exporter's read-only role)**: least-
  privilege by design (`pg_monitor` built-in role only) — rotate the
  same way as `erp_app`, update the exporter's own connection string.

## 11. Confirmed production prerequisites (consolidated)

See `docs/M18_HARDENING_AUDIT.md` §6 for the full list with evidence.
Summary: (1) PITR/WAL archiving, (2) a real off-box `rclone` remote
pointing at genuinely separate storage, (3) an Alertmanager external
notification receiver, (4) an audit-log/data-volume capacity plan, (5)
confirming the `pg_hba.conf` requirement against whatever the actual
production Postgres instance turns out to be, (6) re-tuning the
conservative starting compose resource limits for real production scale.
