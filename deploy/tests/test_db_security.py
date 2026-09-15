"""M13 Phase 8: network and database security re-audit.

Re-verifies, against the real running PostgreSQL 16 instance this
sandbox provides, the production-topology-specific items
docs/M13_DESIGN.md Section 8 calls out beyond what M12 already proved
(erp_app's table-level privileges, alembic_version protection, and the
ledger tables' immutability are NOT re-tested here -- they already run
as part of the full backend suite via backend/tests/test_constraints.py
and are unaffected by anything in this milestone; duplicating them here
would just be the same assertion twice).

Every test below either reads a real, live `pg_settings` value or
actually exercises the mechanism (kills a real idle transaction,
writes a real connection-log line) rather than inspecting
deploy/postgres/postgresql.prod.conf.snippet's text.
"""

from __future__ import annotations

import time

import pytest
from conftest import DB_URL, SUDO, run
from sqlalchemy import create_engine, text

_PG_LOG_PATH = "/var/log/postgresql/postgresql-16-main.log"


def test_listen_addresses_is_not_publicly_exposed() -> None:
    """Never '*' on a publicly-routable interface (docs/M13_DESIGN.md
    Section 8). This sandbox's own Postgres was found already correctly
    configured this way -- this test guards against a future change
    accidentally widening it, the same "config guard" pattern M12 used
    for the application's own settings."""
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        value = conn.execute(text("SHOW listen_addresses")).scalar_one()
    assert value not in ("*", "0.0.0.0"), (
        f"listen_addresses={value!r} would expose PostgreSQL beyond loopback/the "
        "private network -- see deploy/postgres/postgresql.prod.conf.snippet"
    )


def test_idle_in_transaction_timeout_actually_terminates_a_stalled_session() -> None:
    """Proves the mechanism, not just the config value: a session that
    opens a transaction and then goes genuinely idle (no query in
    flight, not even a long-running one -- pg_sleep() would NOT count,
    since that keeps the session 'active') for longer than
    idle_in_transaction_session_timeout is terminated by PostgreSQL
    itself. Set per-session via SET, not ALTER SYSTEM, so this test
    touches no cluster-wide state and needs no revert step."""
    engine = create_engine(DB_URL)
    raw_conn = engine.raw_connection()
    try:
        raw_conn.autocommit = True
        cursor = raw_conn.cursor()
        cursor.execute("SET idle_in_transaction_session_timeout = '2s'")
        raw_conn.autocommit = False

        cursor.execute("SELECT 1")  # opens an implicit transaction
        time.sleep(3)  # genuinely idle -- no query in flight

        with pytest.raises(Exception, match="idle"):
            cursor.execute("SELECT 1")
    finally:
        try:
            raw_conn.close()
        except Exception:  # noqa: BLE001 -- the connection may already be dead
            pass


def test_connection_and_disconnection_logging_produces_real_log_lines() -> None:
    """log_connections/log_disconnections are 'superuser-backend'
    context -- cannot be set per-session by an ordinary role, only
    cluster-wide (ALTER SYSTEM) by a superuser, taking effect for
    connections established after a reload. Toggled on, exercised
    against a real new connection, and reverted -- this is cluster-wide
    state so every step is wrapped to guarantee the revert runs."""
    run([*SUDO, "-u", "postgres", "psql", "-c", "ALTER SYSTEM SET log_connections = on;"])
    run([*SUDO, "-u", "postgres", "psql", "-c", "ALTER SYSTEM SET log_disconnections = on;"])
    run([*SUDO, "-u", "postgres", "psql", "-c", "SELECT pg_reload_conf();"])
    try:
        time.sleep(0.5)  # let the reload take effect before the next connection
        # A fixed-size tail is not reliable here: other tests/processes
        # in this shared sandbox Postgres generate their own connection
        # traffic concurrently, which can push this test's own lines out
        # of a small fixed window. Record the log's exact size first and
        # read only the bytes appended after that point instead.
        size_before = int(run([*SUDO, "stat", "-c%s", _PG_LOG_PATH]).stdout.strip())

        # NullPool: SQLAlchemy's default pool returns a connection to
        # the pool (keeping the physical socket open for reuse) rather
        # than closing it when `with engine.connect()` exits -- no
        # "disconnection" log line would ever appear until the pool
        # itself is later disposed. NullPool closes the real connection
        # on every checkin, which is what this test needs to observe.
        from sqlalchemy.pool import NullPool

        engine = create_engine(DB_URL, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        time.sleep(0.5)  # let the disconnection line actually get written

        new_content = run([*SUDO, "tail", "-c", f"+{size_before + 1}", _PG_LOG_PATH]).stdout
        assert "connection received" in new_content
        assert "connection authorized" in new_content
        assert "disconnection: session time" in new_content
    finally:
        run([*SUDO, "-u", "postgres", "psql", "-c", "ALTER SYSTEM SET log_connections = off;"])
        run([*SUDO, "-u", "postgres", "psql", "-c", "ALTER SYSTEM SET log_disconnections = off;"])
        run([*SUDO, "-u", "postgres", "psql", "-c", "SELECT pg_reload_conf();"])


def test_application_connection_pool_leaves_headroom_under_max_connections() -> None:
    """erp_app's own connection pool must stay well under max_connections
    even at a realistic worker count, so a runaway backend cannot itself
    exhaust the cluster's connections (docs/M13_DESIGN.md Section 8).
    Reads both sides live: the app's actual configured pool size
    (app.core.config.Settings, not a hardcoded assumption) and
    PostgreSQL's actual live max_connections."""
    from app.core.config import get_settings

    settings = get_settings()
    per_worker = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW

    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        max_connections = int(conn.execute(text("SHOW max_connections")).scalar_one())

    # A documented, conservative worker-count assumption (deploy/systemd
    # and docker-compose.prod.yml both run this application single-
    # process today, but size for headroom against a future multi-worker
    # deploy): 4 workers is a reasonable ceiling for a single-VPS
    # deployment at this project's stated scale.
    assumed_max_workers = 4
    application_max_connections = per_worker * assumed_max_workers

    # Leave room for erp_user (migrations/admin), erp_monitor
    # (postgres_exporter), and a manual incident-response psql session
    # on top of the application's own pool -- not just squeak under the
    # limit.
    assert application_max_connections <= max_connections * 0.8, (
        f"{assumed_max_workers} workers x {per_worker} connections/worker = "
        f"{application_max_connections}, too close to max_connections={max_connections}"
    )
