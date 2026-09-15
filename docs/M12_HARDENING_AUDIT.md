# M12 Hardening Audit — Operational Readiness

**Verdict: PASS WITH CONDITIONS**

M11 closed PASS WITH CONDITIONS at commit `cc5d848`, migration head
`32e51bcda102`, backend 733/733, frontend 36/36. M12 is an operational
hardening and production-readiness milestone — it adds **no new
business module** and exactly **one** new administrative capability
(user deactivation, Section 1.6/Section 7 below), scoped narrowly per
the task's own "do not turn this into a generic admin-UI project"
instruction. Everything else in this document is either a real defect
found and fixed, a permanent adversarial test proving an existing
protection holds, or an honest, explicit limitation.

This document distinguishes four categories throughout, per the task's
explicit instruction never to blur them:

- **Tested capability** — an executable test in this repository proves
  it, run against a real PostgreSQL database, not mocked.
- **Documented procedure** — a script/runbook exists and was run at
  least once by hand in this sandbox, but is not exercised by the
  automated suite on every run.
- **Assumption** — believed true, consistent with the design, but not
  verified in this environment (usually because the sandbox cannot
  provide the real infrastructure involved — a reverse proxy, a second
  physical host, real production traffic volume).
- **Untested operational dependency** — something a real deployment
  needs that this milestone deliberately did not build or verify (with
  the reason stated).

## 1. Result summary

| Metric | M11 close | M12 close |
|---|---|---|
| Commit | `cc5d848` | `41cf728` |
| Migration head | `32e51bcda102` | `17fb9afe8d39` |
| Backend tests | 733/733 | **805/805** |
| Frontend tests | 36/36 | 36/36 (unchanged — M12 is backend-only) |
| Backend lint/format/type | clean | clean |
| Frontend lint/build | clean | clean |
| New backend test files | — | 12 |
| Real defects found and fixed | — | **2** |
| PR opened | no | no (per task instruction) |
| M13 started | no | no |

72 net new backend tests across 12 new files (805 vs 733), plus one
existing test in `tests/test_migrations.py` extended and one in
`tests/test_auth_hardening.py` strengthened after a mutation-testing
miss (Section 12). 3,528 lines added, 50 removed, across 34 backend
files plus `docs/M12_DESIGN.md` (466 lines) and this document.

## 2. Defects found and fixed

Two real, previously-undetected defects were found by this milestone's
adversarial testing — both fixed immediately, per the task's explicit
"must be fixed before completion" instruction for anything touching
authentication or migration safety.

### 2.1 Concurrent refresh-token race defeated reuse-detection (auth)

**Found**: Phase 6 (`tests/test_auth_hardening.py`). Two threads racing
to refresh the SAME not-yet-revoked refresh token both succeeded —
14/15 real-thread trials produced two valid sessions from one
single-use token, with reuse-detection never firing, because neither
request's `SELECT` observed the other's `UPDATE` before either
committed.

**Impact**: an attacker in possession of a stolen refresh token who
raced the legitimate client's own refresh had a ~93% chance (measured)
of walking away with a live session instead of tripping the
compromise-detection path that should have killed every session for
that user.

**Fix**: `app/modules/auth/service.py::refresh_access_token` now takes
`SELECT ... FOR UPDATE` on the token row, forcing the loser of the race
to block until the winner commits, then correctly observe the row as
already-revoked and take the existing reuse-detected branch (revoke
every session, audit-log `REFRESH_TOKEN_REUSE_DETECTED`).

**Proof**: `tests/test_auth_hardening.py::test_concurrent_refresh_with_same_token_never_yields_two_sessions`
reproduces the real race with independent DB connections/threads across
12 trials. Re-run against the pre-fix code during Phase 19 mutation
testing: the mutation was caught on the first trial. (The single-trial
version of this test, written in Phase 6, was itself found by Phase 19
to be an unreliable detector — see Section 12.)

### 2.2 `erp_app` had unrestricted DML on `alembic_version` (DB privilege)

**Found**: Phase 4/8 (`tests/test_backup_restore.py`). An adversarial
`DELETE FROM alembic_version` as the application's runtime role
(`erp_app`) succeeded — the table had never been explicitly REVOKEd,
unlike every other ledger table (`journal_entries`, `audit_logs`,
`inventory_movements`), so it inherited the broad default grant every
new table gets.

**Impact**: a SQL-injection or compromised-application-credential
scenario could let an attacker corrupt Alembic's own bookkeeping of
which migrations are applied — a stealthy way to desynchronize the
deployed code from what the database believes its schema state is.

**Fix**: migration `4708fb75ace5` revokes ALL privileges on
`alembic_version` from `erp_app`. A second migration, `17fb9afe8d39`
(Phase 11), then grants back **SELECT only** — a deliberate, narrow
refinement, not a reversal: the new `GET /health/migration` endpoint
needs to read the applied version to do its job, and SELECT carries no
write risk. INSERT/UPDATE/DELETE remain revoked.

**Proof**: `tests/test_migrations.py::test_m12_alembic_version_privilege_revoked_on_upgrade_and_restored_on_downgrade`
cycles through M11 head → Phase 8 revision → M12 head → back down,
asserting the exact grant set at each step against
`information_schema.role_table_grants`.
`tests/test_health_and_observability.py::test_erp_app_can_read_but_not_write_alembic_version`
proves SELECT works and UPDATE is still rejected.

## 3. Backup and restore — tested capability

A real `pg_dump -Fc` / `pg_restore --clean --if-exists` cycle against a
realistic, freshly-seeded database (`erp_backup_test`, created and
destroyed by the test itself, never touching `erp_dev`/`erp_test`):
products, sales, purchase orders/receipts, an AP invoice with a
supplier credit note allocation, a full stock-count lifecycle, and the
resulting accounting/inventory state. `tests/test_backup_restore.py`
verifies, after restore:

- Every table's row count matches exactly.
- `docs/…`'s "no second accounting truth" discipline extended to
  recovery: `scripts/integrity_snapshot.py` (Phase 15) — a dataclass of
  30 fields spanning accounting/inventory/AP/payroll/sales, built by
  **reusing** the existing trial-balance/P&L/reconciliation service
  functions, never re-deriving the arithmetic — produces an
  identical snapshot before and after the backup/restore cycle.
- GRANT/REVOKE state round-trips exactly (the dump deliberately omits
  `--no-owner`/`--no-privileges`, unlike the original design sketch —
  see Section 13).
- `erp_app` still cannot DELETE/UPDATE `journal_lines`, `audit_logs`,
  `inventory_movements`, `journal_entries` after restore (adversarial
  proof, not just "the GRANT statement ran").
- A journal entry cannot be double-reversed post-restore; a duplicate
  journal number is rejected post-restore.
- A fresh stock adjustment against restored data produces correct
  inventory movements and GL postings (Phase 5) — recovery isn't just
  "the data looks right," it's "the system keeps working correctly on
  top of the restored data."

**Documented procedure**: `scripts/backup_database.sh` /
`scripts/restore_database.sh` are the operator-facing scripts the test
exercises the same mechanism as, but the scripts themselves are not
run by the automated suite (they wrap the same `pg_dump`/`pg_restore`
commands, with interactive confirmation on restore).

**Untested operational dependency**: off-box/encrypted backup
transport, and any backup rotation/retention policy — both explicitly
out of scope (Section 20 below), no cloud service invented to fill the
gap.

## 4. Configuration and secrets — tested capability

`app/core/config.py::Settings._forbid_insecure_production_config`
(a `model_validator`) makes `ENVIRONMENT=production` with a dev-shaped
`SECRET_KEY` (known-insecure value or under 32 characters), a
`CHANGE_ME_IN_PRODUCTION` placeholder in `DATABASE_URL`/
`MIGRATIONS_DATABASE_URL`, or a wildcard CORS origin **crash at
Settings() construction** — before the app can serve a single request.
`development`/`staging` are deliberately exempt. `tests/test_config_safety.py`
(10 tests) proves each rejection individually, the positive "safe
config accepted" case, environment exemption, and that the secret value
never leaks into the exception message.

## 5. Authentication and session hardening — tested capability

`tests/test_auth_hardening.py` (23 tests) attacks the existing,
already-sound mechanism (argon2id hashing, JWT with a fixed algorithm
allowlist, opaque hashed rotating refresh tokens with reuse-detection,
httpOnly/SameSite=Lax refresh cookies, access token never in a cookie):
credential stuffing across many usernames from one IP, refresh-token
replay after logout, **concurrent refresh** (Section 2.1's real find),
expired access/refresh tokens, malformed bearer tokens, JWT algorithm
confusion (`alg: none`), a token signed with the wrong secret, a token
typed `refresh` presented as `access`, missing/non-numeric/nonexistent
`sub` claims, immediate session-kill on user deactivation (both the
live access token and the refresh token), cookie flags, and that a
refresh always mints an access token for the token's own owner, never
a client-influenced identity.

## 6. API security hardening — tested capability

`app/core/http_hardening.py::MaxBodySizeMiddleware` rejects any request
whose `Content-Length` exceeds `settings.MAX_REQUEST_BODY_BYTES` (2MB)
with a 413 before the body is read — the application-layer half of the
M2-deferred request-size gap. Every one of the 15 `limit: int = 50`
list-endpoint parameters across products/sales/purchasing/inventory/
transfers/ap/replenishment/accounting now has an explicit
`Query(50, ge=1, le=500)` bound. `tests/test_api_security_hardening.py`
(7 tests) proves both, plus the security-headers middleware
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`).

**Assumption**: report date ranges remain deliberately uncapped — M11's
adversarial audit already proved a multi-century range doesn't error,
and "view all time" is a legitimate analytics use case, not a gap.

**Untested operational dependency**: the proxy-layer request-size
backstop (`client_max_body_size`) — documented for whenever nginx is
actually stood up (Section 13).

## 7. Operational administration — tested capability

The one new capability: `POST /api/v1/auth/users/{id}/deactivate`
(`users.manage` permission, already granted to Admin in the existing
seed matrix — dormant until now). Sets `is_active = False` and
immediately revokes every refresh token the account holds, so a live
access token dies on its very next `get_current_user` check, not merely
on the next login attempt. Self-deactivation is refused (409) — no
reactivation capability exists in this milestone, so a lone admin
locking themselves out would have no way back in through the API.
`tests/test_user_deactivation.py` (9 tests). No user creation, no
reactivation, no listing endpoint — deliberately narrow, per the task's
"only implement A" instruction.

## 8. Database privilege audit — tested capability

Section 2.2's fix is the one concrete privilege change. Everything else
in the privilege model was already correct (DB-privilege-layer
immutability on `journal_entries`/`journal_lines`/`audit_logs`/
`inventory_movements`/`payroll_reversals`; application-layer-only
immutability elsewhere, by deliberate inherited M1 design, not an M12
gap) and is proven by `tests/test_constraints.py` (pre-M12, still
passing) plus the adversarial checks embedded in
`tests/test_backup_restore.py` and `tests/test_migrations.py`.

## 9. Audit log and security events — tested capability

`tests/test_audit_hardening.py` (5 tests) closes the gaps M2–M11 left:
`SALE_COMPLETED`/`VOID_COMPLETED` — the highest-volume, money-moving
actions in the system — had never been asserted as audit events;
`LOGIN_SUCCESS` was exercised everywhere but never itself asserted.
Also a cross-cutting invariant: every `audit_logs` row belonging to a
test user, after a realistic mix of successful/failed logins, is
scanned and proven never to contain a distinctive marker password, the
real test password, or an argon2 hash prefix — a regression test
against any future call site anywhere in the codebase that might
naively log a whole request payload. DB-level immutability of
`audit_logs` (`erp_app` cannot UPDATE/DELETE) was already covered
pre-M12 and is not repeated.

## 10. Health checks and observability — tested capability

`GET /health/migration` compares the DB's applied `alembic_version`
against this deployed code's own migration head (read from
`alembic/versions/` at import time, not a network call), returning a
clean 503 `{"reason": "schema_mismatch"}` instead of a confusing 500 —
making disaster scenario I (Section 11) observable.
`tests/test_health_and_observability.py` (10 tests) plus
`tests/test_migration_operations_audit.py` (1 test, against a real
database genuinely one revision behind, not a mock) prove it. A
per-request correlation ID (`app/core/correlation.py`) is generated (or
reused from a bounded inbound `X-Request-ID`), threaded through a
`contextvars.ContextVar`, stamped on every log record via a new
`logging.Filter`, and echoed in the response header.

## 11. Disaster/recovery scenarios

Mapped to evidence in `tests/test_disaster_recovery_scenarios.py`'s
module docstring; reproduced here:

| # | Scenario | Status | Evidence |
|---|---|---|---|
| A | Full restore from backup | Tested | `test_backup_restore.py` |
| B | Accounting/inventory integrity survives restore | Tested | `test_backup_restore.py` Phases 4/5 |
| C | Crashed process mid-transaction leaves no partial state | Tested | `test_m8_failure_injection.py`, `test_ap_failure_injection.py`, `test_sales_returns_failure_injection.py`, `test_payroll_hardening.py`, `test_sale_finalization_failure_injection.py` |
| D | Compromised credential revoked mid-session | Tested | `test_user_deactivation.py`, `test_auth_hardening.py` |
| E | Concurrent replay of a single-use credential treated as compromise | Tested | `test_auth_hardening.py` (Section 2.1) |
| F | Stale app instance against mismatched schema is observable | Tested | `test_health_and_observability.py`, `test_migration_operations_audit.py` |
| G | Runtime role cannot tamper with financial/audit/migration ledgers | Tested | `test_constraints.py`, `test_backup_restore.py`, `test_migrations.py` |
| H | Network retry/double-submit never double-books | Tested | `test_idempotency.py` + per-module tests, `test_sale_finalization_failure_injection.py` |
| I | Migration interrupted before completion leaves exact pre-migration state | Tested | `test_disaster_recovery_scenarios.py` (new for M12) |

Scenario I was the one genuinely untested gap going into M12: every
migration relies on Alembic's "Will assume transactional DDL" guarantee
printed in its own log output, but nothing had ever forced a real
migration to fail partway through (one real `CREATE TABLE`, then a
forced exception) and proven Postgres rolls back the whole thing, not
just the statement that raised. Now proven directly.

**Untested operational dependency**: zero-downtime deployment — this
system has none today (a single Compose service restarting means a
brief outage), acknowledged rather than claimed away.

## 12. Mutation testing

15 targeted mutations against genuinely new/modified M12 code (the
config validator's three checks, the refresh-token row lock, both
user-deactivation guards, the request-size and security-header
middleware, one pagination bound, the correlation-ID header and its
inbound-length bound, the `/health/migration` comparison, both
`alembic_version` privilege migrations, and the `SALE_COMPLETED` audit
call) — apply, run the targeted test, expect failure, revert via `git
checkout`, confirm green again.

**14/15 caught immediately.** The one miss was instructive: removing
`.with_for_update()` (Section 2.1's fix) was **not** reliably caught by
the single-trial version of its regression test — a timing-dependent
race can pass by chance on any one trial, and it did, once. Per the
task's explicit "if a mutation is not detected, strengthen the test"
instruction, the test now repeats the race across 12 independent trials
(fresh user/login/token each time), mirroring the scale of the manual
probe that originally found the bug (14/15 failures without the fix).
Re-running the same mutation against the strengthened test: caught on
the first trial.

This is the second real defect this milestone's own verification
process found in itself (the first being the mutation's target defect
in the first place) — exactly the point of the exercise: not "does the
code work," but "does the test suite actually notice when it doesn't."

## 13. What M12 deliberately did not build

Stated up front, mirroring M11's own discipline — never claimed as
"safe because untested," always named as a gap:

1. No reverse proxy (nginx/TLS) stood up — standing up and TLS-testing
   one end-to-end is its own substantial body of work; the
   application-layer request-size limit (Section 6) is the defense that
   has to exist regardless of whether a proxy sits in front of it, and
   the proxy-layer backstop is documented, not simulated.
2. The in-process rate limiter (`app/core/rate_limit.py`, unchanged
   from M2) remains explicitly single-process-only — a documented,
   pre-existing limitation, not something M12 introduced or was asked
   to fix.
3. `payroll_periods`/`payroll_employee_results`/`sales`/etc. remain
   application-layer-only immutable, not DB-privilege-enforced — a
   deliberate, inherited M1 decision.
4. Store isolation remains 100% application-layer (no RLS) — same
   reasoning, unchanged since M1.
5. Backups are local-filesystem only; off-box/encrypted transport is a
   documented follow-up, not built.
6. No monitoring/alerting stack is deployed; only the instrumentation
   (structured logs + correlation IDs) it would consume.
7. No admin UI beyond the one user-deactivation endpoint (Section 7) —
   explicitly scoped that narrowly per the task brief.
8. `pg_dump`/`pg_restore` deliberately do NOT use `--no-owner`/
   `--no-privileges` (the design doc's original sketch suggested them;
   the actual working test corrected course) — using those flags would
   have silently stripped the privilege state Section 8's fix depends
   on surviving a restore.

## 14. Known operational risks carried forward

- A single Compose process restarting is a real, if brief, outage — no
  zero-downtime deployment story exists.
- The rate limiter's per-process counters mean horizontal scaling
  (more than one app instance) silently multiplies the effective
  brute-force budget until a shared store replaces it — documented in
  `app/core/rate_limit.py`'s own module docstring since M2, unchanged.
- Reading the audit log has no dedicated API endpoint today — the
  `AUDIT_READ` permission and `AUDITOR` role exist (since M2) but no
  route consumes them; direct DB access or a future admin UI is the
  only way to browse it. Not built in M12 (would have widened scope
  beyond the one named capability).

## 15. Exact files changed

34 backend files (3,528 insertions, 50 deletions) plus
`docs/M12_DESIGN.md` (466 lines) and this document. Full list:
`git diff --stat cc5d848..HEAD`. Twelve new test files
(`test_config_safety.py`, `test_backup_restore.py`,
`test_auth_hardening.py`, `test_api_security_hardening.py`,
`test_audit_hardening.py`, `test_user_deactivation.py`,
`test_health_and_observability.py`,
`test_sale_finalization_failure_injection.py`,
`test_migration_operations_audit.py`,
`test_disaster_recovery_scenarios.py`), three new scripts
(`backup_database.sh`, `restore_database.sh`, `integrity_snapshot.py`,
`performance_baseline.py`), two new Alembic migrations, and one new
core module (`app/core/correlation.py`) plus
`app/core/http_hardening.py`.

## 16. Final verdict: PASS WITH CONDITIONS

**PASS** because: two real defects were found by this milestone's own
adversarial process and fixed with permanent regression tests before
completion (both touching authentication/migration safety, both in the
Final Gate's "must be fixed" category); backup/restore is proven, not
assumed, against a realistic dataset with accounting/inventory
integrity verified end to end; every DB-privilege claim in this
document is backed by a live adversarial SQL attempt, not the grant
table alone; mutation testing found and fixed a second-order gap in the
test suite itself.

**WITH CONDITIONS** because Section 13's list is real: no reverse
proxy/TLS, a single-process rate limiter, no monitoring stack, no
off-box backup transport, and no audit-log read endpoint are all
genuine, named, unaddressed operational gaps for a real production
deployment — none of them newly introduced by M12, none silently
claimed as solved, all explicitly carried forward as the honest state
of the system per this milestone's own "never call something
production-ready merely because it is theoretically possible"
instruction.

M13 has not been started.
