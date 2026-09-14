-- M13 Phase 6: bootstrap a least-privilege role for postgres_exporter.
--
-- Run this ONCE per PostgreSQL cluster, as a superuser (or any role with
-- CREATEROLE), alongside backend/scripts/bootstrap_db_roles.sql. Idempotent
-- -- safe to re-run.
--
-- Why this exists: postgres_exporter needs read access to PostgreSQL's own
-- statistics views (pg_stat_activity, pg_stat_database, etc.) to report
-- connection counts, replication lag, and similar operational metrics. The
-- built-in `pg_monitor` role grants exactly that -- SELECT on the
-- statistics views and EXECUTE on a handful of read-only monitoring
-- functions -- and nothing else: no table data, no DDL, no write access of
-- any kind. Using erp_user (the schema-owning, migration-running role) or
-- erp_app for this instead would hand a metrics exporter far more privilege
-- than it needs, in direct violation of "do not grant broad privileges for
-- convenience" (found during real testing in this milestone -- the exporter
-- was first pointed at erp_user before this dedicated role was created).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_monitor') THEN
        CREATE ROLE erp_monitor WITH LOGIN PASSWORD 'CHANGE_ME_IN_PRODUCTION';
        RAISE NOTICE 'Created role "erp_monitor" with a placeholder password.';
    ELSE
        RAISE NOTICE 'Role "erp_monitor" already exists; leaving it untouched.';
    END IF;
END
$$;

GRANT pg_monitor TO erp_monitor;
GRANT CONNECT ON DATABASE erp_dev TO erp_monitor;

-- PRODUCTION REQUIREMENT: immediately after running this script against a
-- production (or any non-throwaway) database, rotate the password and
-- update the postgres_exporter DATA_SOURCE_NAME to match:
--
--   ALTER ROLE erp_monitor WITH PASSWORD '<a long, random, secret value>';
--
-- Never leave the placeholder password in place outside local development.
-- erp_monitor cannot read application table data (no table grants were
-- issued) and cannot write anything -- pg_monitor is read-only by design.
