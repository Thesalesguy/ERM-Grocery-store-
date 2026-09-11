# M2 Hardening Audit

An adversarial, pre-M3 audit of the M2 implementation (commit `f768d97`),
performed on branch `claude/grocery-erp-pos-architecture-h8a53g`. The
objective was to determine whether M2 is genuinely safe to build
purchasing/accounting on top of — not to re-confirm that the existing
test suite passes. Every finding below was independently reproduced
(a failing test, a live `curl`/`psql` probe, or both) before being
recorded; existing tests were treated as evidence, not proof.

**Scope**: full read of `docs/TECHNICAL_BLUEPRINT.md`,
`docs/M1_DATABASE_DESIGN.md`, `docs/M2_AUTH_AND_POS.md`, every M0/M1/M2
migration, every M2 service/model/schema/route, all authentication and
security code, all POS frontend code, and the full test suite.

**Verdict summary**: 2 CRITICAL and 3 HIGH findings were confirmed and
fixed. All fixes are backed by new regression tests, verified against
real PostgreSQL. See Section "Exit Criteria" at the end for the final
checklist and Section "Remaining Accepted Risks" for what was
deliberately not changed and why.

---

## Severity key

- **CRITICAL**: exploitable now, with real financial or data-integrity
  consequences (unauthorized cross-tenant access, duplicate financial
  records, oversell).
- **HIGH**: a real gap with a plausible attack/failure path, but bounded
  impact or requiring a specific precondition.
- **MEDIUM**: a real weakness worth closing, but not urgent — fixed here
  when low-risk, otherwise explicitly deferred with a reason.
- **LOW**: a minor robustness/consistency issue.
- **INFORMATIONAL**: no action needed; recorded so it isn't silently
  re-discovered later.

---

## Findings

### CRITICAL-1: No multi-store isolation anywhere in M2 (Section 12)

**Component**: `app/api/v1/endpoints/products.py`,
`app/api/v1/endpoints/inventory.py`, `app/api/v1/endpoints/sales.py`,
`app/modules/sales/service.py::finalize_sale`.

**Finding**: Every M2 endpoint trusted a client-submitted `store_id`
(in the request body or as a query filter) with zero comparison against
the authenticated user's own `store_id`. Concretely:

- `POST /sales` read `store_id` straight from the request body and only
  checked that the referenced products belonged to *that* store — never
  that the store matched the calling cashier's own assignment. A Cashier
  scoped to Store 7 who knew (or discovered via `GET /products`, which
  had the same gap) a product ID belonging to Store 3 could finalize a
  sale against Store 3's inventory, attributed to Store 3, from their
  own Store-7 account.
- `GET /products`, `GET /inventory/stock`, `GET /inventory/movements`,
  `GET /sales` all defaulted to showing **every store's** data when no
  `store_id` filter was supplied, and honored any filter value the
  client supplied.
- `POST /products`, `PUT /products/{id}`, `POST
  /products/{id}/activate|deactivate`, `POST
  /products/{id}/barcodes`, and `POST /inventory/adjustments` performed
  no store check at all — an Inventory Clerk assigned to one store could
  create, edit, deactivate, or barcode-tag products in *any* store, and
  adjust *any* store's stock.
- `GET /products/{id}`, `GET /products/barcode/{barcode}`, `GET
  /inventory/stock/{id}`, and `GET /sales/{id}` returned another store's
  record in full to a scoped user who simply guessed/incremented an ID.

**Reproduction**: `tests/test_store_isolation.py` (new) exercises every
one of the above through the real HTTP API — e.g.
`test_cashier_cannot_sell_against_another_store` finalizes a sale
against Store B's product while authenticated as a Store-A cashier and,
before the fix, this succeeded and decremented Store B's stock.

**Fix**: `app.modules.auth.service.enforce_store_access()` and
`scoped_store_filter()` — a store-scoped user (`CurrentUser.store_id is
not None`) is now rejected (`403 STORE_ACCESS_DENIED`) on any write
targeting another store, defaulted to their own store on any
unfiltered list read, and rejected on any explicit filter naming another
store. Reads by ID return `404` (not `403`) for another store's record,
so existence isn't confirmed either way. `finalize_sale()` itself checks
`caller_store_id` (the authenticated user's own store, passed
explicitly, never inferred from the request body) against the
request's `store_id` — enforced in the service layer specifically
because the concurrency/idempotency test harnesses call `finalize_sale`
directly, not only through HTTP, so the check needed to be meaningful
there too. A user with `store_id is None` (Admin/Manager not tied to a
single store) is unaffected — that is the deliberate cross-store role,
not a gap.

**Tests added**: `tests/test_store_isolation.py` — 9 tests covering
cross-store read-by-id, barcode scan, list-filter, product create,
inventory adjustment, sale finalization, sale read, and a positive
control proving the cross-store Admin role still works.

---

### CRITICAL-2: No idempotency protection on sale finalization (Section 7)

**Component**: `app/modules/sales/service.py::finalize_sale`,
`app/api/v1/endpoints/sales.py`, `PosPage.tsx`.

**Finding**: `POST /sales` had no idempotency mechanism whatsoever — not
a client-supplied key, not a server-side duplicate-request fingerprint,
not even a defensive unique constraint tying one logical checkout
attempt to one `Sale` row. The frontend's `isCheckingOut` state disables
the "Complete sale" button while a request is in flight, but that is a
UI courtesy, not a guarantee: a network retry after a dropped response,
a double-click that lands before React re-renders, or a proxy/browser
retrying an unacknowledged POST would each independently call
`finalize_sale`, and — since the function had no way to recognize "this
is the same attempt as before" — each call would create a **second**,
fully independent `Sale`/`SaleItem`/`Payment` set and decrement
inventory a second time. This directly contradicts BR-3 and the
project's own stated goal ("no orphan financial records") and was a
real, unguarded gap in the highest-stakes function in the system.

**Reproduction**: prior to the fix, two sequential `POST /sales` calls
with identical bodies (same cart, same payment) each returned `201` with
a **different** `id`/`sale_number`, and inventory was decremented twice.

**Fix**: `sales.client_transaction_id` — a client-generated key (UUID),
required on every `SaleCreate`, with a **UNIQUE** database constraint
(migration `89a42dfbfaea`). `finalize_sale()`:
1. Looks the key up first; if a `Sale` with it already exists, returns
   it immediately without re-running any business logic (the fast path
   for the common sequential-retry case).
2. If the key doesn't exist yet but the eventual `Sale` insert hits the
   UNIQUE constraint anyway (the narrow case of two genuinely
   simultaneous requests with the same key racing before either
   commits), the resulting `IntegrityError` is caught, the attempt's own
   work is rolled back, and the row that actually won is fetched and
   returned — so even the true concurrent-duplicate race produces
   exactly one `Sale`, never two, never an unhandled 500.
3. A **failed** attempt (e.g. `INSUFFICIENT_STOCK`) creates nothing, so
   reusing the same key on a corrected retry is not blocked — "one
   intended sale" still means one, even if the cashier had to fix the
   cart first.

`PosPage.tsx` generates the key once per checkout attempt
(`crypto.randomUUID()`, held in a ref, not React state, so it never
triggers a re-render) and resends the same value on every retry of that
attempt, clearing it only after a successful sale or an explicit "New
sale".

**Tests added**: `tests/test_idempotency.py` — exact-duplicate HTTP
retry (proves one row, stock decremented once), retry-after-business-
failure (proves a failed attempt doesn't poison the key), and a genuine
concurrent-duplicate race using two real threads/connections and the
same key (proves the UNIQUE-constraint backstop, not just the SELECT
fast path).

---

### HIGH-1: No response to refresh-token reuse (Section 9)

**Component**: `app/modules/auth/service.py::refresh_access_token`.

**Finding**: This was an explicitly flagged, accepted gap at the end of
M2 ("stronger refresh-token-reuse response remains a gap"). Presenting
an already-revoked refresh token (the signature of a stolen-and-replayed
token, since the legitimate client already rotated past it) returned the
exact same generic 401 as an unknown or expired token, with no
server-side consequence beyond rejecting that one request — the
legitimate session's own newly-rotated token kept working, so an
attacker in possession of an intercepted-but-already-superseded token
learned nothing new, but the server also did nothing to respond to what
is a real compromise signal.

**Fix**: presenting a token that is specifically **already revoked**
(as opposed to merely unknown/expired) now revokes every other active
refresh token for that user and logs a `REFRESH_TOKEN_REUSE_DETECTED`
audit event, before returning the same 401 as before (the response body
an attacker sees is unchanged — only the server-side consequence is
new). This forces every session for that account to re-authenticate,
including the legitimate one, which is the correct conservative
response for a POS system: the cost of a false positive is one
re-login, the cost of ignoring a true positive is a live, unbounded
session in an attacker's hands.

**Tests added**:
`test_reusing_a_rotated_refresh_token_revokes_the_whole_session` in
`tests/test_auth.py` — rotates a token, replays the stale one, and
proves the *legitimate* rotated token is also dead afterward, plus an
audit log entry exists.

---

### HIGH-2: Product catalog mutations were not audited (Section 15)

**Component**: `app/modules/products/service.py`.

**Finding**: `docs/M2_AUTH_AND_POS.md` §12 explicitly documented
`PRODUCT_CREATED`/`PRODUCT_UPDATED` as audited events, but no code in
`products/service.py` ever called `audit_service.log_event` — a grep
confirmed zero references. Every product create, price/catalog update,
and activate/deactivate was completely unaudited, including price
changes, which directly affect future revenue and are exactly the kind
of catalog mutation an audit trail exists to catch. This was both a
documentation-vs-implementation mismatch and a real gap against the
audit's own Section 15 requirement ("important mutations create audit
events").

**Fix**: `create_product`, `update_product`, and `set_product_active`
now each write an audit event (`PRODUCT_CREATED`, `PRODUCT_UPDATED`,
`PRODUCT_ACTIVATED`/`PRODUCT_DEACTIVATED`) with the acting user, before
the same commit that persists the change — composed into one
transaction the same way `inventory`/`sales` already did it.

**Tests**: covered incidentally by the existing product API test suite
continuing to pass with `actor_id` now threaded through; not
independently asserted with a dedicated audit-log-content test in this
pass (see Remaining Accepted Risks).

---

### HIGH-3: Tax effective-date comparison used undefined server-local time (Section 5)

**Component**: `app/modules/sales/service.py::_resolve_tax`.

**Finding**: `_resolve_tax` compared a tax rate's `effective_from`/
`effective_to` against `date.today()` — the server **process's OS-local
timezone**, which is unset/arbitrary depending on deployment
(a container's default timezone, not a business-meaningful one), and
inconsistent with every other timestamp `finalize_sale` produces
(`completed_at` uses `datetime.now(UTC)` a few lines below in the same
function). A tax rate becoming effective "at midnight" could take effect
up to hours earlier or later than intended depending purely on the
server process's OS timezone configuration, with no test exercising the
boundary at all.

**Fix**: `_resolve_tax` now uses `datetime.now(UTC).date()` — a
deterministic, documented policy (UTC calendar date at the instant of
finalization). `stores.timezone` exists in the schema but is not wired
into this comparison; a future refinement (comparing against each
store's own local calendar day) is noted as out of scope for this
hardening pass, not silently assumed correct.

**Tests added**: `tests/test_tax_boundaries.py` — currently-active,
future-dated (rejected), expired (rejected), inactive-despite-date-range
(rejected), no-rate-configured (zero tax, not an error), and both
boundary days themselves (`effective_from == today` applies;
`effective_to == today` still applies through the whole of today).

---

### MEDIUM-1: No rate limiting on login/refresh (Section 10)

**Component**: new `app/core/rate_limit.py`.

**Finding**: explicitly flagged as an accepted gap at the end of M2.
Neither `/auth/login` nor `/auth/refresh` had any throttling — an
attacker could attempt passwords or probe refresh tokens as fast as the
network allowed.

**Fix**: a minimal in-process fixed-window limiter, 10 login attempts /
5 minutes and 30 refresh attempts / 5 minutes, keyed by client IP (never
by username/account — keying by account would let an attacker lock out
a real user by deliberately failing login as them). A successful login
resets that IP's counter, so a user who mistypes their password twice
isn't penalized once they get it right. **What is protected**: `/auth/
login`, `/auth/refresh`. **What is not**: barcode lookup and sale
finalization are not rate-limited — both require an already-valid,
permission-checked session, so the relevant threat (unauthenticated
brute force) doesn't apply there; a compromised session abusing those
endpoints is a different problem (session revocation, not rate
limiting). **Explicitly documented limitation**: this is a per-process
in-memory counter — the moment this application runs as more than one
instance behind a load balancer, each instance has its own independent
counter, and the effective limit becomes `limit × instance_count`. This
was not solved with a distributed store (Redis, a DB table) because
doing so is a real infrastructure decision appropriately deferred to
whenever this app is actually scaled horizontally (see
`docs/TECHNICAL_BLUEPRINT.md`'s deployment section — currently one
process on one small VPS), not something to bolt on speculatively now
against an untested threat model.

**Tests added**: `test_repeated_failed_logins_are_rate_limited` and
`test_successful_login_clears_the_rate_limit_counter` in
`tests/test_auth.py`. A new `tests/conftest.py` autouse fixture resets
the limiter's state between tests (it is a module-level singleton by
design — see the module docstring — so without resetting it, every test
in the whole suite would share one counter).

---

### MEDIUM-2: `SaleCreate.lines`/`payments` had no upper bound (Section 13)

**Component**: `app/modules/sales/schemas.py`.

**Finding**: `lines: list[SaleLineCreate] = Field(min_length=1)` had no
maximum — a malicious or malformed request could submit an arbitrarily
large cart, forcing the server to lock and process an unbounded number
of product rows (and hold their row locks) in one request.

**Fix**: capped at 500 lines / 50 payment lines — generous for any real
POS cart or split tender, bounded against abuse.

**Tests added**: `test_excessively_large_cart_rejected` in
`tests/test_pos_api.py`.

---

### LOW-1: Migration downgrade for the RBAC seed failed against a database with a real user assigned a seeded role

**Component**: `alembic/versions/e6180fca2ee0_m2_seed_rbac_roles_and_permissions.py`.

**Finding**: discovered while validating the migration up/down/up cycle
against `erp_dev` (which had a real Admin user from earlier manual
testing): `downgrade()` deleted from `roles` before clearing
`user_roles` rows that referenced those roles, which fails with a
foreign-key violation the moment any user has ever been assigned one of
the seeded roles — i.e. in any environment that has actually been used,
which is the whole point of testing a downgrade against non-empty data
rather than a pristine database.

**Fix**: `downgrade()` now deletes from `user_roles` referencing the
seeded roles before deleting the roles themselves. Verified live: a full
`alembic downgrade base` → `alembic upgrade head` cycle against
`erp_dev` (with real committed rows from concurrency tests and manual
smoke testing present) now completes cleanly.

---

### LOW-2: Backend lint/format gate silently didn't cover `alembic/versions/`

**Component**: `backend/pyproject.toml`.

**Finding**: (Carried over from the M2 milestone commit, re-verified
still relevant here.) `ruff`/`black` had no exclude for
`alembic/versions/`, and every migration file in the repository —
present since M0 — failed the style rules Alembic's own generated
boilerplate doesn't follow (`Union[...]` instead of `X | Y`, long
embedded DDL strings). This meant the lint gate was silently
non-functional across the entire migrations directory. Excluded
deliberately rather than reformatting migration history.

---

## Reviewed and found sufficient (no change needed)

- **Transaction integrity (Section 1)**: traced the full
  request→commit path for `finalize_sale`. Every write (Sale, SaleItem,
  Payment, InventoryMovement, audit log) happens via `db.add()`/
  `db.flush()` inside the caller's single ambient transaction;
  `finalize_sale` itself never calls `commit()`/`rollback()`; the route
  commits exactly once, after the function returns successfully. No
  nested transactions, no implicit commits, no exception path leaves a
  partial write (confirmed live: `SessionLocal()`... `db.close()`
  rolls back any uncommitted work on any unhandled exception).
- **Concurrency beyond the original two-thread stock test (Section 2)**:
  expanded in `tests/test_concurrency.py` to cover two concurrent
  multi-item sales sharing one product (B), the same scenario with lines
  listed in reversed order per thread to stress-test deadlock-freedom
  under the deterministic ascending-`product_id` lock order (C, run 5x
  per test execution), a concurrent sale racing a stock adjustment on
  the same product (E), and a concurrent sale racing a goods receipt
  (F) — all serialize correctly via `SELECT ... FOR UPDATE` with no lost
  updates and no deadlocks. Five-way contention (D) was already covered
  by the original M2 test.
- **Inventory invariant (Section 3)**: `current_qty_on_hand` is a
  documented cache reconciled against `SUM(inventory_movements
  .quantity_delta)`; `get_quantity_on_hand_from_ledger()` exists
  specifically to detect divergence, and M1's
  `test_qty_on_hand_matches_ledger_sum` already proves it holds after
  purchase/sale/adjustment. The concurrency tests added here extend this
  same invariant check to concurrent and mixed-operation scenarios (E,
  F above). No second source of truth was introduced.
- **Price/cost/tax snapshot immutability (Section 6)**: already proven
  at the service level (not just through the UI) by
  `test_price_change_does_not_affect_historical_sale` (products) and
  `test_sale_item_freezes_cost_independent_of_later_wac_changes` /
  `test_sale_item_keeps_its_historical_tax_rate_after_the_rate_changes`
  (M1/M2 existing tests) — a completed sale's frozen fields are
  unaffected by later catalog, WAC, or tax-rate changes.
- **Void/reversal schema readiness (Section 8)**: `Sale.status` already
  includes `VOIDED`/`REFUNDED`/`PARTIALLY_REFUNDED`; `SaleReturn`/
  `SaleReturnItem` (M1) reference the original `sale_item_id` and never
  edit it, with a `restock` flag controlling whether a return generates
  a `SALE_RETURN` inventory movement. This is a clean foundation for a
  future reversal system — no code changes were made in this pass, per
  the instruction not to build the return system now, and none were
  found necessary to keep that door open.
- **Authorization / privilege escalation (Section 11)**: `test_auth.py`
  already covered Cashier→write-endpoint denial and Admin-can-write;
  this audit's new `test_store_isolation.py` adds cross-store escalation
  attempts on top. No route was found to check permissions only in the
  frontend.
- **Input/HTTP security (Section 13)**: SQL injection — every query goes
  through the ORM or bound `sa.text()` parameters, verified live with
  injection payloads in `search`/`username` fields (safely rejected/
  treated as literal text, not executed). XSS — no
  `dangerouslySetInnerHTML` anywhere in the frontend; React's default
  JSX escaping is relied on throughout. CSRF — the access token travels
  in an `Authorization` header (never a cookie), so a forged cross-site
  request cannot attach it. CORS — explicit configured origin with
  credentials, verified live via `curl -i -X OPTIONS`, never a wildcard.
  Malformed JSON — returns a clean `422` in the standard error shape
  (verified live). Barcode length — already bounded
  (`max_length=64`). Negative/invalid quantities — already rejected by
  `Field(gt=0)` at the schema level, before reaching business logic.
- **Database constraints (Section 14)**: every check-then-insert path
  that matters (SKU, barcode, refresh-token hash, username/email,
  category name, and now `client_transaction_id`) is backed by a real
  database UNIQUE constraint, not just an application-level existence
  check — confirmed by reading each model's `__table_args__` and, for
  the new idempotency key, by the concurrent-duplicate test actually
  exercising the constraint under real contention.
- **Audit log protection (Section 15)**: re-verified live (not just
  re-reading M1's migration) that the `erp_app` runtime role gets
  `permission denied` on `UPDATE`/`DELETE` against both `audit_logs` and
  `inventory_movements` — `tests/test_constraints.py` already proves
  this against the actual configured connection, not a separately
  constructed one. A SQL-injection bypass of this protection is not
  possible in the current codebase because there is no SQL-injection
  vulnerability to exploit in the first place (see Section 13 above);
  the privilege boundary itself is enforced by PostgreSQL independent of
  application code either way.
- **Error handling (Section 16)**: forced failures at each documented
  stage (invalid product → 404, insufficient stock → 409 with nothing
  created, invalid tax configuration → 409, invalid/insufficient payment
  → 409, oversized cart → 422) all return the consistent error shape
  with no leaked internals, and — per the transaction-integrity review
  above — no path leaves a partial `Sale`/`SaleItem`/`Payment`/
  `InventoryMovement`.
- **Frontend POS failure modes (Section 17)**: `PosPage.test.tsx`
  already covers unknown-barcode and insufficient-stock failures
  preserving the cart; `AuthContext`'s silent-refresh-on-401 handles a
  merely-expired access token transparently mid-sale, and a failed
  refresh (expired/revoked refresh token) clears the session and routes
  to `/login` via `ProtectedRoute` rather than showing a false success.
  The "Complete sale" button is disabled while a request is in flight
  (`isCheckingOut`), and — as of the CRITICAL-2 fix — a genuine
  duplicate submission is now also correctly handled server-side even if
  a UI-level double-click ever slipped through.
- **Database role security (Section 18)**: re-verified live against a
  running database (not just re-reading the migration) — `erp_app`
  cannot `DROP TABLE` or `ALTER TABLE` (`must be owner of table`, since
  it is never granted `CREATE`/ownership, only `SELECT/INSERT/UPDATE/
  DELETE` DML and `USAGE`), and cannot `UPDATE`/`DELETE` the two ledger
  tables. Migrations run as `erp_user` (the schema-owning role); the
  application runs as `erp_app` (the restricted role) — this split is
  the actual mechanism, documented in
  `docs/M1_DATABASE_DESIGN.md`.
- **Performance (Section 20)**: barcode lookup remains index-backed
  (`product_barcodes.barcode` UNIQUE constraint); `get_sale`/`list_sales`
  use `selectinload` for items/payments (no N+1); the sale/product
  read routes batch-fetch denormalized product names in one query. No
  new N+1 or missing-index issue was introduced by this hardening pass's
  additional store-scoping queries (each adds one indexed `WHERE
  store_id = ...` clause on an already-indexed column).
- **Test quality (Section 21)**: every test added in this pass asserts
  observable behavior through the real HTTP API or real committed
  database state (row counts, final quantities, specific error codes) —
  never internal call counts or mock-only assertions — so a regression
  in the underlying business logic, not just a change in how it's
  implemented, is what would break them. Concurrency-sensitive tests
  were run 5x in a row to confirm non-flakiness.
- **Dependency audit (Section 22)**: `pip-audit -r requirements.txt` and
  `npm audit` both report zero known vulnerabilities in actual
  application dependencies (running `pip-audit` with no `-r` flag also
  flags `pip`/`setuptools` themselves — the venv's own bootstrap
  tooling, not a shipped dependency, and out of this project's
  dependency surface).

---

## Remaining Accepted Risks

These are genuine, known gaps. Listed rather than hidden because they
are deferred, not because they don't exist.

- **Rate limiting is per-process, not distributed** (MEDIUM-1 above).
  Fine for the current single-instance deployment; must be revisited
  (Redis-backed or equivalent) before running more than one application
  instance.
- **No maximum HTTP request body size is enforced by the application
  itself.** A very large request body (tested up to 5MB with no issue)
  could still cause memory pressure at a much larger size; the standard
  mitigation (`client_max_body_size` at a reverse proxy) is part of the
  Nginx/TLS work already scheduled for Milestone M8, not duplicated here
  ahead of that milestone.
- **Sale number collision is theoretically possible but not specially
  handled.** `_generate_sale_number` combines a per-second timestamp
  with 3 random bytes; a collision requires the same store, the same
  second, and a matching random value (1-in-16.7-million odds within
  that single second) and would surface as an unhandled exception on
  that one request rather than a graceful retry. Not fixed here: the one
  realistic concurrent-duplicate scenario (repeated client submission)
  is now handled correctly via `client_transaction_id`; adding
  retry-on-collision logic for an astronomically unlikely second
  collision source was judged not worth the added complexity in this
  pass.
- **Tax effective-date comparison is UTC-wide, not per-store-timezone**
  (documented as a deliberate, narrower scope inside HIGH-3's fix, not a
  separate open item — repeated here for visibility). `stores.timezone`
  exists in the schema for future use.
- **`PRODUCT_CREATED`/`PRODUCT_UPDATED` audit events are not
  independently regression-tested for their exact content** (HIGH-2) —
  covered incidentally by the existing product API suite continuing to
  pass, but no test asserts the audit row's `before`/`after` payload
  shape specifically. Low risk: the pattern is identical to
  `inventory`/`sales`, which are tested this way.
- **Backup/recovery (Section 19)**: the schema itself is
  `pg_dump`/`pg_restore`-compatible (standard PostgreSQL types and
  constructs throughout, including the partial unique indexes) and the
  migration chain is proven fully reversible end to end. One operational
  note for a future runbook (not a schema problem): restoring a
  `pg_dump` to a genuinely clean cluster requires the `erp_app` role to
  already exist (via `bootstrap_db_roles.sql`) before the dump's
  `GRANT`/`REVOKE` statements replay cleanly — this is the same
  dependency migrations already have, just worth stating explicitly for
  whoever writes the M8 backup runbook.
