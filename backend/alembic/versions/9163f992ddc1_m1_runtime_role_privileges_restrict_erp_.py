"""m1 runtime role privileges: restrict erp_app on audit_logs and inventory_movements

Revision ID: 9163f992ddc1
Revises: 5914a2603ce7
Create Date: 2026-09-11 06:15:57.994638

Design (see docs/M1_DATABASE_DESIGN.md "Audit-log & ledger write
protection" for the full writeup):

The application is meant to connect to PostgreSQL as a role ("erp_app")
distinct from whatever role owns the schema and runs migrations. That
separation is the only way "revoke UPDATE/DELETE on audit_logs" can mean
anything: in PostgreSQL, a table's OWNER always retains full privileges on
it regardless of GRANT/REVOKE — ownership bypasses the ACL system, it is
not just "a role with all privileges granted". So this migration grants
erp_app broad DML (SELECT/INSERT/UPDATE/DELETE) on every table — the
normal case for most operational tables — and then explicitly carves out
UPDATE/DELETE on the two append-only ledgers the blueprint calls
"never updated or deleted": audit_logs and inventory_movements.

erp_app itself is NOT created here. Role creation is cluster-wide
administration, not app schema history, and this migration runs AS the
owning/migration role, which typically lacks CREATEROLE. See
backend/scripts/bootstrap_db_roles.sql — run once by a superuser before
this migration in any new environment. If erp_app does not exist yet
(e.g. a developer who hasn't run the bootstrap script), this migration
skips the grants with a NOTICE rather than failing, so `alembic upgrade`
still succeeds; the grants can be applied later by re-running this
migration's SQL by hand once erp_app exists (or by downgrading and
re-upgrading past this revision).

IMPORTANT for future migrations: ALTER DEFAULT PRIVILEGES below makes
erp_app automatically get SELECT/INSERT/UPDATE/DELETE on any *new* table
the migration role creates later — the correct default for ordinary
tables. A future migration that adds another append-only/ledger-style
table MUST explicitly REVOKE UPDATE, DELETE ... FROM erp_app on that table
in the same migration, following the pattern here — it is not automatic.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9163f992ddc1"
down_revision: Union[str, None] = "5914a2603ce7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLE = "erp_app"
_LEDGER_TABLES = ("audit_logs", "inventory_movements")


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                RAISE NOTICE 'Role "{_APP_ROLE}" does not exist yet; skipping runtime-role '
                    'privilege grants. Run backend/scripts/bootstrap_db_roles.sql as a '
                    'superuser first, then re-apply this migration''s SQL (see the '
                    'migration file''s docstring).';
                RETURN;
            END IF;

            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {_APP_ROLE}', current_database());
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {_APP_ROLE}', 'public');
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO {_APP_ROLE}',
                'public'
            );
            EXECUTE format(
                'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO {_APP_ROLE}', 'public'
            );

            -- New tables created later by the migration role automatically
            -- pick up the same broad grant — see the module docstring for
            -- why append-only tables must then explicitly opt back out.
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}',
                'public'
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'GRANT USAGE, SELECT ON SEQUENCES TO {_APP_ROLE}',
                'public'
            );
        END
        $$;
        """
    )

    for table in _LEDGER_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                    EXECUTE 'REVOKE UPDATE, DELETE ON {table} FROM {_APP_ROLE}';
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                RETURN;
            END IF;

            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_APP_ROLE}',
                'public'
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'REVOKE USAGE, SELECT ON SEQUENCES FROM {_APP_ROLE}',
                'public'
            );
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM {_APP_ROLE}', 'public'
            );
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM {_APP_ROLE}', 'public'
            );
            EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {_APP_ROLE}', 'public');
            EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {_APP_ROLE}', current_database());
        END
        $$;
        """
    )
