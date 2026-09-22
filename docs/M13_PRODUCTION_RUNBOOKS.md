# Operational Runbooks

M13 Phases 14, 17-18. Sixteen scenarios, each stating: **Symptoms** (how you'd
notice), **Immediate checks** (what to look at first, read-only),
**Safe actions** (what you may do), **Dangerous actions** (what you
must NOT do), **Recovery**, **Verification** (how you know it's
actually fixed).

**Standing rule, every runbook**: never directly `UPDATE`/`INSERT`/
`DELETE` an accounting, inventory, AP, or payroll table by hand as a
first-line recovery method. Those tables are protected precisely so
that even a compromised or careless `erp_app` credential cannot alter
them (`docs/M1_DATABASE_DESIGN.md`, `docs/M12_HARDENING_AUDIT.md`
Section 8) — a human operator bypassing that protection with a raw
`UPDATE` defeats the entire control and can silently break the
double-entry invariant a real fix (a correcting journal entry, a
reversing service-layer transaction, or a restore from backup) would
have preserved. Every runbook below routes "fix the data" through the
application's own service layer, a documented reversing operation, or
a full restore — never a manual row edit.

Alert cross-references (`deploy/prometheus/alerts.yml`) point at the
numbered sections below by name; the numbers here are the contract —
don't renumber without updating that file.

---

## 1. Application unavailable

**Symptoms**: `ERPApiDown` fires (`erp_api_up == 0` for 1m); nginx
returns 502/504 for every request; `GET /health` fails or times out.

**Immediate checks**:
- `curl -s http://127.0.0.1:8000/health` directly on the host (bypass
  nginx) — distinguishes "backend is down" from "nginx can't reach it."
- `docker compose ps backend` (or `systemctl status` for a native
  deploy) — is the process running at all?
- `docker compose logs --tail=200 backend` (or the equivalent log
  file) — a crash loop, an unhandled startup exception, or an OOM kill
  (`dmesg | grep -i oom`) all look different here.

**Safe actions**:
- Restart the backend process/container (`docker compose restart
  backend`, or the equivalent systemd unit).
- If it crash-loops, roll back to the last known-good image/commit per
  `deploy/scripts/deploy.sh`'s own rollback guidance (Section 11
  below) rather than repeatedly restarting a broken build.

**Dangerous actions**:
- Do not restart PostgreSQL to "fix" an application problem — confirm
  with `/health/db` (Section 2) first; restarting the database is a
  much bigger blast radius for what might be a pure application bug.
- Do not skip the pre-restart log check "to save time" — a crash-loop
  restarted blindly just loops again.

**Recovery**: fix the underlying cause (bad config, bad deploy, OOM)
and redeploy via `deploy/scripts/deploy.sh`, which itself will not
declare success until smoke tests pass.

**Verification**: `GET /health` returns `{"status":"ok"}` through
nginx; `ERPApiDown` clears in Alertmanager; a real login + one read
request succeeds.

---

## 2. Database unavailable

**Symptoms**: `ERPDatabaseDown` fires (`erp_db_up == 0` for 1m); `GET
/health/db` returns 503 `{"status":"error","database":"unreachable"}`;
`GET /health` may still return 200 (the app process itself is fine —
see Section 1's distinction).

**Immediate checks**:
- `sudo -u postgres pg_isready` (or `systemctl status postgresql`) —
  is the cluster actually up?
- `pg_settings`/`pg_stat_activity` connection count against
  `max_connections` (`ERPDatabaseConnectionsNearExhaustion` may have
  fired first — see Section 15's "high error rate" for the connection-
  exhaustion variant of this).
- Disk space on the PostgreSQL data volume (`df -h` on
  `/var/lib/postgresql`) — a full disk stops PostgreSQL from accepting
  writes even though the process is alive (see Section 3).

**Safe actions**:
- Restart PostgreSQL (`systemctl restart postgresql`) if it's down and
  disk space is confirmed NOT the cause.
- If connections are exhausted: identify and terminate genuinely stuck
  `idle in transaction` sessions (`SELECT pid, state, query,
  state_change FROM pg_stat_activity WHERE state = 'idle in
  transaction' AND state_change < now() - interval '10 minutes';` then
  `SELECT pg_terminate_backend(<pid>)` for ones confirmed stuck, not a
  blanket kill of every connection).

**Dangerous actions**:
- Do not run `pg_resetwal` or any WAL-surgery command as a first
  response — those are last-resort, data-loss-risking tools for a
  corrupted cluster that won't start at all, not for "the database is
  slow or full."
- Do not `DELETE FROM` any table to free disk space under pressure —
  see Section 3.

**Recovery**: once PostgreSQL is reachable again, confirm the app's
own connection pool recovers on its own (it will — `pool_pre_ping=True`
in `app/db/session.py` discards stale connections automatically); no
application restart should be needed for a transient DB outage.

**Verification**: `GET /health/db` returns 200
`{"status":"ok","database":"reachable"}`; `ERPDatabaseDown` clears.

---

## 3. Disk full

**Symptoms**: `ERPDiskSpaceLow` fires (<10% free for 5m); PostgreSQL
writes start failing (`could not extend file` in its log); backups
(Section 4) start failing too.

**Immediate checks**:
- `df -h` on every mounted filesystem — identify WHICH one is full
  (the PostgreSQL data volume, the application log volume, and the
  local backup directory are the three most likely culprits in this
  topology).
- `du -sh /var/lib/postgresql/*/main/pg_wal` — a stuck replication
  slot or a runaway WAL archive is a common, specific cause of
  PostgreSQL's own disk usage growing unbounded.
- `du -sh backend/backups/* deploy scripts' BACKUP_DIR` — old local
  backups accumulating with no retention policy is the other common
  cause (`deploy/scripts/backup_offbox.sh` has retention cleanup for
  the OFF-BOX copy; the LOCAL backup directory does not, by design —
  see `docs/M13_DESIGN.md` Section 5 — an operator/cron job must
  prune it).

**Safe actions**:
- Delete OLD LOCAL BACKUP FILES ONLY after confirming each one's
  off-box copy exists and its manifest checksum was verified
  (`deploy/scripts/backup_offbox.sh` step 4) — never delete the only
  copy of a backup.
- Delete old, rotated application log files.
- Expand the volume if this is a recurring pattern rather than a
  one-time spike.

**Dangerous actions**:
- Do not delete anything under PostgreSQL's own data directory
  (`pg_wal`, `base/`, etc.) by hand — this is how you turn "disk full"
  into "corrupted cluster." Let PostgreSQL manage its own WAL; fix the
  underlying cause (a stuck replication slot, `archive_command`
  failing) instead.
- Do not delete the MOST RECENT local backup to free space right
  before a deploy — Section 4/5 need it.

**Recovery**: free space via the safe actions above; if PostgreSQL
itself refused writes, restart it once space is freed.

**Verification**: `ERPDiskSpaceLow` clears; a real write (e.g. the
next scheduled backup) succeeds.

---

## 4. Backup failure

**Symptoms**: `ERPBackupFailed` fires (`erp_backup_last_success == 0`)
or `ERPBackupStale` fires (no successful run in >25h).

**Immediate checks**:
- Run `deploy/scripts/backup_offbox.sh` manually and read its output —
  it prints which of its 5 steps failed (local dump, encrypt, upload,
  remote-checksum-verify, retention cleanup).
- If step 1 (local `pg_dump`) failed: check disk space (Section 3) and
  database connectivity (Section 2) first — a backup failure is often
  a symptom of one of those, not an independent problem.
- If step 2 (encrypt) failed: confirm `BACKUP_AGE_PUBLIC_KEY` is set
  and valid — the script refuses to run without it by design (never an
  unencrypted backup transported off-box).
- If step 3/4 (`rclone` upload/verify) failed: check network
  connectivity to the off-box destination and `rclone`'s own
  credentials (`rclone config show`).

**Safe actions**:
- Re-run the backup script manually once the underlying cause (disk,
  DB, network, credentials) is fixed.
- Check the Prometheus textfile status
  (`/var/lib/erp/textfile_collector/backup_status.prom`) reflects the
  corrected state after a successful manual run.

**Dangerous actions**:
- Do not disable the `ERPBackupFailed` alert to silence it — a missed
  backup is exactly the kind of thing that must stay visible until
  fixed.
- Do not manually write a "fake success" into the status file — that
  actively hides a real gap in recoverability.

**Recovery**: a successful manual (or next scheduled) run of
`backup_offbox.sh`.

**Verification**: `erp_backup_last_success` reads 1; the alert clears;
periodically (not only during an incident) actually run a full restore
per Section 5 to prove backups are not just "succeeding" but genuinely
restorable.

---

## 5. Restore procedure

**Symptoms**: N/A — this runbook is invoked deliberately (disaster
recovery, or a scheduled restore drill), not by an alert.

**Immediate checks**: identify the correct backup to restore
(`rclone lsl <remote>` for off-box copies, or the local `backups/`
directory) and confirm ITS manifest exists alongside it.

**Safe actions**: restore into a NEW or explicitly-designated recovery
target database, never directly over a database anyone still depends
on:
- Local restore: `backend/scripts/restore_database.sh <dump_file>
  <target_db_name>` (already destructive to `<target_db_name>` by
  design — see that script's own header).
- Off-box restore: `deploy/scripts/restore_offbox.sh <remote>
  <encrypted_filename> <staging_dir> <target_db_name>` — refuses to
  proceed if the decrypted checksum doesn't match the manifest
  (corruption/tamper detection, `deploy/tests/test_backup_offbox.py`).
- After EITHER restore path: run `python -m
  scripts.integrity_snapshot <restored_db_url>` and compare against a
  snapshot taken before the incident (financial totals, inventory
  quantities, AP/payroll balances) — this is the actual proof the
  restore is trustworthy, not just that the commands exited 0.

**Dangerous actions**:
- Do not restore over the live production database directly — restore
  to a fresh target, verify it, THEN cut the application over
  (`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`) to the verified target.
- Do not skip the integrity-snapshot comparison "because the restore
  command succeeded" — a truncated or logically-corrupted dump can
  restore "successfully" while still being wrong.

**Recovery**: point the application at the verified, restored
database; run `alembic upgrade head` if the backup predates the
current schema (matching the deployed code's own migration head).

**Verification**: `GET /health/migration` reports current; the
integrity snapshot comparison shows no discrepancy; a manual spot
check of a few recent, known transactions confirms they're present and
correct.

---

## 6. Migration failure

**Symptoms**: `ERPMigrationMismatch` fires; `GET /health/migration`
returns 503 `schema_mismatch`; `deploy/scripts/deploy.sh` itself
aborted at "Step 3: Migration" (see its own printed rollback guidance).

**Immediate checks**:
- Read the exact Alembic error from `deploy.sh`'s output or the
  migration log — a syntax error, a constraint violation against
  existing data, and a lock timeout all need different fixes.
- Confirm the database was NOT left in a partial state:
  `SELECT version_num FROM alembic_version;` should show the
  PREVIOUS revision, not a new/unexpected one (Alembic's transactional
  DDL guarantees this —
  `backend/tests/test_disaster_recovery_scenarios.py`,
  `deploy/tests/test_deploy_procedure.py`).

**Safe actions**:
- Fix the migration (or the data it conflicts with, via a proper
  service-layer correction, not a raw `UPDATE`) and re-run `alembic
  upgrade head`.
- If the migration is simply wrong, write a corrected replacement
  migration rather than hand-editing the database to match what the
  broken one would have produced.

**Dangerous actions**:
- Do not manually run the migration's SQL by hand piecemeal, skipping
  the parts that failed — this is exactly how a database ends up in a
  state Alembic's own history no longer describes correctly.
- Do not `stamp` the database to a revision it hasn't actually reached
  as a shortcut — `alembic_version` must reflect the database's real
  schema, not an aspirational one.
- Never deploy a NEW application version against this half-attempted
  state, and never redeploy the PREVIOUS version once a migration DID
  succeed (see Section 11's compatibility boundary).

**Recovery**: fix and re-apply the migration; confirm via
`GET /health/migration`.

**Verification**: `GET /health/migration` returns 200 `current`;
`ERPMigrationMismatch` clears; the application itself functions
normally against the new schema.

---

## 7. Authentication incident

**Symptoms**: `ERPElevatedAuthFailureRate` fires (>20 failed logins in
5m — possible credential stuffing); `ERPRefreshTokenReuseDetected`
fires (a stolen/replayed refresh token was used — M12's reuse-
detection already force-revoked that user's sessions automatically
when this fired).

**Immediate checks**:
- `GET /api/v1/audit-log?action=LOGIN_FAILURE` (or
  `REFRESH_TOKEN_REUSE_DETECTED`), scoped by the caller's own
  AUDIT_READ-gated access (M13 Phase 7) — identify the affected
  account(s) and the source IP pattern.
- Is the failure pattern spread across many usernames (credential
  stuffing against the whole user base) or concentrated on one account
  (a targeted attempt)?

**Safe actions**:
- For `ERPRefreshTokenReuseDetected`: confirm the automatic session
  revocation actually happened (the audit event's `after_state`
  records `revoked_all_sessions: true`); require the affected user to
  reset their password before logging in again.
- For a credential-stuffing pattern: deactivate the specifically
  targeted account(s) via `POST /api/v1/auth/users/{user_id}/deactivate`
  if compromise is confirmed or suspected, forcing a password reset on
  reactivation.
- Consider a firewall-level IP block for a clearly malicious source
  (nginx's `erp_login` rate-limit zone already throttles the request
  RATE from any one source — Section 15/Phase 4 — a firewall block is
  an escalation beyond that, not a replacement for it).

**Dangerous actions**:
- Do not disable login rate limiting "to see if legitimate users are
  affected" — that removes the very control mitigating the incident.
- Do not reuse or manually reissue a revoked refresh token for a user
  "to save them from re-logging in" — the whole point of reuse
  detection is that token is no longer trustworthy.

**Recovery**: affected accounts re-authenticate with a new password;
confirm no unauthorized action was taken during the compromised
window via the audit log (cross-reference the account's audit trail
for the incident window).

**Verification**: `ERPElevatedAuthFailureRate`/
`ERPRefreshTokenReuseDetected` clear; the affected account's next
login succeeds normally; no further reuse-detection events for that
account.

---

## 8. Suspected duplicate transaction

**Symptoms**: a cashier/operator reports what looks like the same sale
(or payment, receipt, etc.) appearing twice; a customer was charged
twice.

**Immediate checks**:
- Query by `client_transaction_id` (sales) or the equivalent
  idempotency key for the affected domain (AP payments, transfers,
  payroll posting all have their own — see
  `docs/M12_HARDENING_AUDIT.md` Section 7 and
  `deploy/tests/test_connectivity_failure.py` for the sales case
  specifically) — a genuine duplicate is structurally impossible if
  the SAME key was reused (a unique DB constraint prevents it); TWO
  DIFFERENT keys for what a human perceives as "the same" transaction
  means the client actually sent two distinct requests (e.g., a
  cashier manually re-entered a sale instead of the POS retrying
  automatically), which is a process/training issue, not a system bug.
- `GET /api/v1/audit-log?entity_type=sale&actor_id=<cashier>` (or the
  relevant entity_type) around the reported time to see the actual
  sequence of events.

**Safe actions**:
- If it IS a genuine duplicate (same key somehow produced two rows —
  would indicate a real bug, not expected behavior): stop and escalate
  to engineering before touching any data; this is the one scenario
  where the "how did this happen" question matters as much as the
  cleanup.
- If it's two distinct legitimate-looking transactions from human
  double-entry: process a normal return/void (Sale) or a correcting
  entry (AP/payroll) through the application's own workflow — never a
  raw delete of the extra row.

**Dangerous actions**:
- Do not `DELETE` the "duplicate" row directly — even a genuine
  duplicate needs a reversing entry through the application (so
  inventory/accounting stay consistent), not a silent deletion that
  leaves no trail.
- Do not assume it's a system bug without checking the idempotency key
  first — most "duplicates" reported by staff are two distinct client
  requests, not a broken guarantee.

**Recovery**: a return/void or correcting entry through the normal
application workflow, refund the customer if applicable.

**Verification**: `python -m scripts.integrity_snapshot` shows the
store's totals reconcile after the correction; the audit log shows
the correcting entry.

---

## 9. Accounting reconciliation discrepancy

**Symptoms**: trial balance doesn't balance, or a report total doesn't
match expected figures.

**Immediate checks**:
- `python -m scripts.integrity_snapshot <database_url>` — compares
  actual ledger totals against expected invariants (M12 Phase 15).
- `GET /api/v1/reports/...` (Trial Balance, P&L — M11) for the
  specific period in question.
- `GET /api/v1/audit-log?entity_type=journal_entry` for the affected
  period — every journal entry is traceable to the operation that
  created it.

**Safe actions**: identify the SPECIFIC journal entry/entries causing
the imbalance via the audit trail; if a genuine posting error is
found, post a CORRECTING journal entry through the normal accounting
flow (not a manual `UPDATE` to the original entry — the original stays
as the historical record).

**Dangerous actions**:
- Do not `UPDATE` any `journal_entries`/`journal_lines` row directly —
  `erp_app` cannot do this anyway (M1's runtime-role grants deny
  UPDATE/DELETE on ledger tables), and an operator working around that
  via a superuser connection defeats the entire control.
- Do not "balance the books" with a plug entry that isn't tied to a
  real, understood cause.

**Recovery**: a correcting entry that brings the trial balance back
into agreement, with a clear audit trail explaining why.

**Verification**: `python -m scripts.integrity_snapshot` and the
Trial Balance report both show the discrepancy resolved.

---

## 10. Inventory reconciliation discrepancy

**Symptoms**: `current_qty_on_hand` doesn't match a physical count;
`inventory_movements` don't sum to the expected on-hand quantity.

**Immediate checks**:
- `GET /api/v1/audit-log?entity_type=stock_count` and
  `entity_type=stock_adjustment` for the product/store in question.
- Sum `inventory_movements.quantity_delta` for the product and compare
  against `products.current_qty_on_hand` — M8's own invariant.

**Safe actions**: if a genuine physical/system mismatch is confirmed,
post a `stock_adjustment` through the normal inventory workflow (`POST
/api/v1/inventory/adjustments`) with a clear reason — this is the
SAME mechanism a routine physical count reconciliation already uses,
not a special-case override.

**Dangerous actions**:
- Do not `UPDATE products SET current_qty_on_hand = ...` directly —
  this breaks the invariant that on-hand quantity is always the sum of
  its movements, and leaves no movement record explaining the change.
- Do not adjust inventory to match a suspected sales/theft loss
  without first confirming there isn't a simpler, pending explanation
  (an un-received purchase order, an un-posted transfer receipt).

**Recovery**: a `stock_adjustment` (with a category/reason) through
the application.

**Verification**: the movement-sum-vs-on-hand invariant holds again;
`python -m scripts.integrity_snapshot` confirms.

---

## 11. AP discrepancy

**Symptoms**: a supplier invoice balance, payment allocation, or AP
aging figure looks wrong.

**Immediate checks**:
- `GET /api/v1/audit-log?entity_type=purchase_invoice` (and
  `supplier_payment`, `supplier_credit_note`) for the supplier/invoice
  in question.
- Compare the invoice's recorded payments/allocations against what was
  actually received/paid.

**Safe actions**: post a correcting entry through the normal AP
workflow (a credit note, a payment reversal/reallocation via the
application's own endpoints) — never a manual balance edit.

**Dangerous actions**: do not directly modify
`purchase_invoices`/`supplier_payments`/`supplier_payment_allocations`
rows — same rationale as Section 9: these are financial ledger data
with the same protection and the same reasoning for never bypassing
it.

**Recovery**: a correcting AP transaction through the application.

**Verification**: the invoice's balance and the supplier's AP aging
report reflect the correction.

---

## 12. Payroll discrepancy

**Symptoms**: a payroll period's calculated pay for an employee looks
wrong, or doesn't match expectations after posting.

**Immediate checks**:
- `GET /api/v1/audit-log?entity_type=payroll_period` and
  `entity_type=attendance_record` for the employee/period.
- Review the specific earning/deduction lines
  (`payroll_earning_lines`/`payroll_deduction_lines`) that produced the
  total, and the attendance records/compensation rate that fed the
  calculation.

**Safe actions**: if the payroll period is still in a pre-posting
status, correct the underlying attendance/compensation data and
RE-RUN the calculation through the normal workflow. If it has already
been POSTED (and thus already reflected in accounting), use the
documented payroll reversal mechanism
(`payroll_reversals` — M10's own reversal workflow) rather than
editing posted figures.

**Dangerous actions**: do not directly edit a POSTED payroll period's
result rows — this is the one domain where a mistake also has
downstream accounting AND (potentially) real payment implications;
always go through the reversal-and-repost workflow, and always involve
a second person's sign-off for anything payroll-related given the
stakes, even though this system doesn't enforce that technically.

**Recovery**: corrected attendance/compensation data, re-run
calculation, or a formal reversal-and-repost.

**Verification**: the corrected payroll period's totals match
expectations; the accounting entries it posted reconcile
(Section 9).

---

## 13. Suspicious cross-store access

**Symptoms**: a store-scoped user (e.g. a Manager) appears to have
accessed or acted on another store's data; or an unscoped
role (Admin/Auditor) shows unexpected activity across many stores in
a short window.

**Immediate checks**:
- `GET /api/v1/audit-log?actor_id=<user>` — the audit-log endpoint
  itself enforces store-scoping for a store-scoped CALLER (M13 Phase
  7), so run this check as an Admin/Auditor to see the FULL picture
  across every store that user touched.
- Cross-reference against `users.store_id` — is this user even
  SUPPOSED to be unscoped? An unexpected `store_id = NULL` on an
  account that should be store-limited is itself a finding.

**Safe actions**: if genuine unauthorized cross-store access is
confirmed, deactivate the account immediately (`POST
/api/v1/auth/users/{user_id}/deactivate`) and treat as an
authentication incident (Section 7) for credential-compromise
handling.

**Dangerous actions**: do not assume a single cross-store audit row is
malicious without checking the role matrix first — Admin, Manager
(when genuinely unscoped by design), and Auditor legitimately see
multiple stores; the finding is a MISMATCH between a user's intended
scope and their actual access, not "any" cross-store row.

**Recovery**: account deactivation/re-scoping as appropriate; review
what the account actually did via the audit trail and correct any
unauthorized changes through the normal domain-specific runbook
(Sections 9-12) for whatever was touched.

**Verification**: the account's access now matches its intended scope;
no further unexplained cross-store activity.

---

## 14. Expired TLS certificate

**Symptoms**: browsers/clients reject the connection with a
certificate-expired error; `nginx -t` still passes (nginx doesn't
validate expiry itself) but the cert's `notAfter` date has passed.

**Immediate checks**: `openssl x509 -enddate -noout -in
/etc/nginx/tls/fullchain.pem` (or the certbot-managed path — see
`docs/M13_DESIGN.md` Section 4.1) — confirm the actual expiry and how
far past it you are.

**Safe actions**:
- Real deployment (certbot-managed): `certbot renew` (should have
  auto-renewed well before expiry via its own systemd timer — this
  runbook firing at all means that automation itself failed and needs
  investigating, not just the immediate symptom); then `nginx -s
  reload` (NOT a full restart — reload picks up the new cert with zero
  connection drops).
- Self-signed (test/internal use only):
  `deploy/tls/generate_self_signed_cert.sh` to regenerate, then
  `nginx -s reload`.

**Dangerous actions**: do not disable TLS or fall back to plain HTTP
"temporarily" — the HTTP→HTTPS redirect (M13 Phase 3) is a real
security control, not a formality; a brief cert problem is better than
a window of unencrypted traffic.

**Recovery**: a valid, renewed certificate reloaded into nginx.

**Verification**: `openssl x509 -enddate -noout` shows a future date;
`deploy/tests/test_proxy_tls.py`'s own HTTPS checks (run manually
against production if needed) pass; a real browser connection no
longer warns.

---

## 15. High error rate

**Symptoms**: `ERPElevatedRequestErrorRate` fires (>5% of requests
5xx over 5m); `ERPElevatedSlowRequestRate` fires (>20 requests slower
than 1s in 5m).

**Immediate checks**:
- `erp_db_up` and `pg_stat_activity_count` first — a slow/unreachable
  database is the single most likely cause of BOTH a high error rate
  and a high slow-request rate at once (Section 2).
- nginx's own access log
  (`erp_json` format, `deploy/nginx/nginx.conf`) filtered to 5xx
  entries — which specific endpoint(s) are failing, and is it
  concentrated (one broken endpoint) or spread across everything
  (a systemic resource problem)?
- Correlate a specific failing request's `X-Request-ID` (M13 Phase 13)
  against the backend's own log for the actual exception.

**Safe actions**: once the specific cause is identified, apply the
matching runbook above (Section 1 for a crashing process, Section 2
for a DB problem, Section 3 for disk pressure).

**Dangerous actions**: do not restart the backend repeatedly as a
generic "fix" without first checking whether the cause is upstream
(the database) — restarting the app does nothing for a DB-side
problem and just adds a brief additional outage on top of the existing
one.

**Recovery**: whatever the underlying runbook resolves.

**Verification**: `ERPElevatedRequestErrorRate`/
`ERPElevatedSlowRequestRate` clear; the specific endpoint(s) identified
during triage return to normal latency/status-code distribution.

---

## 16. Monitoring component down

**Symptoms**: `ERPMonitoringTargetDown` fires (`up{job!="prometheus"}
== 0` for the named job). Found as a real gap during M13 Phase 17
failure-injection testing: every other alert in this file is computed
from `erp_exporter`'s own scrape (`erp_api_up`, `erp_db_up`,
`erp_migration_current`, etc.) — if `erp_exporter` itself crashes,
those alerts don't fire "down", they simply stop producing fresh
samples, and nothing else in this file would have told an operator the
system had gone blind. Prometheus's own `up` metric is the only signal
that still reflects reality when a target disappears entirely.

**Immediate checks**:
- `curl http://<prometheus-host>:9090/api/v1/targets` — confirm which
  job (`erp_exporter`, `node_exporter`, or `postgres_exporter`) is
  reporting `"health": "down"`, and read its `lastError`.
- `systemctl status erp-exporter` (or the equivalent unit for
  `node_exporter`/`postgres_exporter`) — is the process actually
  running?
- Check the exporter's own log for a crash/exception.

**Safe actions**:
- Restart the named exporter service.
- Confirm Prometheus's `/targets` page shows it healthy again.

**Dangerous actions**:
- Do not treat "no other alerts are firing" as evidence the system is
  healthy while this alert is active — it means the opposite: the
  alerts that would normally tell you are not receiving data.
- Do not silence this alert as a workaround for a flapping exporter;
  fix the exporter's stability instead (a flapping monitoring target is
  itself a reliability problem worth root-causing).

**Recovery**: the named exporter process is running and being scraped
successfully again.

**Verification**: the target shows `"health": "up"` in Prometheus;
`ERPMonitoringTargetDown` clears; spot-check that the alerts that
depend on this exporter (Sections 1–2 for `erp_exporter`) are producing
fresh values again, not just that the target itself is up.
