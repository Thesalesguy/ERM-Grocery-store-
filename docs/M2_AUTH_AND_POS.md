# M2 — Authentication, Catalog/Inventory Management, and POS: Design & Decisions

This document records the M2 milestone's design decisions and explains the
auth, products, inventory, and sales modules added in
`backend/app/modules/*` and `backend/app/api/v1/endpoints/*`. Read
alongside `docs/TECHNICAL_BLUEPRINT.md` and `docs/M1_DATABASE_DESIGN.md`,
which remain authoritative for anything this document doesn't override or
refine.

M2's objective: turn M1's transactional data model into a system a real
cashier and a real store manager can use — authenticated, permission-gated
catalog and inventory management, and a barcode-driven POS whose sale
finalization is safe under concurrent access. Two stages were built in
order: **Stage A** (auth, product catalog, inventory) before **Stage B**
(POS, sale finalization) — Stage B depends on both.

---

## 1. Pre-implementation architecture review

Before writing Stage A, the M1 code was re-read rather than trusted as
correct. Findings, and what was done about them:

- **`products/service.py` misdiagnosed all `IntegrityError`s as
  `DUPLICATE_SKU`.** An invalid `category_id`, `default_supplier_id`, or
  `tax_rate_id` would also raise `IntegrityError` (FK violation) and be
  reported to the client as a duplicate-SKU conflict — wrong status code
  family (409 instead of 422) and a misleading message. Fixed by adding
  `_validate_references()`, which checks referenced rows exist *before*
  the insert and raises a specific `ValidationAppError` (`INVALID_CATEGORY`,
  `INVALID_SUPPLIER`, `INVALID_TAX_RATE`, `INVALID_STORE`) for each case,
  leaving `IntegrityError` handling to actually mean "duplicate SKU."
- **No auth meant every M1 endpoint was unauthenticated.** Confirmed this
  was M1's documented, deliberate deferral (see
  `M1_DATABASE_DESIGN.md` §1.A) rather than an oversight — M2 is exactly
  the milestone that closes it.
- **WAC (`inventory/wac.py`) and `record_movement` were reviewed, not
  changed.** M2 does not alter M1's Weighted Average Cost policy or
  recompute it outside that service — `sales/service.py` calls
  `inventory_service.record_movement()` for the `SALE` movement type the
  same way M1's goods-receiving slice calls it for `PURCHASE_RECEIPT`,
  so cost accounting logic lives in exactly one place.
- **`app.db.base` side-effect import** (all models must be imported
  somewhere before SQLAlchemy resolves cross-module FKs) was already
  fixed in M1's `app/db/session.py`; M2 adds `RefreshToken` to that same
  import list rather than introducing a second registration path.

---

## 2. Authentication architecture

### Why JWT access tokens + rotating opaque refresh tokens (not server-side sessions)

- A session-table-per-request design (look up a session row on every
  request) adds a DB round-trip to every authenticated call for no
  benefit here: this is a single-service API, not a fleet of services
  that need to validate a session without a shared DB.
  A signed JWT lets `get_current_user` verify identity and expiry with no
  DB hit at all.
- Permissions are **not** embedded in the JWT. They're resolved fresh
  from `user_roles`/`role_permissions` on every request
  (`get_user_permissions()`, one JOIN query). Embedding permissions in
  the token would mean a permission change (an Admin revoking a role)
  doesn't take effect until the token expires — up to 15 minutes of a
  stale privilege. The JOIN costs one query per request, which is cheap
  and correct beats fast-and-stale for an authorization decision.
- Short-lived access tokens (15 min) limit the blast radius of a leaked
  token without forcing a re-login every few minutes, because...
- ...a **rotating, opaque, SHA-256-hashed refresh token** stored in an
  httpOnly cookie (`refresh_tokens` table: hash, not the raw token, is
  stored — the same principle as password hashing: a DB read doesn't
  hand an attacker a usable credential) restores a fresh access token
  silently. Rotation (`refresh_access_token()` issues a new refresh
  token and revokes the old one on every use) means a stolen *and reused*
  refresh token is detectable — reuse of an already-revoked token is a
  signal of compromise, even though M2 does not yet act on that signal
  beyond rejecting it (a stated remaining decision, §17).
- The refresh cookie is scoped to `/api/v1/auth` (`path=`), `httpOnly`,
  `samesite=lax`, and `secure` in production/staging — never readable
  from JS, never sent to non-auth routes, and never sent over plain HTTP
  outside local development.

### Password storage

`argon2-cffi` (argon2id), not bcrypt or PBKDF2 — argon2id is the current
OWASP-recommended default for password hashing and resists both GPU and
side-channel attacks better than the alternatives.

### Login timing and enumeration

`authenticate_user()` always runs a password verification — against a
real stored hash for a valid username, or a fixed dummy hash
(`_DUMMY_PASSWORD_HASH`) for an unknown one — before returning failure, so
"unknown username" and "wrong password" take the same wall-clock time and
can't be distinguished by a timing side channel. Both cases log a
`LOGIN_FAILURE` audit event and return the same generic
`INVALID_CREDENTIALS` error.

### Bootstrapping the first user

There is no hardcoded admin account and no public self-registration
endpoint — an internal POS/ERP has no legitimate "sign yourself up" use
case, and a hardcoded credential is exactly the kind of insecure
placeholder M1 and M2's instructions both explicitly forbid.
`backend/scripts/create_admin_user.py` is a one-time, interactive,
operator-run CLI script (hidden password input, confirmed, 12-character
minimum) that creates the first Admin; every subsequent user is created
by an Admin through the application.

---

## 3. RBAC: roles and permission matrix

Single source of truth: `backend/app/modules/auth/permissions.py`. Both
`require_permission()` (the FastAPI dependency every protected route
uses) and migration `e6180fca2ee0` (which seeds `roles`/`permissions`/
`role_permissions`) import the same `ROLE_PERMISSIONS` dict — so the
seeded database data and the code path that checks it can never silently
drift apart.

### Permissions (12)

| Code | Meaning |
|---|---|
| `products.read` | View products, search, barcode lookup |
| `products.write` | Create/update products, manage barcodes, activate/deactivate |
| `inventory.read` | View stock levels and movement history |
| `inventory.adjust` | Create stock adjustments (damage, theft, stocktake correction, etc.) |
| `purchasing.read` | View purchase orders |
| `purchasing.write` | Create/edit purchase orders |
| `purchasing.receive` | Receive goods against a purchase order (M1's WAC-affecting slice) |
| `pos.use` | Operate the POS: scan, cart, finalize a sale |
| `sales.read` | View past sales and receipts |
| `reports.read` | View reports (reserved; no report endpoints exist yet) |
| `users.manage` | Manage user accounts and role assignments (reserved; no endpoint yet — see §17) |
| `audit.read` | View audit log entries (reserved; no endpoint yet — see §17) |

### Roles (5) and their permissions

| Role | Permissions |
|---|---|
| **Admin** | All 12 |
| **Manager** | All except `users.manage` |
| **Cashier** | `products.read`, `pos.use`, `sales.read` |
| **Inventory Clerk** | `products.read`, `products.write`, `inventory.read`, `inventory.adjust`, `purchasing.read`, `purchasing.write`, `purchasing.receive` |
| **Auditor** | Every `*.read` permission (read-only everywhere, including `audit.read`) |

A user's *effective* permission set is the union of every role assigned
to them (a user can hold more than one role via `user_roles`), resolved
fresh on every request — never cached in the JWT (§2).

---

## 4. Product catalog API

Endpoints (all under `/api/v1/products`, all requiring `products.read` or
`products.write` as noted):

- `POST /products` (`products.write`) — create, with `_validate_references()`
  checking store/category/supplier/tax_rate exist first (§1).
- `GET /products` (`products.read`) — list with `search` (name/SKU,
  case-insensitive substring), `category_id`, `is_active` filters.
- `GET /products/barcode/{barcode}` (`products.read`) — the POS scan path;
  see §7 for why this must be indexed.
- `GET /products/{id}` (`products.read`).
- `PUT /products/{id}` (`products.write`) — partial update
  (`exclude_unset=True`); duplicate-SKU and invalid-reference checks apply
  the same as create.
- `POST /products/{id}/activate` / `.../deactivate` (`products.write`) —
  soft-disable rather than delete, since a product referenced by
  historical sales must never actually be removed.
- `POST /products/{id}/barcodes` (`products.write`) — a product can have
  multiple barcodes (case/pack sizes); `products` and `product_barcodes`
  are kept as separate tables (M1's design, unchanged) rather than a
  single barcode column, because a product legitimately has more than one.

### Price changes never affect historical sales

`SaleItem.unit_price_at_sale` (and `unit_cost_at_sale`, `discount_amount`,
`tax_amount`) are captured at sale-finalization time from the *current*
catalog values and never re-read from `products` afterward. Updating a
product's `current_price` via `PUT /products/{id}` has no effect on any
`SaleItem` created before the change. This is directly regression-tested:
`test_price_change_does_not_affect_historical_sale` finalizes a real sale,
updates the product's price through the real endpoint, and asserts the
sale's frozen `unit_price_at_sale` is unchanged.

---

## 5. Inventory API

Endpoints under `/api/v1/inventory`:

- `GET /inventory/stock` (`inventory.read`) — current stock levels per
  product, with an `is_low_stock` flag (`current_qty_on_hand <=
  reorder_point`) and a `low_stock_only` filter.
- `GET /inventory/stock/{id}`.
- `GET /inventory/movements` (`inventory.read`) — the audit trail of every
  stock change, filterable by `movement_type`.
- `POST /inventory/adjustments` (`inventory.adjust`) — the only way to
  directly change `current_qty_on_hand` outside of a sale or goods
  receipt. Every adjustment:
  1. Requires a `reason_code` from a fixed enum (`DAMAGE`, `THEFT`,
     `EXPIRY`, `STOCKTAKE_CORRECTION`, `OTHER`) — never a free-text-only
     reason, so adjustments are reportable.
  2. Rejects a zero `quantity_delta` (a `@field_validator` on the
     schema) — an adjustment that changes nothing isn't a real event.
  3. Locks the product row, calls `inventory_service.record_movement()`
     (the same M1 function goods-receiving and POS sales use) so the
     movement ledger and the `current_qty_on_hand` cache are always
     updated together, and writes a `STOCK_ADJUSTMENT` audit log entry —
     all in one DB transaction. **Stock is never overwritten by an
     absolute "set to N" operation** — every change is a signed delta
     recorded as a movement row, so the ledger can always reconstruct how
     the current quantity was reached.

---

## 6. POS workflow (Stage B, frontend)

`PosPage` is built around a single always-focused text input
(`scannerInputRef`) that a keyboard-emulating barcode scanner types into
and terminates with Enter — no button click, no explicit "scan mode"
required, matching how real barcode scanners behave. Design details:

- **Focus recovery**: the scanner input re-focuses `onBlur` and on any
  click within the page body, so a cashier's stray click elsewhere in the
  UI doesn't leave the scanner "listening" to the wrong element.
- **Manual entry fallback**: a separate search-by-name/SKU box (debounced,
  calls the same `products.read`-gated list endpoint) covers a scanner
  outage or a barcode that won't scan, without needing developer tools or
  the browser URL bar.
- **Unknown barcode**: shows an inline error (`No product found for
  barcode "…"`) and adds nothing to the cart — verified in
  `PosPage.test.tsx`.
- **Client-side subtotal is informational only.** The page computes and
  displays a running subtotal from cart quantity × the product's known
  price, but labels it "Tax and the final total are calculated by the
  server at checkout" — the server, not the browser, is the source of
  truth for every financial figure (§10).
- **Cart editing**: quantity and per-line discount are editable inputs;
  removing a line is always available. Scanning the same barcode twice
  increments the existing line's quantity rather than adding a duplicate
  row.
- **Checkout → receipt**: a successful `POST /sales` replaces the cart
  view with a printable `Receipt` component (browser print, via the
  browser's native print dialog — no fiscal/thermal-printer integration
  yet, see §12); "New sale" clears it and refocuses the scanner.
- **Failed checkout preserves the cart.** An `INSUFFICIENT_STOCK`,
  `INSUFFICIENT_PAYMENT`, or other 409/422 response shows the error
  inline and leaves every cart line and quantity intact, so the cashier
  can correct the payment amount or remove a line rather than re-scanning
  everything.

---

## 7. Sale finalization: the atomic transaction

`sales/service.py::finalize_sale()` is the one function in M2 with the
highest correctness bar, because it is simultaneously a financial ledger
write and an inventory-decrementing write, and must behave correctly when
two cashiers finalize sales against the same stock at the same instant.
It never calls `db.commit()` or `db.rollback()` itself — the caller (the
`POST /sales` route) commits once, after `finalize_sale()` returns
successfully, so the entire sequence below is one database transaction:
any exception anywhere in it leaves nothing behind.

Sequence:

1. Reject an empty cart or an empty payment list (422) before touching
   the database at all.
2. Look up and validate the store exists and is active.
3. Collect the **distinct** product IDs referenced by the cart, sort them
   ascending, and lock each with `SELECT ... FOR UPDATE` in that order.
   Sorting before locking is what prevents deadlock: if two concurrent
   sales both reference products 5 and 9, both transactions try to
   acquire the locks in the same order (5 then 9), so neither can end up
   holding one lock while waiting on a lock the other already holds.
4. Validate every referenced product belongs to the requested store and
   is active (422/404 otherwise) — using the just-locked, guaranteed-
   fresh row data, not a value read before the lock.
5. **Sum requested quantity per product across all cart lines**, then
   verify each product's `current_qty_on_hand` (as read *after* acquiring
   the lock) covers the total. If any product is short, the whole sale is
   rejected (`INSUFFICIENT_STOCK`, 409) **before a single `Sale`,
   `SaleItem`, `Payment`, or `InventoryMovement` row is created** — a
   failed sale leaves nothing in the database at all, by construction,
   not by a subsequent cleanup step.
6. Compute every line's authoritative amounts (unit price, discount, tax)
   from the locked catalog data — never from client-submitted prices
   (§10) — resolving the applicable tax rate via `_resolve_tax()`, which
   checks the rate is active and currently within its effective date
   range (`ConflictError("PRODUCT_TAX_CONFIGURATION_INVALID")` otherwise).
7. Validate the payment total against the computed grand total (§11).
8. Create the `Sale`, its `SaleItem` rows, its `Payment` rows, and one
   `InventoryMovement` per product (via the shared
   `inventory_service.record_movement()`, movement type `SALE`) — all in
   this same transaction.
9. Write a `SALE_COMPLETED` audit log entry.
10. Return the assembled `Sale` for the route to commit and serialize.

### Why this can't oversell — and how that's proven, not just argued

Locking happens in step 3, *before* the stock-sufficiency check in step
5. So if two concurrent sales both target the same product, the second
transaction to reach the `SELECT ... FOR UPDATE` blocks until the first
either commits (its stock decrement now visible) or rolls back (nothing
changed). There's no window where both transactions read the same
"available" quantity and both proceed — the row lock removes it. This is
not merely reasoned about — see §9.

---

## 8. Payment validation policy

Implemented in `finalize_sale()`, immediately after computing the
authoritative grand total:

- The **sum of all payment lines** is compared against the grand total.
- **Underpayment is always rejected** — `INSUFFICIENT_PAYMENT` (409) —
  regardless of payment method. A sale cannot be finalized for less than
  its total.
- **Overpayment is rejected for every non-cash method**
  (`OVERPAYMENT_NOT_ALLOWED`, 409) — a card or mobile-money terminal is
  charged the exact authorized amount; there is no "change" concept for
  those rails.
- **Cash overpayment is allowed and produces `change_due`** (grand total
  minus amount tendered), which the receipt displays. This is the only
  case where tendered amount may exceed the total.
- **No negative payment amounts** — enforced by the Pydantic schema
  (`PaymentCreate`, `Field(gt=0)`), not just the service layer, so a
  negative amount never reaches business logic at all.
- Split tender (multiple payment lines, potentially different methods)
  is supported; the sum-of-all-lines rule applies across the whole set.

---

## 9. Concurrency: the critical evidence

Per the explicit instruction that "89 tests pass" is not itself meaningful
evidence, `backend/tests/test_concurrency.py` exists specifically to prove
the claim in §7, against a **real PostgreSQL** database with **genuinely
independent connections** — not the savepoint-isolated `db` fixture every
other test uses (see that file's module docstring for why: savepoint
isolation would make one thread's setup invisible to another's
connection, which is the opposite of what a concurrency test needs).

Two tests, both using `threading.Barrier` to force two (or five) real OS
threads, each with its own `SessionLocal()` connection, to call
`finalize_sale()` against a single shared product at the same instant:

- **`test_two_concurrent_sales_against_one_unit_of_stock_cannot_both_succeed`**
  — one unit of stock, two concurrent sales each requesting that one
  unit.
- **`test_five_concurrent_sales_against_one_unit_only_one_succeeds`** — the
  same scenario with five concurrent attempts, to make a race-condition
  false-pass (two threads happening to interleave without contending)
  much less likely than with just two.

Both assert, after all threads complete:

- **Exactly one** attempt succeeded.
- Every other attempt failed with `INSUFFICIENT_STOCK` specifically (not
  a deadlock error, not a generic 500) — proving the losing transactions
  were correctly rejected by business logic, not by an unrelated failure.
- The product's final `current_qty_on_hand` is exactly `0` — never
  negative, and never left at a value implying two sales both partially
  succeeded.
- **Exactly one** `SALE`-type `InventoryMovement` row exists for that
  product — no orphaned movement from a sale that didn't actually
  complete.
- **Exactly one** `Sale`, one `SaleItem`, and one `Payment` row exist —
  no orphaned financial records from the losing attempts.

**Result on this branch: passing, verified non-flaky by 5 repeated runs**
against a freshly migrated real PostgreSQL database (see §16 for the
exact validation run). This is the evidence for the "cannot oversell,
cannot leave orphan financial records" requirement — not the aggregate
test count.

---

## 10. Server-side financial recalculation

The frontend's cart never sends a price, discount amount, tax amount, or
line total that the server trusts. `SaleLineCreate` accepts only
`product_id`, `quantity`, and `discount_amount`; every price, tax, and
total is computed server-side from the locked catalog row in
`finalize_sale()` step 6. `test_client_cannot_submit_price_ignored`
verifies this directly: a request that includes a client-supplied price
field has that field silently ignored, and the server's own catalog price
is what's charged.

---

## 11. Receipt

`Receipt` (in `PosPage.tsx`) renders sale number, timestamp, line items
(historical name/SKU/price snapshot — §4), subtotal, discount, tax, grand
total, each payment line, and change due when applicable. It is a normal
DOM element intended to be sent to a physical receipt printer via the
browser's native print dialog. **No fiscal-device or thermal-printer
integration exists yet** — this is the same "basic printable receipt, no
fiscal integration" scope the M2 instructions specified, not a partial or
broken attempt at one.

---

## 12. Audit logging

`audit_logs` (from M1, write-protected at the PostgreSQL role level — see
`M1_DATABASE_DESIGN.md` §1.C) gains these M2 event types, each composed
into the same transaction as the action it records via `db.add()` +
`db.flush()` (never its own commit, so a login failure's audit entry is
the one exception — see below):

| Event | Where |
|---|---|
| `LOGIN_FAILURE` | `authenticate_user()` — logged and **committed immediately** before raising, since the caller's request is about to fail and unwind; without an explicit commit here the audit record would be lost along with everything else in the failed request. |
| `LOGIN_SUCCESS` | `login()` |
| `PRODUCT_CREATED` / `PRODUCT_UPDATED` | `products/service.py` |
| `STOCK_ADJUSTMENT` | `inventory/service.py::create_stock_adjustment()` |
| `SALE_COMPLETED` | `sales/service.py::finalize_sale()` |

`audit_logs` remains non-editable: no endpoint updates or deletes an
audit row, and the PostgreSQL-role-level protection from M1 is unchanged
by M2 (the running application still connects as `erp_app`, which does
not own the table and has no UPDATE/DELETE grant on it).

---

## 13. API error conventions

Every error response has the shape `{"error": {"code": "...", "message":
"..."}}`. This required one fix beyond the custom `AppError` hierarchy
already in `core/exceptions.py`: **FastAPI's own `RequestValidationError`
(422, Pydantic field validation) and Starlette's `HTTPException` (404 on
an unmatched route, 405 on a wrong method) previously returned FastAPI's
default `{"detail": [...]}` shape**, discovered via a real test failure
where a Pydantic-level constraint (`Field(gt=0)`) intercepted a request
before it reached the intended service-layer check. Handlers for both
exception types were added so *every* error response — custom or
framework-raised — normalizes to the same shape. No response ever
includes a raw SQL statement, a stack trace, or an internal exception
message; unexpected exceptions are caught by a final handler and reported
as a generic 500 with no internal detail leaked.

Status code conventions: `401` (no/invalid/expired credentials), `403`
(authenticated but lacking the required permission), `404` (no such
resource), `409` (a state conflict — duplicate SKU/barcode, insufficient
stock, payment mismatch, tax misconfiguration), `422` (request failed
validation before reaching business logic).

---

## 14. Security review

This is a review of what was checked and built, **not a claim of security
certification or complete security assurance** — a genuine third-party
audit would go further than one milestone's self-review can.

- **Authentication**: argon2id password hashing, constant-time-shaped
  login (dummy-hash comparison for unknown usernames, §2), short-lived
  JWT access tokens, rotating opaque refresh tokens hashed at rest.
- **Authorization**: every state-changing and every data-reading endpoint
  (other than `/health` and the auth endpoints themselves) requires a
  specific permission via `require_permission()`, resolved fresh from the
  database per request (§2) — never trusted from a client-supplied claim.
- **Password storage**: hashes only, argon2id, never logged, never
  returned by any endpoint (`CurrentUserResponse`/`LoginRequest` schemas
  don't carry a hash field).
- **Tokens**: access tokens are short-lived and stateless; refresh tokens
  are opaque (not JWTs — no information disclosure if intercepted before
  hashing) and stored hashed, so a database read alone cannot produce a
  usable token.
- **CORS**: `allow_credentials=True` paired with an explicit configured
  origin (`CORS_ORIGINS`), never a wildcard — required for the httpOnly
  refresh cookie to be usable at all, and verified live via `curl -i
  -X OPTIONS` showing the exact configured origin echoed back (§16).
- **CSRF**: the refresh endpoint is cookie-authenticated, but it is
  scoped to `path=/api/v1/auth`, requires no state-changing side effect
  beyond issuing a new token pair (an attacker forging the request gains
  nothing they don't already have), and every *other* state-changing
  endpoint requires an `Authorization: Bearer` header, which a
  cross-site form/script cannot attach — the standard mitigation for an
  API that doesn't rely on cookies for its primary auth.
- **XSS**: React's default JSX escaping is relied on throughout the
  frontend; no `dangerouslySetInnerHTML` or raw HTML injection exists
  anywhere in M2's frontend code.
- **Input validation**: Pydantic schemas validate shape, type, and
  constraints (e.g. `Field(gt=0)` on amounts, a fixed enum for
  `reason_code`) before any request reaches service logic.
- **SQL injection**: all queries go through SQLAlchemy's Core/ORM query
  builder or `sa.text()` with bound parameters (the RBAC-seed migration's
  raw SQL uses `:name`-style bound parameters throughout, never string
  interpolation) — no endpoint or migration builds a SQL string from
  unsanitized input.
- **Secrets**: `SECRET_KEY`/`DATABASE_URL`/`MIGRATIONS_DATABASE_URL` are
  environment-configured (`.env`, not committed with real values); no
  credential is hardcoded in application code (§2's bootstrap script).
- **Error disclosure**: see §13 — no stack trace or SQL text ever reaches
  a client response.
- **Rate limiting**: **implemented in the post-M2 hardening pass** —
  see `docs/M2_HARDENING_AUDIT.md` MEDIUM-1. A per-process, per-IP
  fixed-window limiter now covers `/auth/login` and `/auth/refresh`;
  the in-process (not distributed) nature is a documented, deliberate
  limitation for the current single-instance deployment.
- **Multi-store isolation**: **implemented in the post-M2 hardening
  pass** — see `docs/M2_HARDENING_AUDIT.md` CRITICAL-1. The original M2
  commit trusted client-submitted `store_id` values with no server-side
  comparison against the authenticated user's own store assignment; this
  is now enforced on every read and write across products, inventory,
  and sales.
- **Sale idempotency**: **implemented in the post-M2 hardening pass** —
  see `docs/M2_HARDENING_AUDIT.md` CRITICAL-2. `POST /sales` now
  requires a client-generated `client_transaction_id`, backed by a
  UNIQUE database constraint, so a double-click or a network retry
  cannot create a second sale.
- **Audit protection**: unchanged from M1 (§12) — the running application
  role has no UPDATE/DELETE grant on `audit_logs`, enforced at the
  PostgreSQL level, not just by omitting the endpoints.

---

## 15. Performance

- **Barcode lookup uses an index.** `product_barcodes.barcode` carries a
  `UNIQUE` constraint from M1, which PostgreSQL backs with a unique
  b-tree index automatically — `GET /products/barcode/{barcode}` is
  therefore an index lookup, not a sequential scan, regardless of catalog
  size. This was verified as already true from M1's schema, not
  something M2 needed to add.
- **N+1 avoidance**: `sales/service.py::get_sale()` and `list_sales()`
  use `selectinload(Sale.items)` and `selectinload(Sale.payments)`
  rather than lazy-loading each sale's items/payments in a per-row query
  loop. The `/products` and `/sales` route handlers that denormalize
  product names onto sale items batch-fetch the referenced products in
  one query rather than one query per line item.
- **No premature optimization elsewhere** — no caching layer, no
  read-replica routing, no query result memoization was added; M2's data
  volumes and access patterns don't warrant it yet, and adding it now
  would be exactly the kind of speculative complexity the project's
  guidelines warn against.

---

## 16. Full validation performed for M2

All of the following were run and passed on this branch, in this order,
against the real services (not mocks) wherever the task specified it:

**Backend** (`backend/`, virtualenv active):
- `pytest -q` → **89 passed** (0 failed), against real PostgreSQL
  (`erp_test`/savepoint-isolated per test, per `tests/conftest.py`).
- `ruff check .` → clean (after adding `extend-exclude =
  ["alembic/versions"]` to `pyproject.toml` — Alembic's own
  revision-file boilerplate, present since M0/M1, predates this
  project's style rules and isn't hand-edited for style; fixed as part
  of making the lint gate meaningful, not left silently failing).
- `black --check .` → clean (same exclude applied).
- `mypy app` → clean, no issues in 48 source files.
- `pip-audit -r requirements.txt` → **no known vulnerabilities** in any
  application dependency (`pip-audit` with no `-r` also flags `pip`/
  `setuptools` themselves — the venv's own bootstrap tooling, not a
  shipped dependency — which is not part of this project's dependency
  surface).

**Database** (real PostgreSQL 16, not SQLite):
- `alembic downgrade base` then `alembic upgrade head` — full reversible
  migration cycle, verified clean on a database with real data present
  (this surfaced and fixed a real bug: `e6180fca2ee0`'s downgrade deleted
  `roles` before clearing `user_roles` rows referencing them, which
  fails with a live admin user assigned that role — fixed by deleting
  `user_roles` first).
- **The concurrency tests were re-run 5 times in a row against this
  freshly-migrated database and passed every time** — see §9.

**Frontend** (`frontend/`):
- `npx vitest run` → **13 passed** across 4 files (`App.test.tsx`,
  `LoginPage.test.tsx`, `ProductsPage.test.tsx`, `PosPage.test.tsx`),
  covering: unauthenticated redirect, permission-filtered navigation,
  login failure/success, product search (debounced), product
  deactivate-with-confirmation, barcode scan → cart, unknown barcode
  error, duplicate-scan quantity increment, manual quantity-edit
  subtotal recalculation, full checkout → receipt, and
  insufficient-stock checkout failure preserving the cart.
- `npm run lint` (oxlint) → clean.
- `npm run format:check` (prettier) → clean.
- `npx tsc -b` → clean.
- `npm run build` (vite build) → succeeds.
- `npm audit` → **0 vulnerabilities**.

**Runtime smoke test** (FastAPI started with `uvicorn`, against the
freshly-migrated real database): admin bootstrap script → login → create
product → add barcode → barcode lookup → stock adjustment → finalize a
sale → fetch the sale/receipt by ID — full round trip succeeded, correct
`grand_total`/`change_due` computed server-side. Also verified: no-token
request → 401, garbage-token request → 401 with the standard error shape,
unknown-barcode → 404 with the standard error shape, CORS preflight →
exact configured origin echoed back (not a wildcard).

---

## 17. Remaining business decisions (not resolved by M2, listed rather than silently assumed)

**Note**: this section is the original, as-shipped M2 list. A dedicated
pre-M3 adversarial audit (`docs/M2_HARDENING_AUDIT.md`) subsequently
closed several of these items — rate limiting, multi-store isolation,
sale idempotency, and refresh-token-reuse response are now implemented;
see that document for what changed and what remains genuinely open
(distributed rate limiting, request body size limits, and others, under
"Remaining Accepted Risks").

- **`users.manage` and `audit.read` have no endpoints yet** — the
  permission codes and role assignments exist and are seeded (§3) so
  that RBAC checks and tests can reference them now, but user management
  and audit-log viewing UIs are out of M2's minimal-API-surface scope
  (Section 19 of the task instructions: no premature CRUD).
- **No sale voids/refunds/returns** — M1's schema has `sale_returns`
  tables, but M2 implements only forward sale finalization; a returns
  workflow is future scope.
- **No receipt printer / fiscal device integration** (§11) — deliberately
  out of scope per the task instructions.
- **Multi-currency / multi-tax-jurisdiction** beyond a single
  `tax_rate_id` per product is not addressed — M1's `tax_rates` model is
  used as-is.

---

## 18. Files changed

See the M2 commit for the exact diff. In summary: new `auth`, `sales`
modules end-to-end (models/schemas/service/endpoints); extended
`products` and `inventory` modules (schemas/service/endpoints); two new
Alembic migrations (`d29dbe67b5b1`, `e6180fca2ee0`); a new admin-bootstrap
script; a rewritten frontend auth layer (`AuthContext`, `ProtectedRoute`,
`LoginPage`, API client changes) and rewritten `ProductsPage`,
`InventoryPage`, `PosPage`; new backend tests (`test_auth.py`,
`test_products_api.py`, `test_inventory_api.py`, `test_pos_api.py`,
`test_concurrency.py`) and new frontend tests (`App.test.tsx` rewrite,
`LoginPage.test.tsx`, `ProductsPage.test.tsx`, `PosPage.test.tsx`); this
document.
