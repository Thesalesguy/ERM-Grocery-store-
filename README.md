# Grocery Store ERP/POS

A production-oriented, self-hosted ERP/POS system for a grocery store: barcode POS, inventory
with Weighted Average Cost, purchasing/goods receiving, and Profit & Loss reporting.

The authoritative technical design lives in [`docs/TECHNICAL_BLUEPRINT.md`](docs/TECHNICAL_BLUEPRINT.md)
— requirements, architecture, full database design, accounting formulas, workflows, API design,
security, deployment, and the milestone roadmap. **Read that document first**; this README only
covers running what has been built so far (Milestone M0: project foundation).

## Status

**Milestone M0 — Foundation.** No business features (POS, purchasing, accounting, tax) are
implemented yet. What exists: the modular-monolith project skeleton, database connectivity and
migrations for the identity/access + audit schema, a health-checked FastAPI backend, and a React
frontend shell with placeholder pages for every planned module.

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
    auth/       stores, users, roles, permissions (data model only in M0)
    audit/      append-only audit log (data model only in M0)
    products/   catalog                       -- M1
    inventory/  stock + Weighted Average Cost  -- M2
    purchasing/ suppliers, PO, goods receiving -- M3
    sales/      POS / checkout                -- M4
    accounting/ P&L and reporting              -- M6
    tax/        tax-authority integration      -- M7
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
| `DATABASE_URL` | SQLAlchemy/psycopg3 Postgres connection string | `postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev` |
| `SECRET_KEY` | Reserved for future JWT signing (Section H of the blueprint) — replace before any non-local deployment | dev-only placeholder |
| `CORS_ORIGINS` | Comma-separated list of allowed frontend origins | `http://localhost:5173` |
| `LOG_LEVEL` | Python logging level | `INFO` |

### Frontend (`frontend/.env`)

| Variable | Purpose | Local default |
|---|---|---|
| `VITE_API_BASE_URL` | Base URL the SPA calls for the backend API | `http://localhost:8000` |

## Database Setup

Create a local Postgres user/database (adjust names to match your `.env`):

```bash
sudo -u postgres psql -c "CREATE USER erp_user WITH PASSWORD 'erp_password';"
sudo -u postgres psql -c "CREATE DATABASE erp_dev OWNER erp_user;"
```

Or start Postgres via Docker instead of installing it locally:

```bash
docker compose up -d db
```

## Migrations

From `backend/`, with the virtualenv active:

```bash
alembic upgrade head        # apply all migrations
alembic downgrade base      # roll back everything (dev/test only)
alembic revision --autogenerate -m "describe change"   # create a new migration
```

Migrations are the only way schema changes reach the database — never edit tables by hand.

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
│   └── TECHNICAL_BLUEPRINT.md   # authoritative design document
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory + entry point
│   │   ├── core/                # config, logging, exception handling
│   │   ├── db/                  # SQLAlchemy session + declarative base
│   │   ├── api/v1/               # versioned API routes
│   │   └── modules/              # one package per business module boundary
│   ├── alembic/                  # migrations
│   ├── tests/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/                  # fetch client
│   │   ├── hooks/                # shared data-fetching hook
│   │   ├── components/           # layout + shared UI
│   │   └── pages/                # one placeholder page per module
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
- **Audit log write-protection**: the blueprint calls for revoking `UPDATE`/`DELETE` on
  `audit_logs` from the application's database role. In PostgreSQL, `REVOKE` has no effect on a
  table's *owner* (the owner always retains full privileges), so this requires a second,
  lower-privileged runtime database role distinct from the migration-owning role — not yet
  introduced, since M0 has no code writing to `audit_logs` yet. This is flagged as a decision to
  make before Milestone M5 (returns, voids & audit logging), when real audit writes begin.
- **Local dev database**: examples in this README assume a local Postgres install for running
  tests directly on the host; the same `DATABASE_URL` pattern works identically against the
  Dockerized `db` service.
