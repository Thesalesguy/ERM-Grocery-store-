# M12 — Operational Hardening & Production Readiness: Design

## 0. What this milestone is, and is not

M11 closed PASS WITH CONDITIONS: 733 backend tests, a working analytics
layer, and a hardening discipline (mutation testing, adversarial audit,
concurrency/failure injection) applied consistently since M2. What has
never been exercised, across eleven milestones, is whether the system
survives the operational events that actually happen to a running
production system: a backup and restore, a bad deploy, a crashed
process mid-transaction, a compromised credential, an oversized
request, a migration that needs to roll back. M12 answers that
question for the system **as it exists today** — it does not add a new
business capability, and per the task brief it must not become a
generic admin-UI project. The one new capability built in this
milestone (Section 10) is scoped to exactly what safe operation
requires and nothing more.

**Baseline** (Phase 0, verified before any change): branch
`claude/grocery-erp-pos-architecture-h8a53g`, commit `cc5d848`,
migration head `32e51bcda102`, backend 733/733, frontend 36/36,
lint/format/type/build clean on both.

## 1. What already exists (read before designing anything)

This section is the actual state of the system today, established via
direct inspection (`app/core/config.py`, `app/core/security.py`,
`app/core/rate_limit.py`, `app/core/logging.py`,
`app/api/v1/endpoints/health.py`, `docker-compose.yml`,
`backend/scripts/`, live `\du`/privilege queries against `erp_dev`) and
a full read of every M0-M11 design/hardening document. Nothing below is
guessed.

### 1.1 Configuration
- Single `Settings` class (`app/core/config.py`), env-var driven,
  `ENVIRONMENT` restricted to `development|staging|production`.
- `SECRET_KEY` defaults to `"dev-only-insecure-secret-key-change-me"`.
  **No runtime check stops the app from booting in `production` with
  this default, or with a `DATABASE_URL` still pointing at a
  known-placeholder password.** This is the single most important gap
  Phase 2 closes.
- `/docs`/`/redoc` already correctly gated off `is_production`.
- Refresh-token cookie already correctly gates `secure=` off
  `is_production or ENVIRONMENT == "staging"`.

### 1.2 Authentication/session (already solid — verify, don't rebuild)
- Argon2id password hashing; timing-safe login (a fixed dummy hash is
  verified against for an unknown username, so username enumeration
  isn't observable via timing).
- JWT access tokens (HS256, fixed algorithm allowlist passed to
  `jwt.decode` — already immune to algorithm-confusion), 15 min,
  permissions resolved fresh per request (a revoked role takes effect
  immediately, never waiting out token expiry).
- Refresh tokens: opaque, SHA-256-hashed at rest, rotated on every use,
  **reuse of an already-revoked token revokes every other active token
  for that user** (`REFRESH_TOKEN_REUSE_DETECTED`) — the correct
  conservative response to a suspected stolen token.
- httpOnly, `samesite=lax`, path-scoped refresh cookie; access tokens
  travel only via `Authorization: Bearer` — CSRF is structurally not
  applicable to the mutating API surface (a cross-site form can't set a
  custom header), so no CSRF token scheme is added in M12.
- Login/refresh rate-limited (10/5min, 30/5min per client IP),
  documented as in-process/per-instance only — correct and sufficient
  for the current single-instance deployment; revisit only if/when a
  second instance is ever run.
- What Phase 6 actually needs to do: **prove all of the above under
  adversarial testing** (replay, concurrent refresh, expired reuse,
  malformed JWT, cross-user substitution) with permanent tests, not
  rebuild a mechanism that is already well designed.

### 1.3 Database privilege model (established M1, unchanged since)
Two roles: `erp_user` (schema owner, migrations only,
`MIGRATIONS_DATABASE_URL`) and `erp_app` (runtime, `DATABASE_URL`).
Table ownership always bypasses GRANT/REVOKE in Postgres, which is
exactly why the app never connects as the owner.

**Verified live against `erp_dev`** (`information_schema.role_table_grants`):

| Protection class | Tables | Mechanism |
|---|---|---|
| DB-enforced append-only (no UPDATE/DELETE grant to `erp_app`) | `audit_logs`, `inventory_movements`, `journal_entries`, `journal_lines`, `payroll_reversals` | `REVOKE` in the owning migration |
| DB-enforced balance invariant | `journal_lines` | `trg_journal_lines_balance` (`AFTER INSERT OR UPDATE`, `CONSTRAINT TRIGGER`) |
| **Application-layer only** — `erp_app` has full `SELECT/INSERT/UPDATE/DELETE` | `payroll_periods`, `payroll_employee_results`, `sales`, `purchase_invoices`, `supplier_payments`, and every other business table not listed above | Service-layer state machine (status checks) + CHECK constraints only — **no DB privilege stops a compromised or buggy application process from mutating these directly** |
| Store isolation | *every* table | **100% application-layer.** `SELECT rowsecurity FROM pg_tables` returns zero rows — no Postgres Row-Level Security is configured anywhere. A raw SQL statement issued as `erp_app` (e.g. via a SQL-injection bug, were one ever introduced) is not constrained to any store. |
| Migration metadata | `alembic_version` | **Currently a real gap**: `erp_app` has full `DELETE/INSERT/SELECT/UPDATE` on `alembic_version` today. Phase 8 fixes this — the running application never needs to read or write this table. |

**This asymmetry is deliberate and must be documented honestly, never
glossed over** (Phase 8's explicit instruction): "immutable" in this
codebase means two different things depending on the table, and M12's
job is to state which is which, not to claim uniform protection that
doesn't exist. Retrofitting `REVOKE` onto `payroll_periods` etc. is
**out of scope for M12** — it would risk breaking a legitimate update
path (the same reasoning M1 §11 already gave for `sales`/
`purchase_orders`) and is exactly the kind of "reopen an earlier
milestone's design" the M11/M12 briefs both warn against. What M12
does instead: prove the *application* still protects these correctly
(state-machine tests already exist from M6/M7/M10; Phase 4 re-verifies
they survive backup/restore) and document the DB-layer boundary
honestly.

### 1.4 Backup/restore, health, observability — greenfield
- **No backup/restore tooling or documentation exists anywhere in the
  repo.** `docs/TECHNICAL_BLUEPRINT.md`'s deployment section describes
  an aspirational nightly `pg_dump`/retention/DR-runbook that was never
  built by any milestone. This is the single largest gap M12 closes.
- `/health` (liveness) and `/health/db` (readiness, `SELECT 1`) already
  exist and behave correctly (503 on DB failure, no leaked detail).
  Missing: a migration-state check.
- Structured JSON logging with sensitive-field redaction already
  exists. Missing: a request/correlation ID, so one transaction can't
  currently be traced end-to-end through the logs.
- No metrics endpoint, no tracing — out of scope to build a full
  Prometheus/Grafana stack in M12 (Section 12 explains why), but the
  correlation-ID gap is cheap and worth closing now.

### 1.5 Request/response limits — a live, not historical, gap
M2's hardening audit explicitly deferred a maximum request body size to
"the Nginx/TLS work already scheduled for Milestone M8." **That work
was never done — M8 became Advanced Inventory instead, and no later
milestone built an nginx layer or an application-level size limit.**
This is quoted in full because it is easy to mistake for closed: it is
not. Separately, every list endpoint across the app (`products`,
`sales`, `purchasing`, `inventory`, `ap`, `accounting`, `transfers`,
`replenishment` — 15+ routes) shares an identical pattern,
`limit: int = 50`, with **no upper bound** — a client can request
`limit=10000000` and the query will attempt exactly that.

### 1.6 Administrative capability gap
There is no API to deactivate a user. `users.is_active` exists and is
checked at login and token-refresh time, but the only way to set it is
direct SQL as the owning role — there is no operator-facing way to
revoke a terminated or compromised account's access without shell/DB
access. Classified in Section 10 as category A (genuinely required for
safe operation) and the one new capability this milestone adds.

## 2. Deployment architecture

M12 does not stand up a new production topology from scratch — that
would be inventing infrastructure the project has never run, is not
tested, and can't be honestly verified in this milestone. Instead:

- **Documented, minimal production shape**: single VPS, Docker Compose,
  Postgres in its own container with a named volume, the existing
  `backend`/`frontend` images, **plus** an nginx reverse-proxy service
  added in front (TLS termination via Let's Encrypt/Certbot,
  `client_max_body_size` as the outer request-size backstop). This
  matches the low-cost/single-instance shape every other design
  decision in this codebase (in-process rate limiting, no RLS, no
  distributed anything) already assumes — introducing Kubernetes,
  managed load balancers, or a multi-region topology here would
  contradict that established shape for no tested benefit.
- A `docker-compose.prod.yml` overlay is written (Section on
  Configuration) rather than modifying the dev compose file, so the two
  never drift into each other by accident.
- **Not built in M12**: the nginx container itself, since standing up
  and TLS-testing a reverse proxy end-to-end is a distinct, sizable
  piece of infrastructure work with its own testing burden (real
  domain, real certificate) that doesn't fit this milestone's
  proof-of-safety focus. What IS built: the application-level
  request-size limit that has to exist regardless of whether a proxy
  sits in front of it (defense in depth — Phase 7), and the documented
  nginx config snippet an operator adds when they do stand up TLS
  (Section 8 of the hardening audit).

## 3. Configuration/secrets

Extend `Settings` with a fail-closed validator, not a new mechanism:

```python
_KNOWN_INSECURE_DEFAULTS = {
    "dev-only-insecure-secret-key-change-me",
}

@model_validator(mode="after")
def _forbid_insecure_production_config(self) -> "Settings":
    if self.is_production:
        if self.SECRET_KEY in _KNOWN_INSECURE_DEFAULTS or len(self.SECRET_KEY) < 32:
            raise ValueError(
                "SECRET_KEY must be overridden with a long random value "
                "before starting in production"
            )
        if "CHANGE_ME_IN_PRODUCTION" in self.DATABASE_URL:
            raise ValueError(
                "DATABASE_URL still contains the bootstrap placeholder "
                "password; rotate it before starting in production"
            )
    return self
```

This raises at `Settings()` construction time — i.e. at import/startup,
before the app can serve a single request — which is what "fail
closed" means here: a misconfigured production deploy crashes loudly
on boot instead of running insecurely. Tested by constructing `Settings`
directly with `ENVIRONMENT=production` and each unsafe value, asserting
`ValidationError`, and confirming `development`/`staging` with the same
values do NOT raise (the defaults must stay usable for local dev).

No secret is ever logged: the existing `RedactSensitiveFieldsFilter`
already strips `secret`/`secret_key`/`token`/etc. keys from `extra=`;
Phase 2 adds a test that actually triggers a logged exception containing
a `Settings` object's repr and confirms `SECRET_KEY`'s value is absent
from the captured log output, not just trust the filter's key-matching
logic that runs on other tests.

## 4. Database backup/restore

**Tooling**: `pg_dump`/`pg_restore` in the custom (`-Fc`) format —
standard PostgreSQL tooling already installed in this environment (the
same image the whole project already runs), zero new dependency, zero
external service.

[pg_dump / pg_restore (PostgreSQL core tools)]
- Access Mode: Guest Allowed (no account of any kind — it's a local binary)
- Onboarding Friction: None
- Free Tier Caps: N/A — not a hosted service
- Next Tier Cost: N/A
- Local/Open-Source Alternative: N/A — this already is the local open-source tool; no alternative needed

**Procedure** (`backend/scripts/backup_database.sh` /
`restore_database.sh`, both new, documented in
`docs/M12_HARDENING_AUDIT.md` Section on backup/restore):

- Backup: `pg_dump -Fc --no-owner --no-privileges` against
  `MIGRATIONS_DATABASE_URL` (the owning role, so the dump captures
  every table regardless of `erp_app`'s restricted grants),
  timestamped filename, written to a local, operator-chosen directory
  (no cloud upload in M12 — see Known Limitations).
- Restore: `pg_restore -Fc --clean --if-exists` into a target database,
  **then** `bootstrap_db_roles.sql` (idempotent) if restoring into a
  cluster that doesn't already have `erp_app`, **then**
  `alembic upgrade head` if the dump predates the current schema.
  `--no-owner --no-privileges` on the dump side means restore never
  tries to recreate roles or replay stale GRANT/REVOKE statements that
  could conflict with a freshly-bootstrapped `erp_app` — the current
  migration chain's own GRANT/REVOKE statements are the single source
  of truth for privileges, replayed by `alembic upgrade`, never by the
  dump.
- **Actually executed in Phase 3**, against a populated database built
  for this purpose (not `erp_dev`, to avoid disturbing other work) —
  see Phase 3 below. This is not a paper procedure.

## 5. Migration operations

No new pattern — M12 reuses the guard style established at M6 and used
identically through M10 (a `DO $$ ... RAISE EXCEPTION` block as the
first statement of `downgrade()`, checked against real populated data
before any destructive DDL runs). Any M12 migration (the
`alembic_version` privilege fix, Section 1.3) that changes a GRANT is
non-destructive to data by construction — revoking a privilege can
never lose a row — so no downgrade guard is needed for it, matching the
same reasoning the M11 index migration used.

## 6. Health checks

Extend `app/api/v1/endpoints/health.py` with a third route:

- `GET /health/migration` — compares the DB's current
  `alembic_version` row against this deployed code's own migration
  head (read from the local `alembic/versions/` history at import
  time, not a network call), returns 503 with `{"status": "error",
  "reason": "schema_mismatch"}` on mismatch. This is exactly Phase 14
  scenario I (a stale app instance against a newer/older schema) made
  observable rather than a silent, confusing 500 on the first query
  that touches a changed column.
- Neither existing nor new health routes ever include a stack trace,
  connection string, or credential — verified by a test that forces
  each failure path and asserts on the exact response body.

## 7. Observability / structured logging

Add one thing: a per-request correlation ID. Starlette middleware
generates a UUID4 (or reads an inbound `X-Request-ID` if a future proxy
sets one), binds it into the logging context for the duration of the
request (via `contextvars`, the standard way to thread a value through
async call stacks without passing it explicitly everywhere), and
returns it in the response's `X-Request-ID` header. Combined with the
existing audit log (which already records `user_id`, `action`,
`entity_type/id`, timestamp) and each service function's own log lines,
an operator can now grep one request ID across app logs and cross-
reference the timestamp against the audit table — closing the
"correlate a transaction across request/service/DB/audit" gap without
standing up a tracing backend.

## 8. Auditability

Phase 9 audits the *existing* audit system rather than redesigning it —
M2 through M11 already log login, sale, return, void, receipt, AP
posting/payment, payroll calculate/approve/post, and journal reversal
events, and M10 already made a deliberate, documented choice to exclude
payroll dollar figures from audit entries. What Phase 9 verifies with
new permanent tests: failed-login is actually recorded (not just
successful login), no audit entry anywhere contains a password/token/
raw payment credential, and — reusing the Section 1.3 privilege table —
`erp_app` genuinely cannot UPDATE or DELETE an `audit_logs` row (already
true; proven with a live adversarial SQL attempt, not just cited from
the grant table).

## 9. Authentication/session management, rate limiting, authorization

Already designed and largely correct (Section 1.2). M12's job here is
adversarial testing (Phase 6/18/19), not redesign — see those sections
for the specific attack list. The one enhancement: extending session
revocation to cover the new user-deactivation capability (Section 10)
— deactivating a user must also revoke every refresh token they hold,
reusing the existing `_revoke_all_refresh_tokens_for_user` the reuse-
detection path already calls, so a deactivated account can't keep
refreshing its way to new access tokens for up to 14 days.

## 10. Database privilege model changes

The only privilege change in M12: **revoke `erp_app`'s
DELETE/INSERT/UPDATE on `alembic_version`**, leaving it with no access
at all (the application never legitimately reads or writes this table
— only Alembic, running as `erp_user`, touches it). This is the one
concrete DB-privilege fix this milestone makes; everything else in
Section 1.3's table is either already correct or explicitly deferred
with a stated reason.

## 11. Data retention

No new retention *policy* is invented (that's a business decision, not
an engineering one, and no stakeholder requirement for one exists in
any prior milestone doc). What M12 documents: backups are retained
locally, operator-managed, with a suggested (not enforced) rotation
documented in the hardening audit; no application data is ever deleted
by this milestone (nothing in M12 adds a deletion path anywhere).

## 12. Operational recovery / disaster scenarios

Nine scenarios (Phase 14), each with expected behavior, recovery
procedure, and whether manual intervention is required — documented in
full in `docs/M12_HARDENING_AUDIT.md` (not duplicated here) because
several of them (process termination during mutation, migration
interrupted before completion) are proven by tests written in Phases
4/5/12/13, and the design belongs next to the evidence. The short
version: this system has **no zero-downtime deployment story today**
(a single Compose service restarting means a brief outage,
acknowledged rather than claimed away) and recovery from most disaster
scenarios already relies on properties Postgres itself guarantees
(transactional DDL, atomic commit) rather than anything M12 invents.

## 13. Security headers

FastAPI/Starlette add no security headers by default. M12 adds a small
middleware setting `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, and `Referrer-Policy: strict-origin-when-cross-origin`
on every response — standard, zero-config, zero-dependency headers that
cost nothing and close a real (if minor) gap. `Content-Security-Policy`
is deliberately NOT added in M12: the API serves JSON, not HTML, to
almost every route except `/docs`/`/redoc` (already disabled in
production), so a CSP policy would be untested, unused surface area for
the one environment where it would matter least.

## 14. Request limits

- Application-level max request body size: Starlette middleware
  rejecting any request whose `Content-Length` exceeds a configured
  cap (default 2MB — comfortably above the largest legitimate payload
  in this app, a multi-line sale/purchase-order submission) with a
  clean 413, before the body is ever read into memory. This is the
  application-layer half of the M2-deferred gap; the proxy-layer half
  (`client_max_body_size`) is documented as the complementary
  production control for whenever nginx is actually stood up (Section
  2), not simulated here.
- Every `limit: int = 50` list-endpoint parameter across all 15+
  routes gets an explicit upper bound (`Query(50, ge=1, le=500)`),
  closing the unbounded-result-set gap identified in Section 1.5 with
  FastAPI's own validation (a clean 422, not a 500 from an
  out-of-memory query).
- Report date ranges (M11's `sales/trend`, etc.) already handle a
  multi-century range without erroring (M11 adversarial audit); no
  further cap is added there since the underlying aggregation is
  already proven bounded in query count regardless of range width.

## 15. Background jobs / scheduled operations

None exist today, and M12 does not add any (no Celery/cron worker in
this codebase). Backups (Section 4) are documented as an
operator-triggered `cron` entry running the new shell script — `cron`
is the local, zero-dependency, already-present-on-any-Linux-host
scheduler, not a new service.

## 16. Environment separation

`development | staging | production` already exists as a single enum
on `Settings`. M12 makes the separation actually matter (Section 3's
fail-closed validator) rather than adding a fourth environment or a
different mechanism. A `docker-compose.prod.yml` overlay documents the
production-shaped overrides (no bind mounts, no `--reload`, explicit
`ENVIRONMENT=production`) without touching the existing dev compose
file.

## 17. Monitoring/alerting

Not built as running infrastructure in M12 — the honest reason is the
same one Section 2 gives for not building nginx: standing up and
verifying a monitoring stack end-to-end is its own substantial body of
work, and an untested monitoring config is worse than an honestly
absent one. Documented recommendation for a future milestone:

[Prometheus + Grafana (self-hosted)]
- Access Mode: Guest Allowed (both are plain open-source binaries/containers; no account of any kind)
- Onboarding Friction: None
- Free Tier Caps: N/A — self-hosted, no usage caps
- Next Tier Cost: N/A
- Local/Open-Source Alternative: N/A — already the local/open-source choice

[UptimeRobot (external uptime pinger, considered and rejected for M12)]
- Access Mode: Account Needed
- Onboarding Friction: Email Verification
- Free Tier Caps: 50 monitors, 5-minute check interval
- Next Tier Cost: $7-plus/month for 1-minute intervals
- Local/Open-Source Alternative: a `cron`-driven `curl` against `/health` piped to a local log or `mail`, which is what M12 documents as the zero-cost interim option instead of signing up for anything.

What M12 ships instead, that a future monitoring stack would consume:
the `/health`, `/health/db`, `/health/migration` endpoints (Section 6)
and structured logs with correlation IDs (Section 7) — the
instrumentation monitoring needs, without the monitoring service
itself.

## 18. Test strategy

Mirrors the discipline established since M9/M10/M11: permanent,
executable tests for every claim, real adversarial mutation of
protections to prove tests actually detect their removal, and testing
sessions A-P (Phase 17) as real files, not narrative. The one
departure from prior milestones' pure-`pytest` style: Phase 3's
backup/restore test necessarily shells out to real `pg_dump`/
`pg_restore` binaries against a real, separate database — this is
still a `pytest` test (subprocess calls, assertions on the result), not
a manual exercise, but it is slower and more infrastructure-dependent
than the rest of the suite, so it lives in its own file
(`tests/test_backup_restore.py`) that CI can still run since the same
Postgres image/tools are already present there.

## 19. Rollback strategy

For code: revert the commit (this branch has no auto-deploy, so
"rollback" is a `git revert` plus a redeploy, not a special mechanism).
For schema: `alembic downgrade` one step, guarded per Section 5 — but
Section 1.3 already establishes that most of this codebase's
migrations (everything since M6) refuse a downgrade that would lose
real data, so "rollback" for a migration that has already run against
production data is, correctly, sometimes "roll forward with a fix" not
"roll back" — documented explicitly in the hardening audit rather than
implying every migration is always cleanly reversible.

## 20. Known limitations stated up front (mirrors M11's own discipline)

1. No nginx/TLS/reverse-proxy layer is stood up in M12 — documented,
   config-snippet-ready, not executed end-to-end.
2. Rate limiting remains in-process/single-instance (M2's own
   documented limitation, unchanged and still correct for the current
   deployment shape).
3. `payroll_periods`/`payroll_employee_results`/`sales`/etc. remain
   application-layer-only immutable, not DB-privilege-enforced — a
   deliberate, inherited M1 decision, not an M12 oversight.
4. Store isolation remains 100% application-layer (no RLS) — same
   reasoning.
5. Backups are local-filesystem only in M12; off-box/encrypted backup
   transport is documented as a follow-up, not built (no cloud service
   invented to fill this gap, per this milestone's own constraint).
6. No monitoring/alerting stack is deployed; only the instrumentation
   it would consume.
7. No admin UI is built beyond the one user-deactivation capability
   (Section 1.6) — explicitly scoped that narrowly per the task brief.
