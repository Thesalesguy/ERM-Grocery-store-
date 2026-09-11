# Grocery Store ERP/POS

A production-oriented, self-hosted ERP/POS system for a grocery store: barcode POS, inventory
with Weighted Average Cost, purchasing/goods receiving, and Profit & Loss reporting.

The authoritative technical design lives in [`docs/TECHNICAL_BLUEPRINT.md`](docs/TECHNICAL_BLUEPRINT.md)
— requirements, architecture, full database design, accounting formulas, workflows, API design,
security, deployment, and the milestone roadmap.
[`docs/M1_DATABASE_DESIGN.md`](docs/M1_DATABASE_DESIGN.md) records the M1 milestone's decisions in
detail (inventory ledger, Weighted Average Cost, the two-database-role privilege model, and what
remains open). [`docs/M2_AUTH_AND_POS.md`](docs/M2_AUTH_AND_POS.md) records the M2 milestone's
decisions (authentication, RBAC, product/inventory APIs, the POS workflow, and the atomic,
concurrency-safe sale-finalization transaction). **Read all three before touching a module**; this
README only covers running what has been built so far.

## Status

**Milestone M2 — Authentication, catalog/inventory management, and POS.** The system is now
authenticated end-to-end (JWT access tokens + rotating refresh tokens, argon2id password hashing,
RBAC enforced on every route) with a real product-catalog UI, inventory-adjustment UI, and a
barcode-driven POS whose sale finalization is one atomic, row-locked database transaction — proven
safe against concurrent oversell and orphan records by dedicated tests against real PostgreSQL (see
`docs/M2_AUTH_AND_POS.md` §9). No purchasing or accounting UI exists yet (scheduled for later
milestones). See `docs/M2_AUTH_AND_POS.md` for the full design and `docs/M1_DATABASE_DESIGN.md` for
the underlying data model it builds on.

## Architecture

```
Browser --> React SPA (Vite, Tailwind) --> FastAPI backend --> PostgreSQL
```

The backend is a single deployable service internally organized into module boundaries that
mirror the blueprint's service design, so each business module can be built independently without
restructuring the app:

```
backend/app/
  core/       configuration, logging, exception handling
  db/         SQLAlchemy engine/session, declarative base
  api/v1/     versioned HTTP routes
  modules/
    auth/       stores, users, roles, permissions + JWT login, refresh tokens, RBAC
    audit/      append-only audit log (login, product, stock, and sale events write to it)
    products/   catalog: categories, products, barcodes — full CRUD + search + barcode lookup API
    inventory/  ledger + Weighted Average Cost — stock-level/adjustment API
    purchasing/ suppliers, PO, goods receiving — data model + one real transactional service
    sales/      atomic, concurrency-safe sale finalization + POS lookup/receipt API
    accounting/ P&L and reporting                                            -- M6
    tax/        tax_rates table now; provider integration                    -- M7
```

A full production topology (Nginx reverse proxy, TLS, Docker Compose production stack) is planned
for Milestone M8; local development here runs the backend and frontend dev servers directly
against a Postgres container (see [Deployment note](#deployment-note) below).

## Technology Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, psycopg3
- **Database**: PostgreSQL 16 (NUMERIC types for all money/quantity columns — never floating point)
- **Frontend**: React 19 + TypeScript, Vite, Tailwind CSS 4, React Router
- **Testing**: pytest (backend, against a real Postgres database), Vitest + Testing Library (frontend)
- **Quality tooling**: ruff + black + mypy (backend), oxlint + Prettier (frontend)
- **Deployment target**: Docker Compose on a single small VPS (~$20/month)

## Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL 16 (either a local install, or via Docker — see below)
- Docker + Docker Compose (for the containerized workflow)

## Environment Variables

Copy the example files and adjust as needed. **Never commit the real `.env` files** — they are
git-ignored.

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

### Backend (`backend/.env`)

| Variable | Purpose | Local default |
|---|---|---|
| `PROJECT_NAME` | Display name used in API docs | `Grocery ERP/POS` |
| `ENVIRONMENT` | `development` \| `staging` \| `production` — disables `/docs` and `/redoc` in production | `development` |
| `DATABASE_URL` | Connection string the **running application** uses. Must be the restricted `erp_app` role — see [Database Setup](#database-setup) below | `postgresql+psycopg://erp_app:erp_app_password@localhost:5432/erp_dev` |
| `MIGRATIONS_DATABASE_URL` | Connection string **Alembic** uses. Must be the schema-owning role (`erp_user`) — falls back to `DATABASE_URL` if unset, which only works if that role owns the schema | `postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev` |
| `SECRET_KEY` | JWT signing key (Section H of the blueprint; see `docs/M2_AUTH_AND_POS.md` §2) — replace before any non-local deployment | dev-only placeholder |
| `CORS_ORIGINS` | Comma-separated list of allowed frontend origins | `http://localhost:5173` |
| `LOG_LEVEL` | Python logging level | `INFO` |

### Frontend (`frontend/.env`)

| Variable | Purpose | Local default |
|---|---|---|
| `VITE_API_BASE_URL` | Base URL the SPA calls for the backend API | `http://localhost:8000` |

## Database Setup

The application connects as a **restricted runtime role** (`erp_app`), separate from the
**schema-owning role** migrations run as (`erp_user`). This is what makes the audit-log/inventory-
ledger write protection real — see `docs/M1_DATABASE_DESIGN.md` §1.C for why a table's owner can
never be restricted by `GRANT`/`REVOKE` alone.

**Local (non-Docker) setup:**

```bash
# 1. Create the schema-owning role and database (as before).
sudo -u postgres psql -c "CREATE USER erp_user WITH PASSWORD 'erp_password';"
sudo -u postgres psql -c "CREATE DATABASE erp_dev OWNER erp_user;"

# 2. Bootstrap the restricted runtime role (idempotent — safe to re-run).
sudo -u postgres psql -d erp_dev -f backend/scripts/bootstrap_db_roles.sql

# 3. Set its password to match your .env (rotate this for anything beyond local dev).
sudo -u postgres psql -d erp_dev -c "ALTER ROLE erp_app WITH PASSWORD 'erp_app_password';"
```

**Docker**: step 2 runs automatically the first time the `db` volume is created (mounted into
`/docker-entrypoint-initdb.d/`) — no manual step needed for a fresh `docker compose up`. For an
*existing* volume that predates this, run it manually:

```bash
docker compose up -d db
docker compose exec db psql -U erp_user -d erp_dev -f /docker-entrypoint-initdb.d/01_bootstrap_db_roles.sql
```

## Migrations

From `backend/`, with the virtualenv active:

```bash
alembic upgrade head        # apply all migrations
alembic downgrade base      # roll back everything (dev/test only)
alembic revision --autogenerate -m "describe change"   # create a new migration
```

Migrations are the only way schema changes reach the database — never edit tables by hand.

## Creating the First Admin User

There is no hardcoded admin account and no self-registration endpoint. After running migrations,
bootstrap the first Admin interactively:

```bash
cd backend
source .venv/bin/activate
python scripts/create_admin_user.py
```

It prompts for username, email, full name, an optional store, and a password (hidden input,
confirmed, 12-character minimum). Every subsequent user is created by an Admin through the
application. See `docs/M2_AUTH_AND_POS.md` §2.

## Running Locally (without Docker)

**Backend:**

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

API is now at `http://localhost:8000` (`/docs` for interactive OpenAPI docs, `/health` and
`/health/db` for health checks).

**Frontend:**

```bash
cd frontend
npm install
npm run dev
```

SPA is now at `http://localhost:5173`.

## Running with Docker

```bash
docker compose up --build
```

This starts `db` (PostgreSQL 16), `backend` (FastAPI with autoreload), and `frontend` (Vite dev
server with hot reload) — bind-mounted so local edits apply immediately. Run migrations inside the
backend container once the stack is up:

```bash
docker compose exec backend alembic upgrade head
```

Then visit `http://localhost:5173` (frontend) and `http://localhost:8000/health` (backend).

### Deployment note

This Compose file is for **local development only** (no Nginx, no TLS, ports published directly).
The production topology — Nginx reverse proxy, Let's Encrypt TLS, a hardened multi-container
deploy — is designed in `docs/TECHNICAL_BLUEPRINT.md` Section L and scheduled for Milestone M8.

## Running Tests

**Backend** (requires a reachable Postgres database matching `DATABASE_URL`):

```bash
cd backend
source .venv/bin/activate
pytest
```

**Frontend:**

```bash
cd frontend
npm run test
```

## Code Quality

**Backend:**

```bash
cd backend
ruff check app tests      # lint
black --check app tests   # format check (drop --check to auto-fix)
mypy app                  # type check
```

**Frontend:**

```bash
cd frontend
npm run lint            # oxlint
npm run format:check    # Prettier (use `npm run format` to auto-fix)
npx tsc -b               # type check
npm run build            # production build verification
```

## Project Structure

```
.
├── docs/
│   ├── TECHNICAL_BLUEPRINT.md    # authoritative design document
│   ├── M1_DATABASE_DESIGN.md     # M1 decisions: ledger, WAC, COGS, privilege model
│   └── M2_AUTH_AND_POS.md        # M2 decisions: auth, RBAC, POS, sale-finalization concurrency
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory + entry point
│   │   ├── core/                # config, logging, exception handling, security (JWT/argon2)
│   │   ├── db/                  # SQLAlchemy session + declarative base
│   │   ├── api/v1/               # versioned API routes
│   │   └── modules/              # one package per business module boundary
│   ├── alembic/                  # migrations
│   ├── scripts/
│   │   ├── bootstrap_db_roles.sql  # one-time, superuser-run erp_app role setup
│   │   └── create_admin_user.py    # one-time, interactive first-Admin bootstrap
│   ├── tests/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/                  # fetch client + per-module API wrappers
│   │   ├── auth/                  # AuthContext, ProtectedRoute/RequirePermission
│   │   ├── hooks/                # shared data-fetching hook
│   │   ├── components/           # layout + shared UI
│   │   └── pages/                # Login, Products, Inventory, POS
│   ├── package.json
│   └── Dockerfile.dev
└── docker-compose.yml
```

## Development Workflow

1. Read `docs/TECHNICAL_BLUEPRINT.md` before touching a module — it is the source of truth for
   schema, business rules, and API shape.
2. Work module-by-module, following the milestone order in the blueprint's Section M.
3. Every schema change goes through an Alembic migration, never manual DDL.
4. Money and quantity columns are always `NUMERIC`/`DECIMAL` — never floating point.
5. Run backend and frontend quality checks (lint, format, type-check, tests) before committing.
6. Commit to the designated feature branch; do not open pull requests unless explicitly asked.

## Decisions Made During M1

See `docs/M1_DATABASE_DESIGN.md` §1 and §11 for the full write-up (authentication timing, Nginx/TLS,
the `erp_app`/`erp_user` privilege split, and a list of business decisions still needing sign-off —
negative-stock default, the purchase-return WAC approximation, and others). Summarized:

- Authentication remains deferred (unchanged from M0's decision) — M1 added no user-facing
  endpoints that need it yet.
- Nginx/TLS remains deferred to M8 (unchanged from M0's decision).
- The audit-log write-protection gap flagged in M0 is now resolved: the application runs as a
  restricted role (`erp_app`) distinct from the migration-owning role (`erp_user`), with
  `UPDATE`/`DELETE` revoked on `audit_logs` and `inventory_movements` — proved live in
  `tests/test_constraints.py`, not just asserted.
- Table/enum naming was refined from the blueprint's original draft (`purchases` →
  `purchase_orders`, `ADJUSTMENT_IN/OUT` → `STOCK_ADJUSTMENT_IN/OUT`, sale statuses, payment
  methods) — `docs/TECHNICAL_BLUEPRINT.md` has been updated to match throughout.

## Decisions Made During M2

See `docs/M2_AUTH_AND_POS.md` for the full write-up (auth architecture, the permission matrix,
sale-finalization transaction design, concurrency proof, payment/receipt policy, security review,
and a list of business decisions still needing sign-off — rate limiting, refresh-token-reuse
response, and others). Summarized:

- Authentication is resolved: JWT access tokens (15 min) + rotating, hashed-at-rest refresh tokens
  in an httpOnly cookie, argon2id password hashing, RBAC resolved fresh from the database on every
  request (never cached in the token) — see `docs/M2_AUTH_AND_POS.md` §2–3.
- The M1 `products/service.py` bug where any invalid foreign-key reference (category, supplier, tax
  rate) was misreported as a duplicate-SKU conflict is fixed — see §1.
- Sale finalization is one atomic transaction using ascending-order row locking to avoid deadlock
  and a pre-flight stock check to avoid oversell, proven safe under real concurrent load against
  real PostgreSQL, not just argued — see §7 and §9.
- A real bug in the RBAC-seed migration's `downgrade()` (deleting `roles` before clearing
  `user_roles` rows that reference them) was found and fixed while validating the migration
  up/down/up cycle against a database with a real user present — see §16.
- The backend lint/format gate (`ruff`, `black`) did not previously exclude Alembic's own
  autogenerated revision-file boilerplate, which predates this project's style rules; this was a
  pre-existing gap (present since M0/M1) that made the gate non-functional across the whole
  `alembic/versions/` directory. Fixed by excluding that directory — see §16.

## Assumptions Made During M0

- **Authentication scope**: the blueprint's original M0 definition included full login/JWT/RBAC
  enforcement. The M0 implementation task explicitly narrowed this: only the data model (`stores`,
  `users`, `roles`, `permissions`, `role_permissions`, `user_roles`) and the module boundary
  (`app/modules/auth/`) are established now. Login, password hashing, JWT issuance, and
  RBAC-enforcing middleware are deferred to a dedicated auth milestone before M1's endpoints need
  real authorization.
- **Nginx/TLS deferred**: the M0 task instructions say to add Nginx "only if required at this
  stage." Since local development works without it (CORS handles cross-origin calls between the
  Vite dev server and FastAPI), the Nginx reverse proxy + TLS termination designed in the
  blueprint's Section L is deferred to Milestone M8 (deployment hardening) rather than built twice.
- **Audit log write-protection**: flagged in M0 as needing a second, lower-privileged runtime
  database role distinct from the migration-owning role (PostgreSQL `REVOKE` has no effect on a
  table's *owner*). **Resolved in M1** — see "Decisions Made During M1" below.
- **Local dev database**: examples in this README assume a local Postgres install for running
  tests directly on the host; the same `DATABASE_URL` pattern works identically against the
  Dockerized `db` service.
