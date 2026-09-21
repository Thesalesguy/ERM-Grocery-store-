# M13 Design — Production Deployment and Operational Infrastructure

## 0. What this milestone is, and is not

M12 closed PASS WITH CONDITIONS at commit `f03b8b7`, migration head
`17fb9afe8d39`, backend 805/805, frontend 36/36. M12 proved the
application's own correctness/recovery/security properties hold; it
explicitly did **not** stand up the infrastructure those properties run
inside of — no reverse proxy, no TLS, no off-box backup transport, no
monitoring stack, no audit-log read endpoint. M13 closes exactly those
gaps and nothing else: **no new business module**, no redesign of
M0–M12's application logic. Every fact this document states about the
existing system (permission matrix, audit event catalog, model schema)
was verified against the actual M12 codebase, not assumed from memory.

## 1. M12 conditions → M13 scope mapping

Pulled verbatim from `docs/M12_HARDENING_AUDIT.md` Sections 13/14/16
("what M12 deliberately did not build" / "known operational risks" /
verdict conditions). Each is mapped to the M13 phase that addresses it;
none are assumed solved merely because M13 intends to address them —
Section 2 onward states, for each, whether the M13 work is **tested**,
**documented**, an **assumption**, or explicitly **not implemented**.

| # | M12 condition (verbatim source) | M13 phase | Disposition |
|---|---|---|---|
| 1 | "No reverse proxy (nginx/TLS) stood up" | Phase 3 | Tested (real nginx + self-signed TLS in this sandbox) |
| 2 | "The in-process rate limiter... remains explicitly single-process-only" | Phase 4 | Tested (nginx `limit_req_zone`, app limiter kept as defense-in-depth) |
| 3 | "Backups are local-filesystem only... off-box/encrypted transport is a documented follow-up, not built" | Phase 5 | Tested (real transport to a local S3-compatible / filesystem remote via `rclone`, encrypted with `age`) |
| 4 | "No monitoring/alerting stack is deployed" | Phase 6 | Tested (real Prometheus + Alertmanager + exporters, alerts fired against real failures) |
| 5 | "No audit-log read endpoint... direct DB access or a future admin UI is the only way to browse it" | Phase 7 | Tested (new scoped endpoint — see Section 7) |
| 6 | "A single Compose process restarting is a real, if brief, outage — no zero-downtime deployment story exists" | Phase 10/11 | Documented + tested failure paths; zero-downtime NOT claimed or built (out of scope — see Section 20) |
| 7 | "The rate limiter's per-process counters mean horizontal scaling silently multiplies the effective brute-force budget" | Phase 4 | Tested — nginx-level limiting is the horizontal-scaling-safe layer |

Two items from `docs/M12_HARDENING_AUDIT.md` are explicitly **not**
M13 scope, carried forward unchanged, because M12 already classified
them as deliberate inherited M1 design decisions, not gaps:
application-layer-only immutability on `payroll_periods`/`sales`/etc.,
and 100% application-layer store isolation (no Postgres RLS). Nothing
in the M13 task brief asks for either to change.

## 2. Sandbox capability audit (what can actually be tested here)

Before designing further, this sandbox's real capabilities were
checked directly rather than assumed:

- `docker` client is present but the daemon cannot start (nested
  container, `ulimit` operation not permitted) — **Docker Compose
  files in this milestone are config-validated (`docker compose
  config`), not daemon-tested.** The live-fire proxy/TLS/rate-limit
  testing below instead runs nginx and uvicorn as native processes
  against the same Postgres this whole project already uses — nginx's
  behavior in front of a FastAPI process is identical whether that
  process is containerized or not, so this is not a weaker proof of
  the proxy mechanics, only of container packaging itself (which
  M12/M0 already proved works for the dev stack).
- `nginx`, `prometheus`, `prometheus-node-exporter`,
  `prometheus-postgres-exporter`, `prometheus-alertmanager`, `certbot`,
  `python3-certbot-nginx`, `rclone`, `age`, `gpg`, `openssl` are all
  installable via `apt-get` and were installed and smoke-tested in
  this sandbox.
- The sandbox's network namespace has **no IPv6** — every service
  config in this milestone binds IPv4 explicitly
  (`listen 0.0.0.0:PORT`, not the packages' IPv6-including defaults),
  discovered by nginx's default config failing outright on first
  start (`socket() [::]:80 failed`).
- No real public domain/DNS exists in this sandbox, so a genuine
  Let's Encrypt HTTP-01/DNS-01 challenge cannot be completed here.
  TLS termination, HTTP→HTTPS redirect, security headers, and
  forwarded-header trust are tested with a locally-generated
  self-signed certificate (openssl); the certbot renewal *procedure*
  is documented and its config validated (`certbot --nginx --dry-run`
  requires live ACME reachability and is **not** run here — see
  Section 3).

## 3. Production topology

```
Internet / store LAN
        │  HTTPS :443 (HTTP :80 redirects to :443)
        ▼
┌─────────────────────────────────────────────┐
│                nginx (reverse proxy)          │
│  - TLS termination (Let's Encrypt in real     │
│    deployment; self-signed for this sandbox's │
│    tests)                                     │
│  - serves built frontend static assets        │
│  - proxies /api/* and /health* to backend     │
│  - security headers, request-size cap,        │
│    connection/timeout limits                  │
│  - limit_req_zone rate limiting (Phase 4)     │
│  - sets X-Forwarded-For/Proto; strips any      │
│    client-supplied X-Forwarded-* first         │
└───────────────────┬───────────────────────────┘
                     │ 127.0.0.1:8000 (loopback only,
                     │ never bound to a public interface)
                     ▼
┌─────────────────────────────────────────────┐
│         backend (FastAPI/uvicorn)             │
│  app.core.http_hardening trusts the proxy's   │
│  forwarded headers only because nginx is the  │
│  only thing that can reach this port at all   │
└───────────────────┬───────────────────────────┘
                     │ private network / localhost,
                     │ port 5432 NEVER published to a
                     │ public interface
                     ▼
┌─────────────────────────────────────────────┐
│              PostgreSQL 16                    │
│  erp_user (owner, migrations only),           │
│  erp_app (runtime, restricted — M1/M8/M12)    │
└─────────────────────────────────────────────┘
```

**Exposed ports** (public interface): 80 (redirect only), 443.
**Internal-only ports**: 8000 (backend, bound to `127.0.0.1` — loopback
in the single-VPS topology, or the Docker Compose private network's
internal DNS name `backend` when containerized — never a
publicly-routable address either way), 5432 (Postgres, bound to
`127.0.0.1`/the Compose private network only), 9090/9093/9100/9187
(Prometheus/Alertmanager/node_exporter/postgres_exporter — Section 6,
bound to loopback, never exposed publicly; an operator reaches them via
SSH tunnel or a VPN, not a public URL).
**Trusted networks**: the reverse proxy is the only process trusted to
set `X-Forwarded-For`/`X-Forwarded-Proto`; everything behind it
(backend, Postgres, exporters) trusts only loopback/the private Compose
network, nothing from the public interface directly.
**Fail-closed**: if nginx cannot reach the backend, it returns 502/504
(never silently serves stale static content as if the API were up); if
the backend cannot reach Postgres, `/health/db` reports 503 (M12
Phase 11, unchanged); if a client tries to reach 8000 or 5432 directly
from outside the host, there is no route to do so — the firewall
expectation (Section 8) is that only 80/443 (and 22 for SSH
administration) are open on the host's public interface at all,
everything else is unreachable regardless of application-layer
behavior.

## 4. Reverse proxy and TLS

Nginx (mature, already the M0 blueprint's own choice — not a new
decision). Config lives at `deploy/nginx/` in this repo, mounted
read-only into the nginx container in `docker-compose.prod.yml`
(Section 10) and used unmodified for this milestone's native-process
tests (Section 2).

- **TLS termination**: nginx `ssl_certificate`/`ssl_certificate_key`,
  TLSv1.2+ only, a conservative modern cipher list.
- **HTTP→HTTPS redirect**: a `:80` server block that only returns 301
  to the HTTPS URL (and serves the ACME HTTP-01 challenge path for
  certbot — Section 4.1) — no other route.
- **Secure headers**: `Strict-Transport-Security`,
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` set at
  the proxy (defense-in-depth on top of the app's own
  `app.core.http_hardening.add_security_headers`, M12 Phase 7 —
  neither layer is asked to trust the other).
- **Request body limit**: `client_max_body_size` set to match
  `settings.MAX_REQUEST_BODY_BYTES` (2MB, M12) — the proxy-layer
  backstop M12 Section 13 named as still missing.
- **Connection/timeout limits**: `keepalive_timeout`,
  `proxy_connect_timeout`/`proxy_read_timeout`/`proxy_send_timeout`
  bounded (60s) so a hung upstream can't hold a worker connection
  forever; `limit_conn_zone` caps simultaneous connections per IP.
- **Proxy buffering**: enabled for typical JSON responses (nothing this
  API returns is a large stream); a large response is capped by
  `proxy_buffer_size`/`proxy_buffers` rather than left unbounded.
- **Forwarded headers / trusted proxy**: nginx sets
  `X-Forwarded-For $remote_addr` and `X-Forwarded-Proto $scheme`
  itself — it does **not** append to a client-supplied value, so a
  client cannot spoof a chain of trusted-looking proxies. The backend
  is reachable only via loopback (Section 3), so even an app-level bug
  that trusted `X-Forwarded-For` blindly could not be reached by
  anything except nginx itself in this topology. Tested directly in
  Phase 3 (Section 8 below): a request presenting a hostile
  `X-Forwarded-Host`/`X-Forwarded-For` is proven not to change what
  the backend does.
- **Static frontend serving**: nginx serves `frontend/dist` (the
  `vite build` output, M0's already-existing build) directly, with a
  SPA fallback (`try_files $uri /index.html`) — no Node process runs
  in production, unlike the dev Compose stack's `Dockerfile.dev`.

### 4.1 Certificate management — DOCUMENTED, not fully tested

`certbot --nginx` (the `python3-certbot-nginx` plugin, already
installed) is the real-deployment procedure: it edits the nginx config
in place to add the `:80` ACME challenge location and the `:443`
`ssl_certificate` directives, and `certbot.timer` (installed by the
package, already present as a systemd timer) handles renewal
automatically twice daily, only actually renewing within 30 days of
expiry. **This sandbox has no public domain, so a real ACME challenge
cannot complete here** — `certbot --nginx --dry-run` requires the
domain to resolve to this host over the public internet, which is not
possible in this environment. This procedure is therefore
**documented**, not tested, and stated as such in the final audit
rather than claimed as proven. What IS tested (Section 8): the nginx
config's TLS termination, redirect, and header behavior work correctly
with *a* certificate (self-signed), which is the entire surface certbot
would otherwise populate — swapping a self-signed cert for a
Let's-Encrypt-issued one changes zero nginx logic.

**Renewal failure behavior (documented procedure)**: if `certbot`
renewal fails silently for 30+ days, the certificate expires and every
browser blocks the site outright — this is exactly why Phase 6's
monitoring includes a certificate-expiry check (Section 6) as a
real, tested alert condition, not just a runbook entry.

## 5. External services / tools disclosure

Per the mandated format, for every external/cloud tool this design
names:

**Let's Encrypt**
- Access Mode: Guest Allowed (no account; ACME protocol only)
- Onboarding Friction: None
- Free Tier Caps: 50 certificates/domain/week; no cost ever
- Next Tier Cost: N/A (always free)
- Local/Open-Source Alternative: a self-signed certificate (used for
  this milestone's actual tests) or a private/internal CA (`step-ca`,
  not used here — self-signed is simpler and sufficient for a
  single-domain VPS)

**A backup destination requiring an account** (S3-compatible object
storage — AWS S3, Backblaze B2, or a self-hosted MinIO)
- Access Mode: Account Needed (a real cloud destination) — but the
  Local/Open-Source Alternative below is what this milestone actually
  builds and tests
- Onboarding Friction: Backblaze B2 — email verification only, no
  credit card required for the free tier; AWS S3 — credit card
  required at signup even for the free tier
- Free Tier Caps: Backblaze B2 — 10GB storage free forever; AWS S3 —
  5GB for 12 months only
- Next Tier Cost: Backblaze B2 — $6/TB/month; AWS S3 — ~$23/TB/month
  (US East, storage only, excludes egress/requests)
- Local/Open-Source Alternative: **MinIO**, a self-hosted
  S3-API-compatible object store, run on a second cheap VPS or even a
  spare disk on a different physical machine than the primary
  database server — no account, no signup, no credit card. This is
  what "off-box" actually requires (a copy on different physical
  infrastructure than the primary DB), and MinIO satisfies that
  without any external account. **This milestone tests the transport
  mechanism (`rclone` talking to an S3-compatible endpoint) against a
  local filesystem remote** (Section 5, Phase 5) since standing up a
  second physical host is outside a single-sandbox session's reach —
  the `rclone` command and encryption step are identical whether the
  remote is MinIO on a second VPS or the local directory used here;
  only the `rclone.conf` endpoint URL changes.

**UptimeRobot** (or equivalent free external uptime pinger, mentioned
in `docs/TECHNICAL_BLUEPRINT.md` Section L as an option)
- Access Mode: Account Needed (email signup)
- Onboarding Friction: Email verification only, no credit card
- Free Tier Caps: 50 monitors, 5-minute check interval
- Next Tier Cost: $7/month for 1-minute intervals
- Local/Open-Source Alternative: **Prometheus blackbox-style HTTP
  check + Alertmanager**, run on the same VPS (or a second cheap one
  for genuine external reachability testing) — what this milestone
  actually builds (Section 6). No account anywhere.

No external service is required for this milestone's core deliverable
— every piece is installable via `apt-get` with no signup, matching
the project's low-cost/open-source objective. External services above
are documented as options an operator MAY choose later, never as
something this milestone depends on.

## 6. Monitoring and alerting

Prometheus (single static binary, its own package, no daemon
dependencies beyond what's already needed) + Alertmanager + two stock
exporters (`node_exporter` for host metrics, `postgres_exporter` for
DB metrics, both running as the least-privilege `erp_monitor` role —
`pg_monitor` grants read-only access to PostgreSQL's own statistics
views and nothing else) + one small custom exporter,
`deploy/prometheus/erp_exporter.py` (~250 lines), for everything the
two stock exporters cannot see: the application's own `/health`,
`/health/db`, `/health/migration` endpoints (three gauges), per-status-
class request counts, 429 counts, and slow-request counts parsed by
tailing nginx's own JSON access log incrementally (a real byte-offset
tail, not a re-parse every cycle), and authentication-failure counts
(`LOGIN_FAILURE`/`REFRESH_TOKEN_REUSE_DETECTED`) polled directly from
the `audit_logs` table. No Grafana — Prometheus's own expression
browser and Alertmanager's own UI are sufficient for this milestone's
scale; adding a dashboarding layer would be exactly the "unnecessarily
large observability platform" the task warns against.

Monitored: API availability, DB availability, migration health (M12's
three health endpoints), disk space (`node_exporter` filesystem
metrics), memory/CPU (`node_exporter`), backup success/failure (a
Prometheus textfile-collector metric written by `backup_offbox.sh`
itself, Section 5 — picked up by `node_exporter --collector.textfile.
directory`, not duplicated by a second poller), request error rate and
slow requests (`erp_exporter.py` tailing the app's structured JSON
access log — see Section 13), authentication failures (`erp_exporter.
py` polling `audit_logs` directly), rate-limit events (429 counts from
the same access-log tail — simpler and more precise than parsing
nginx's free-text error log for "limiting requests" lines, and it is
the same log already being tailed for status-class counts), database
connection exhaustion (`postgres_exporter`'s `pg_stat_activity`
metrics summed across all databases/states against `max_connections`
— found during live testing that the raw per-database/state metric
needs an explicit `sum()`, not a per-series comparison), application
restart/crash (a Prometheus `up{job="erp_exporter"}` gap, and a
"was down in the last 5m but is up now" rule that catches a
restart-then-recover cycle a simple `up==0` alert would miss).

Every alert rule defined in `deploy/prometheus/alerts.yml` states its
condition, severity, and expected operator action inline as
annotations — reproduced in full in `docs/M13_HARDENING_AUDIT.md`
rather than duplicated here. Live-fire tested end-to-end (real
PostgreSQL stopped/started, a real corrupted backup-status file) in
`deploy/tests/test_monitoring.py`; see that file's module docstring for
exactly what is re-verified on every test run versus what was proven
once interactively (the production `for:` durations themselves — 1m/2m/
5m — would make the automated suite too slow to re-run on every
invocation, so the automated tests use a copy of the same rules with
only `for:` shortened).

## 7. Audit log read endpoint

**Decision: build it.** `AUDIT_READ` (`audit.read`) and the `AUDITOR`
role have existed, unused by any route, since M2. `MANAGER` — a
routinely store-scoped role in real usage — also already holds
`AUDIT_READ` in the existing permission matrix (verified directly in
`app/modules/auth/permissions.py`, not assumed), so store-scoping this
endpoint correctly is a real, present requirement, not a hypothetical
one. This is a bounded, single, well-specified read endpoint — not a
generic admin UI.

`GET /api/v1/audit-log`, gated by the existing `AUDIT_READ` permission
(no new permission needed — it already exists and already means
exactly this). Filters: `date_from`/`date_to` (bounded to a sane max
range the same way M11's reports are, per M12 Section 6's finding that
a wide range doesn't need a hard cap since the query is already
bounded in cost by an index), `action` (exact match against the known
action catalog), `entity_type` (exact match), `actor_id`. No free-text
filter of any kind — every filter is a structured, typed parameter
FastAPI validates before it reaches a query, so there is no string
concatenated into SQL for a client to manipulate. Pagination via the
same `Query(default=50, ge=1, le=500)` pattern M12 Phase 7 already
applied everywhere else.

**Store scoping** (`AuditLog` has no `store_id` column itself — adding
one would mean touching every one of the ~35 existing `log_event(...)`
call sites across the codebase, which is exactly the kind of M0–M12
redesign this milestone must not do). Instead, for a store-scoped
caller (`current_user.store_id is not None`), the endpoint resolves
each request's allowed `entity_id`s per `entity_type` via a lookup of
the already-existing store-bearing column on that entity's own table:

| entity_type | resolution |
|---|---|
| `product`, `sale`, `sale_return`, `purchase_order`, `purchase_invoice`, `purchase_return`, `goods_receipt`, `stock_adjustment`, `stock_count`, `payroll_period`, `attendance_record`, `supplier_payment`, `supplier_credit_note`, `journal_entry`, `inter_store_transfer_receipt` | direct `store_id` column on that table |
| `inter_store_transfer` | `from_store_id` OR `to_store_id` matches |
| `replenishment_plan` | `destination_store_id` OR `source_store_id` matches |
| `stock_count_line` | joined through `stock_count_id` → `stock_counts.store_id` |
| `employee` | joined through the employee's most recent `employment_assignments.store_id` |
| `user` | the target user's own `users.store_id` |
| `department`, `position`, `supplier`, `supplier_product` | **no store dimension exists** — excluded entirely from a store-scoped caller's results (never guessed, never leaked) |

A cross-store caller (`current_user.store_id is None` — Admin/Auditor
in the existing matrix) sees everything, matching every other
list endpoint's existing `scoped_store_filter`/`enforce_store_access`
convention (M2 hardening audit Section 12, unchanged).

**Sensitive-data protection**: `before_state`/`after_state` JSON blobs
are returned as-is — M12 Phase 9 already proved no audit record
anywhere contains a password/hash, and M10 already made a deliberate,
documented choice to exclude raw payroll dollar figures from audit
entries — this endpoint doesn't change what's IN an audit record, only
who can read which rows. `ip_address`/`user_agent` are visible only to
callers who already hold `AUDIT_READ` (Admin/Manager/Auditor), the same
population that can already see a user's full name, store assignment,
and login history via existing endpoints — not a new exposure.

## 8. Network and database security

Re-audits, against the actual running Postgres in this sandbox, every
item M12 already proved plus the new production-topology-specific
ones: bind address (`listen_addresses` — production should be
`localhost` or the Compose private network, never `*` on a
publicly-routable interface), firewall expectation (only 80/443/22
open publicly — Section 3), `erp_app` privilege re-verification
(alembic_version SELECT-only, ledger tables INSERT/SELECT-only —
M12 Sections 2.2/8, re-run here rather than re-derived), connection
limits (`max_connections`, and `erp_app`'s own connection pool size
kept well under it so a runaway backend can't itself exhaust
connections), idle connection handling
(`idle_in_transaction_session_timeout`), and database logging
(`log_connections`/`log_disconnections` for a real audit trail
independent of the application's own `audit_logs` table — a second,
DB-level record an attacker who somehow bypassed the app couldn't
also suppress).

## 9. Production secret management

Extends, does not replace, M12 Phase 2's fail-closed
`Settings._forbid_insecure_production_config` validator. Production
secrets are supplied via the deployment host's environment (a
root-owned, `chmod 600` `.env` file outside the git working tree, or
Docker/systemd environment injection) — never committed, never baked
into an image layer (the `Dockerfile`s `COPY . .` a source tree that
is itself `.gitignore`d for `.env`, and the image build never receives
secrets as build args, so they cannot land in an image layer or a
`docker history` output), never in a frontend bundle (the frontend
only ever holds `VITE_API_BASE_URL`, a public value, not a secret),
never in logs (M12's `RedactSensitiveFieldsFilter`, unchanged), never
in a migration file (every migration in this repo is schema/privilege
DDL only — verified by grep, not assumed), never in a test fixture
(`tests/factories.py`'s `DEFAULT_TEST_PASSWORD` is a fixed,
publicly-known test-only string, structurally incapable of being a
real credential since it's hardcoded identically across the whole
suite).

## 10. Deployment and migration procedure

`docker-compose.prod.yml` (an overlay, per M12's own Section 2 design —
the dev `docker-compose.yml` is never modified) plus
`deploy/scripts/deploy.sh` encoding the seven-step procedure the task
requires: backup → health check → migration → application deployment →
smoke tests → verification → rollback decision. Full detail and the
failure-mode table (migration fails / app fails / health check fails /
DB unavailable / new app against old schema) in Section 10 of
`docs/M13_HARDENING_AUDIT.md`, next to the evidence, matching M12's own
established documentation discipline.

**Zero-downtime is explicitly NOT claimed.** A single Compose stack
means `docker compose up -d --build backend` restarts the one backend
process — a real, brief (single-digit seconds) gap where nginx returns
502 until the new container passes its health check. This is stated
plainly, not hidden behind vague language.

## 11. Rollback and version compatibility

Tested directly (Phase 11, reusing and extending M12's real
up/down/up migration-cycle testing pattern against the dedicated
`erp_test` database — never `erp_dev`): current-schema startup,
post-migration startup, a migration that fails partway (M12 Phase 14
already proved this rolls back cleanly at the DB level — Phase 11
extends it to "does the app refuse to start against a half-migrated
DB", not just "does Alembic roll back"), app startup failure, DB
unavailable at startup, and — the compatibility boundary the task asks
to be stated explicitly — **this codebase does not maintain N-1
schema compatibility**: no migration in M0–M12 was written with an
"old code still runs against new schema" contract (columns are
added/renamed directly, not shadow-column-migrated), so a rollback of
the *application* to a previous version while the *schema* stays
forward-migrated is **not safe in general** and is stated as such
rather than tested-and-passing. What IS safe and tested: a schema
*downgrade* (Alembic `downgrade`) to the exact previous migration,
paired with the matching previous application version — the only
rollback path this codebase actually supports, unchanged from every
milestone's own migration discipline since M1.

## 12. Store connectivity / edge failure

No offline POS mode is built (not asked for, would be a business
feature). What's tested is the existing idempotency machinery's
authority under proxy-layer failure conditions it has never been
tested against before: a request that times out at the nginx layer
(`proxy_read_timeout`) after the backend already committed, and a
duplicate retry arriving after a connection reset — both must resolve
to "the client sees the outcome of the original attempt," reusing
`client_transaction_id` exactly as M12's
`test_sale_finalization_failure_injection.py` and the
pre-M12 `test_idempotency.py` suite already proved at the
application layer. Phase 12 adds the proxy-layer dimension: the
timeout/retry happens as a real HTTP round trip through nginx, not a
direct service-layer call.

## 13. Observability and correlation

M12 Phase 11 already built the correlation-ID middleware and the
redacting log filter. M13 Phase 13 audits both under the new topology
rather than rebuilding: does nginx forward (not strip) `X-Request-ID`;
does the access log nginx itself writes carry a value that can be
cross-referenced against the app's own structured log line for the
same request; do concurrent requests genuinely receive distinguishable
IDs through the proxy (not just directly against uvicorn, which M12
already proved). No new secret-redaction surface is introduced by
this milestone (nginx access logs, by design, log request line/status/
timing — never headers or bodies — so no credential can appear there
either).

## 14. Operational runbooks

`docs/M13_PRODUCTION_RUNBOOKS.md`, one section per required scenario, each with
symptoms/immediate checks/safe actions/dangerous actions/recovery/
verification. Never a first-line "mutate accounting/inventory tables
directly" step — every financial/inventory runbook's recovery path
routes through the same `scripts/integrity_snapshot.py` (M12) and the
existing service-layer reversal/reconciliation functions the
application itself already exposes.

## 15. Performance under production topology

Extends `backend/scripts/performance_baseline.py` (M12) to run the
same measurements through the real nginx+TLS stack built in this
milestone rather than a direct DB session — comparing against M12's
recorded baseline to see what the proxy/TLS hop actually costs, not
guessing.

## 16. Cost assumptions

Unchanged from M0's own stated target: a single ~$20/month VPS (2 vCPU/
4GB), Section 5 above. Nothing in this milestone's design requires a
second host, a managed service, or a paid tier of anything — the
MinIO off-box-backup alternative would need a second cheap VPS
(~$5–6/month at Hetzner/DigitalOcean's low tier) for a *genuinely*
off-box copy in a real deployment, stated honestly as an added cost
an operator should budget for, not hidden.

## 17. Known limitations stated up front

1. Docker Compose itself is not daemon-tested in this sandbox
   (Section 2) — config-validated only.
2. Real Let's Encrypt issuance/renewal is not tested (Section 4.1) —
   documented procedure only.
3. Off-box backup transport is tested against a local filesystem
   `rclone` remote, not a genuinely separate physical host (Section 5)
   — the transport mechanism is proven, physical off-box-ness is not.
4. No zero-downtime deployment (Section 10) — unchanged from M12's own
   statement, now with a concrete measured restart gap.
5. Application-level rollback is not safe across a schema migration in
   general (Section 11) — only paired schema+app downgrade is.
6. No Grafana/dashboarding layer — Prometheus/Alertmanager's own UIs
   only, a deliberate scope choice against "unnecessarily large."
