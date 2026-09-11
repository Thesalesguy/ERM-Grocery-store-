-- Bootstrap the restricted application runtime role.
--
-- Run this ONCE per PostgreSQL cluster, as a superuser (or any role with
-- CREATEROLE), BEFORE running Alembic migrations against a new environment.
-- It is idempotent — safe to re-run.
--
-- Why this exists (docs/M1_DATABASE_DESIGN.md "Audit-log & ledger write
-- protection"): the application connects as a role distinct from the one
-- that owns the schema and runs migrations. That separation is what makes
-- "revoke UPDATE/DELETE on audit_logs and inventory_movements from the
-- app role" mean anything — a table's OWNER can never be blocked from
-- modifying it by GRANT/REVOKE, no matter what privileges are revoked
-- (ownership bypasses the ACL system entirely in PostgreSQL). So the
-- owning/migration role (e.g. erp_user) intentionally keeps full rights;
-- only the separate erp_app runtime role is restricted.
--
-- Role/privilege management is cluster-wide administration, not app
-- schema history, which is why this lives in a plain script rather than
-- an Alembic migration (Alembic migrations run AS the owner role and
-- cannot grant CREATEROLE to themselves). The actual GRANT/REVOKE wiring
-- for erp_app's table privileges lives in the Alembic migration
-- (m1_runtime_role_privileges) and runs AFTER this script, since it
-- needs erp_app to already exist.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
        CREATE ROLE erp_app WITH LOGIN PASSWORD 'CHANGE_ME_IN_PRODUCTION';
        RAISE NOTICE 'Created role "erp_app" with a placeholder password.';
    ELSE
        RAISE NOTICE 'Role "erp_app" already exists; leaving it untouched.';
    END IF;
END
$$;

-- PRODUCTION REQUIREMENT: immediately after running this script against a
-- production (or any non-throwaway) database, rotate the password and
-- update DATABASE_URL to match:
--
--   ALTER ROLE erp_app WITH PASSWORD '<a long, random, secret value>';
--
-- Never leave the placeholder password in place outside local development.
