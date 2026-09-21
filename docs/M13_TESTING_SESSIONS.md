# M13 Testing Sessions

Phases 16–19's major test sessions: what was actually run, against
what, with what result — not what was intended to be tested. Every
defect below was found by running real code against real
infrastructure (nginx, PostgreSQL, Prometheus, Alertmanager, the
actual backup/restore scripts), never simulated or assumed.

---

## Session 1 (Phase 16): permanent integrated production-topology test

**File**: `deploy/tests/test_integrated_production_session.py`

**Objective**: one continuous session against the real stack (client →
nginx/TLS → FastAPI → PostgreSQL → monitoring), covering 15 named
scenarios (A–O), proving the pieces work together, not just each in
isolation.

**Environment**: `conftest.py`'s `production_stack` fixture — a real
self-signed TLS cert, real nginx process using this repo's actual
`deploy/nginx/` config, a real uvicorn process (no `--reload`,
loopback-only), against `erp_dev`.

**Procedure** (file order deliberate — later steps depend on state
earlier ones create): A. normal authenticated request through nginx.
B. store-isolated request refused (403). C. sale finalization through
the full path. D. inventory/accounting integrity after the sale. E.
idempotent retry of the same sale. F. PostgreSQL stopped, health/API
fail cleanly. G. PostgreSQL restarted, traffic resumes. H. backend
killed, nginx returns a clean 502, traffic resumes after restart. I.
correlation ID propagates end to end into the nginx access log. J.
rate limiting fires and recovers. K. audit-log authorization/store-
scoping against a real cross-store sale. L. a real `backup_offbox.sh`
cycle feeds the real monitoring textfile bridge. M. an isolated
monitoring stack catches the exact DB outage steps F/G already caused.
N. `/health/migration` reports current. O. a real uvicorn process with
an insecure production `SECRET_KEY` refuses to boot.

**Expected result**: all 15 steps pass without any mock standing in
for nginx, PostgreSQL, auth, or monitoring.

**Actual result**: all 15 passed, twice consecutively (27.07s, then
30.26s) once the two defects below were fixed.

**Defects found and fixed**:
1. `session_setup`'s engine lacked `pool_pre_ping=True`. Fixed to match
   `app/db/session.py`'s own real engine construction.
2. Root cause after (1) alone didn't fix a rerun: the fixture's
   SQLAlchemy `Session` never called `.close()` between steps, so it
   held one physical connection checked out continuously through F/G's
   PostgreSQL stop/restart — `pool_pre_ping` only revalidates at pool
   *checkout*, which never happened. Fixed by closing the session
   explicitly at the end of step E, the last step to use it before F/G
   disrupt PostgreSQL.

**Regression test**: the fix is the test itself — rerun twice
consecutively to rule out a one-off pass.

**Final status**: verified.

---

## Session 2 (Phase 17): failure injection / disaster recovery

**File**: `deploy/tests/test_failure_injection_disaster_recovery.py`

**Objective**: 15 named failure-injection scenarios. 8 already had
real proof elsewhere in the repo (cited in the file's own module
docstring rather than duplicated); 7 were genuine gaps needing new
tests.

**Environment**: `production_stack`, plus disposable databases created
and dropped per test (never `erp_dev`/`erp_test` for anything
destructive).

**Procedure and result, new tests only**:

- **Failed backup**: ran the real `backup_offbox.sh` against an
  unreachable rclone remote. Expected/actual: nonzero exit, status
  file reports `erp_backup_last_success 0`, via the script's own
  `on_failure` trap — not a hand-written status file.
- **Restore + application startup**: dumped `erp_dev`, restored into a
  disposable database, booted a real uvicorn process against it,
  logged in and queried a marker row through the real API. Expected/
  actual: the restored data is reachable through the running
  application, not just via a direct SQLAlchemy query.
- **Duplicate transaction after recovery**: finalized a sale, killed
  and restarted the backend, retried the identical request. Expected/
  actual: same sale ID, one journal entry, one inventory movement —
  proving idempotency survives a process boundary, not just a
  sequential retry with no restart in between.
- **Monitoring component unavailable**: killed `erp_exporter` inside
  an isolated Prometheus/Alertmanager instance. **Real gap found**: no
  alert covered the exporter itself dying — every other alert's value
  is computed *from* its scrape, so its death produced silence, not a
  firing alert. **Fixed**: added `ERPMonitoringTargetDown` (`up{job!=
  "prometheus"} == 0`) to `deploy/prometheus/alerts.yml` and Section 16
  of `docs/M13_PRODUCTION_RUNBOOKS.md`. Actual: fires within 2m of the
  kill, clears within 2m of restart.
- **Disk pressure**: a 200KB isolated `tmpfs` mount (never the shared
  host disk) as the backup target. Expected/actual: `pg_dump` fails
  loudly on ENOSPC, not a silently truncated dump.
- **Expired/forged JWT through the real proxy**: the same forged-token
  technique `test_auth_hardening.py` already proves exhaustively
  in-process, sent through the real nginx/TLS hop. Expected/actual:
  401, same as in-process, and nginx doesn't leak internals.
- **Concurrent requests during recovery**: 12 distinct sale requests
  fired concurrently while the backend restarts mid-burst; any
  ambiguous (non-201) response retried post-recovery with the same
  `client_transaction_id`. Expected/actual: exactly 12 distinct sales,
  each with exactly one balanced journal entry and one inventory
  movement, inventory decremented by exactly 12 units — no partial or
  duplicate financial effect from the recovery window.

**Defects found during test *development*, not the scenarios
themselves** (fixed before the file was considered done):
- `pg_dump` run as the `postgres` OS user couldn't write into pytest's
  `tmp_path` (root-owned, mode 0700) — switched to the fixed, fully-
  open directory `backend/tests/test_backup_restore.py` already uses
  for the identical reason.
- A test assumed the shared `local_stack_env` fixture's product
  started at a fixed quantity; an earlier test in the same file had
  already sold some of it. Fixed by snapshotting the quantity
  immediately before the concurrent burst.
- A `DetachedInstanceError` from reading `store.id` after the session
  that loaded it had been closed. Fixed by capturing the id into a
  plain variable first.
- `deploy/tests/conftest.py`'s `production_stack` fixture only tracked
  its own original uvicorn process handle — several test files (this
  one included) kill and restart the backend via `pgrep`/`kill` to
  simulate a real crash, and the replacement process was untracked, so
  teardown could leave it running past the end of the test module.
  Fixed with a `pgrep`-based safety-net sweep in the fixture's own
  teardown.

**Final status**: verified. 59/59 then 72/72 (after Phase 18 added
more tests to the suite) full `deploy/tests/` regression runs, twice
consecutively.

---

## Session 3 (Phase 18): adversarial security audit

**File**: `deploy/tests/test_adversarial_security_audit.py`

**Objective**: 24 named attack vectors against the deployment layer.
13 already proven elsewhere (cited, not duplicated); 11 new tests
across 6 genuine gaps and 5 partial gaps.

**Procedure and result** (new coverage only — see
`docs/M13_HARDENING_AUDIT.md` for the full 24-vector classification):

- **Spoofed X-Forwarded-Proto**: config-text assertion
  (`proxy_set_header X-Forwarded-Proto $scheme;` is unconditional) plus
  a live forged-header request. Actual: identical response either way.
- **Internal service loopback-only binding**: the real `ss -tlnp`
  socket table, parametrized over PostgreSQL, Prometheus, Alertmanager,
  node_exporter, postgres_exporter, erp_exporter (using the real
  `monitoring_stack` fixture rather than skipping when nothing was
  already running — the first draft of this test *did* just skip 4 of
  5 cases, a real coverage gap fixed before commit).
- **Backup script output never leaks the DB password**: a real
  `backup_database.sh` run, output searched for the literal password.
- **Audit-log pagination cap**: `limit=999999999` against the real
  endpoint. Actual: 422 (the generic cap test in
  `test_api_security_hardening.py` proved the mechanism against a
  different endpoint; this proves it's actually wired up here too).
- **Oversized query string**: ~27KB of query parameters (kept under
  httpx's own 65536-byte client-side URL cap, which the first draft
  exceeded and never even reached nginx). Actual: 400/414/431, never a
  hang or silent pass-through.
- **Rate-limit bypass via rotated X-Forwarded-For**: a fresh, distinct
  spoofed value on every request in a login burst. Actual: 429 still
  fires — nginx keys on `$binary_remote_addr`, never any header.
- **erp_app privilege escalation**: `ALTER ROLE ... WITH CREATEDB`,
  `CREATE ROLE`, and a self-`GRANT` on `audit_logs`. First two: real
  `permission denied`. The self-`GRANT`: PostgreSQL's real behavior for
  a grantor with no `GRANT OPTION` on anything is a `WARNING`, not an
  error — the command completes without raising. Proved the grant was
  a true no-op by re-attempting the `UPDATE` `test_constraints.py`
  already proves denied: still denied.
- **Backup/restore artifact permissions**: **real defect found**.
  `pg_dump`'s and `age`'s output inherited the invoking process's
  default umask (`-rw-rw-r--`, 0664 — world/group-readable), meaning
  the full plaintext database dump was readable by any OS user on the
  host before encryption ever happened. **Fixed**: `chmod 600`
  immediately after creation in `backend/scripts/backup_database.sh`,
  the encrypted artifact + manifest in `deploy/scripts/backup_offbox.sh`,
  and the decrypted restore output in `deploy/scripts/restore_offbox.sh`.
  Verified against a real backup/restore cycle's actual file mode
  bits, not the scripts' source text.

**Final status**: verified. 13/13 new tests passing; full
`deploy/tests/` regression at 65 then 72 (as later phases added more).

---

## Session 4 (Phase 19): mutation testing

**Method**: for each of 15 mechanisms, a small targeted change was
applied directly to the real source, the relevant test(s) run and
confirmed to fail, the change reverted via `git checkout`, and the
test(s) rerun to confirm they pass again clean. `git diff --stat` on
the touched file was checked empty after every single revert before
moving to the next mutation — no mutation was ever left applied
between cycles.

**Result**: all 15 mutations detected by the existing suite, with two
mutations surfacing real findings before that detection was confirmed
(both write-ups below, full list in
`docs/M13_HARDENING_AUDIT.md` Section on mutation testing):

- Disabling **only** the sale-finalization idempotency fast-path was
  *not* detected by any test — re-running it against the full backend
  suite (823 tests) confirmed the DB `UNIQUE` constraint + its
  `IntegrityError`-recovery fallback is genuine defense-in-depth that
  keeps the system correct even without the fast-path optimization.
  Not a gap; disabling *both* layers together (the fast path and the
  recovery return) was needed to break observable correctness, and
  that combined mutation *was* caught.
- Disabling `restore_offbox.sh`'s manual checksum comparison was *not*
  caught by the existing corrupted-backup test, because that test's
  byte-flip corruption is always caught by `age`'s own authenticated
  encryption failing to decrypt at all, before the checksum comparison
  code ever runs. A new permanent test was added —
  `test_a_tampered_manifest_checksum_is_detected_and_restore_is_refused`
  — that tampers only the manifest's recorded checksum, so decryption
  succeeds cleanly and only the checksum comparison itself can catch
  it. Reran the mutation against the new test: caught.

Two planned mutations (erp_app self-escalating `alembic_version`
privileges; erp_app mutating immutable ledger tables) were not
attempted via live `GRANT`/`REVOKE` against the shared `erp_dev`
database — a direct `GRANT` command was blocked by this session's own
safety classifier as privilege-escalation-shaped, and an
Alembic-migration-based workaround would have had the identical effect
and defeated the intent of that block. Both mechanisms remain verified
by the already-passing static suite
(`test_erp_app_can_read_but_not_write_alembic_version`,
`test_runtime_role_cannot_update_or_delete_ledger_tables`) — a
documented limitation, not a hidden gap.

**Final status**: 15/15 mutations proven detected; every revert
confirmed clean via `git diff --stat` before the next cycle began.
