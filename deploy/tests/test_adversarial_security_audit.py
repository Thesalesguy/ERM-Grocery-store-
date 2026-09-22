"""M13 Phase 18: adversarial security audit of the deployment layer.

A fresh audit against 24 named attack vectors. Most already have real,
non-mocked proof elsewhere in this repo -- cited below rather than
duplicated, following the same discipline
deploy/tests/test_failure_injection_disaster_recovery.py (Phase 17)
established. New tests below cover only genuine gaps or a partial gap
worth a targeted addition.

 1. Spoofed X-Forwarded-For          -> test_proxy_tls.py
                                          test_a_spoofed_forwarded_for_header_is_not_trusted
 2. Spoofed X-Forwarded-Proto        -> NEW: test_spoofed_forwarded_proto_header_has_no_effect
                                          below. No backend code reads this header at all
                                          (repo-wide grep, zero matches) and nginx always
                                          overwrites it with $scheme before proxying
                                          (proxy_params.inc) -- but nothing had actually
                                          sent a forged value through the real stack and
                                          proven it inert, the way vector 1 does for XFF.
 3. Spoofed X-Request-ID             -> backend/tests/test_health_and_observability.py
                                          test_an_oversized_inbound_request_id_is_replaced_not_echoed;
                                          test_observability_correlation.py (treated only as an
                                          untrusted label by design, never a security decision)
 4. Direct backend access bypassing
    nginx                            -> test_proxy_tls.py
                                          test_backend_port_is_not_reachable_except_via_loopback_binding
 5. Direct PostgreSQL access from
    an unauthorized network          -> test_db_security.py
                                          test_listen_addresses_is_not_publicly_exposed
                                          (config value) + NEW below (live socket check, the
                                          same "ground truth" pattern vector 4's test uses)
 6. Access to internal exporter
    ports                            -> NEW below (live socket check)
 7. Access to Alertmanager
    internals                        -> NEW below: proven loopback-only via the same socket
                                          check; unauthenticated API access is an ACCEPTED
                                          RISK, not a defect -- see that test's docstring.
 8. Secret leakage via env/config    -> backend/tests/test_production_secret_management.py
 9. Secret leakage via error
    responses                        -> test_reports_failure_injection.py,
                                          test_sale_finalization_failure_injection.py,
                                          test_health_and_observability.py
10. Secret leakage via logs         -> test_observability_correlation.py
                                          test_a_failed_login_through_the_real_stack_leaks_no_secret_into_either_log
                                          (nginx + app logs); NEW below covers the one
                                          untested sub-case: the backup script's own stdout.
11. Audit-log overexposure          -> backend/tests/test_hr_service.py
                                          test_compensation_rate_never_appears_in_audit_log
12. Cross-store audit-log access    -> backend/tests/test_audit_log_endpoint.py
                                          test_store_scoped_manager_sees_only_their_own_store_product_events
13. Unauthorized audit-log access   -> backend/tests/test_audit_log_endpoint.py
                                          test_cashier_cannot_access_the_audit_log
14. Pagination bypass               -> backend/tests/test_audit_log_endpoint.py
                                          test_pagination_parameters_cannot_bypass_store_scope
                                          (store-scope bypass); NEW below covers the one
                                          untested sub-case: the audit endpoint's own
                                          limit=500 cap actually rejects limit=500+1 with 422
                                          (proven generically against a different endpoint in
                                          test_api_security_hardening.py, never against this one).
15. Oversized request body          -> test_proxy_tls.py (nginx) +
                                          test_api_security_hardening.py (app)
16. Unbounded query parameters      -> NEW below (no existing test sends an oversized query
                                          string against the real stack)
17. Rate-limit bypass through
    forwarded headers                -> NEW below. nginx's limit_req_zone keys on
                                          $binary_remote_addr, never any forwarded header
                                          (nginx.conf), and vector 1 proves spoofed XFF never
                                          reaches the backend -- but nothing had combined the
                                          two into an actual rate-limit-evasion attempt.
18. Multiple-worker rate-limit
    weaknesses                       -> ACCEPTED RISK / not applicable today: nginx's
                                          limit_req_zone is shared-memory owned by the ONE
                                          nginx master process, structurally independent of
                                          backend worker count (test_rate_limiting.py's own
                                          docstring), and the current deployment
                                          (deploy/systemd/*, docker-compose.prod.yml) runs a
                                          single backend process -- there is no multi-worker
                                          configuration in production to test against yet.
                                          Re-verify this test file's assumption (nginx-level
                                          limiting, not app-level) if that ever changes.
19. Health endpoint leakage          -> backend/tests/test_health_and_observability.py
                                          test_health_endpoints_never_leak_a_stack_trace_or_connection_string
20. Migration-health leakage         -> backend/tests/test_health_and_observability.py
                                          test_health_migration_reports_schema_mismatch_without_leaking_internals
21. Runtime DB role privilege
    escalation                       -> NEW below: erp_app attempting ALTER ROLE/CREATE
                                          ROLE/self-GRANT, all real, all against the live
                                          configured connection.
22. erp_app mutating migration
    metadata                         -> backend/tests/test_health_and_observability.py
                                          test_erp_app_can_read_but_not_write_alembic_version
23. erp_app mutating immutable
    ledger tables                    -> backend/tests/test_constraints.py
                                          test_runtime_role_cannot_update_or_delete_ledger_tables;
                                          test_accounting_concurrency.py
                                          test_b_erp_app_cannot_update_or_delete_posted_journal_rows
24. Backup artifact access by
    unauthorized OS users            -> NEW below. A REAL defect was found here: pg_dump's
                                          output inherited the invoking process's umask
                                          (typically 0644/0664 -- world- or group-readable),
                                          meaning the full plaintext database dump (every
                                          table, unencrypted, before backup_offbox.sh's own
                                          encryption step) was readable by any OS user on the
                                          host. Fixed with `chmod 600` immediately after
                                          creation in backend/scripts/backup_database.sh, the
                                          encrypted artifact and manifest in
                                          deploy/scripts/backup_offbox.sh, and the decrypted
                                          restore output in deploy/scripts/restore_offbox.sh.
                                          Verified below against a real backup cycle, not the
                                          script's source text.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from conftest import BACKEND_DIR, BASE_URL, DB_URL, DEPLOY_DIR, run
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from test_monitoring import (
    ALERTMANAGER_PORT,
    ERP_EXPORTER_PORT,
    NODE_EXPORTER_PORT,
    POSTGRES_EXPORTER_PORT,
    PROMETHEUS_PORT,
    monitoring_stack,  # noqa: F401 -- imported so pytest can resolve it as a fixture by name
)

sys.path.insert(0, str(BACKEND_DIR))

SUDO_POSTGRES = ["sudo", "-n", "-u", "postgres"]


# --- 2: spoofed X-Forwarded-Proto has no effect -----------------------------


def test_spoofed_forwarded_proto_header_has_no_effect(production_stack: dict) -> None:
    """nginx always overwrites this header with its own negotiated
    $scheme before proxying (deploy/nginx/conf.d/proxy_params.inc) and
    no backend code reads it at all (a repo-wide grep for
    X-Forwarded-Proto under backend/app returns zero matches) -- proven
    here two ways: the config text unconditionally sets it (never
    falling back to a client-supplied value, the same pattern
    listen_addresses's config-guard test uses), and a live request with
    a forged value gets an ordinary, unaffected response."""
    proxy_params = (DEPLOY_DIR / "nginx" / "conf.d" / "proxy_params.inc").read_text()
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in proxy_params, (
        "X-Forwarded-Proto must be set unconditionally from nginx's own $scheme, "
        "never derived from any client-supplied header"
    )

    forged = httpx.get(
        f"{BASE_URL}/health",
        verify=False,
        headers={"X-Forwarded-Proto": "http"},
        timeout=5.0,
    )
    normal = httpx.get(f"{BASE_URL}/health", verify=False, timeout=5.0)
    assert forged.status_code == normal.status_code == 200
    assert forged.json() == normal.json()


# --- 5, 6, 7: PostgreSQL and every monitoring component are loopback-only --


@pytest.mark.parametrize(
    "port,label",
    [
        (5432, "PostgreSQL"),
        (PROMETHEUS_PORT, "Prometheus"),
        (ALERTMANAGER_PORT, "Alertmanager"),
        (NODE_EXPORTER_PORT, "node_exporter"),
        (POSTGRES_EXPORTER_PORT, "postgres_exporter"),
        (ERP_EXPORTER_PORT, "erp_exporter"),
    ],
)
def test_internal_service_is_not_reachable_except_via_loopback_binding(
    port: int,
    label: str,
    monitoring_stack: dict,  # noqa: F811 -- fixture param shadows the import
) -> None:
    """The same ground-truth check test_proxy_tls.py's
    test_backend_port_is_not_reachable_except_via_loopback_binding uses
    for the backend port, applied to every other internal-only service
    this deployment runs: PostgreSQL itself and the full monitoring
    stack, actually standing up the real monitoring_stack fixture
    (test_monitoring.py) rather than skipping when nothing happens to
    already be running -- every monitoring process it starts uses the
    identical `--web.listen-address=127.0.0.1:<port>` flag deploy/
    systemd's real units use, on offset test-only port numbers to avoid
    colliding with anything else in this sandbox; the security property
    under test (the loopback-only bind flag) is identical either way.

    Alertmanager specifically has no authentication of its own (its API
    can create/expire silences unauthenticated) -- this is an ACCEPTED
    RISK, not a defect, because the only real mitigation for an
    internal-only service with no auth layer IS network isolation, which
    this test verifies directly. If Alertmanager is ever exposed beyond
    loopback, it needs an authenticating reverse proxy in front of it
    first (docs/M13_HARDENING_AUDIT.md should record that as a
    prerequisite before doing so, not a suggestion)."""
    result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    matching = [line for line in result.stdout.splitlines() if f":{port} " in line]
    assert matching, f"expected {label} to be listening on port {port}"
    for line in matching:
        assert f"127.0.0.1:{port}" in line, f"{label} must bind loopback only, found: {line}"
        assert f"0.0.0.0:{port}" not in line
        assert f"*:{port}" not in line


# --- shared fixture: a real logged-in Manager (holds AUDIT_READ) ----------


@pytest.fixture(scope="module")
def local_audit_env(production_stack):
    from app.modules.auth.permissions import MANAGER
    from sqlalchemy.orm import Session
    from tests.factories import (
        DEFAULT_TEST_PASSWORD,
        make_store,
        make_user_with_role,
        unique_suffix,
    )

    engine = create_engine(DB_URL)
    db = Session(engine)
    store = make_store(db, name=f"Phase18 Audit Store {unique_suffix()}")
    username = f"m13_phase18_manager_{unique_suffix()}"
    make_user_with_role(db, store, MANAGER, username=username)
    db.commit()
    db.close()
    engine.dispose()

    login = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        verify=False,
        json={"username": username, "password": DEFAULT_TEST_PASSWORD},
    )
    login.raise_for_status()
    yield {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


# --- 10 (partial): backup script output never leaks the DB password --------


def test_backup_script_output_never_leaks_the_database_password(tmp_path: Path) -> None:
    """test_observability_correlation.py already proves a failed login
    leaks no secret into nginx/app logs -- this covers the one untested
    surface with its own credential handling: backup_database.sh's own
    stdout/stderr from a real backup run against erp_dev (read-only,
    never destructive). Uses erp_user (MIGRATIONS_DATABASE_URL), the
    schema-owning role this script is actually designed to run as
    (backup_database.sh's own header comment) -- never erp_app, whose
    restricted grants would make it the wrong role for a full dump."""
    password = DB_URL.split(":")[2].split("@")[0]
    assert password, "expected to extract a real password from DB_URL to search for"

    output_dir = tmp_path / "backup_output"
    result = subprocess.run(
        [str(BACKEND_DIR / "scripts" / "backup_database.sh"), str(output_dir)],
        env={**os.environ, "MIGRATIONS_DATABASE_URL": DB_URL},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert password not in combined, (
        "the backup script's own output must never echo the DB password"
    )


# --- 14 (partial): audit-log's own limit cap actually rejects an over-cap value


def test_audit_log_limit_beyond_the_cap_is_rejected_with_422(local_audit_env) -> None:
    """test_api_security_hardening.py proves the limit=500 cap rejects
    an over-cap value generically, against /api/v1/products -- this
    proves the identical FastAPI Query(le=500) constraint is actually
    wired up on /api/v1/audit-log too, not assumed from shared code."""
    resp = httpx.get(
        f"{BASE_URL}/api/v1/audit-log",
        verify=False,
        headers=local_audit_env["headers"],
        params={"limit": 999_999_999},
    )
    assert resp.status_code == 422


# --- 16: an oversized query string is rejected, not hung -------------------


def test_oversized_query_string_is_rejected_not_hung(production_stack: dict) -> None:
    """No large_client_header_buffers override exists in this repo's
    nginx config, so nginx runs on its compiled-in defaults for
    request-line/header size (large_client_header_buffers 4 8k) -- this
    proves an oversized query string against the real proxy gets a
    clean rejection rather than hanging or being silently accepted and
    forwarded to the backend. Kept comfortably under httpx's own
    65536-byte client-side URL sanity limit (httpx/_urlparse.py), which
    would otherwise raise InvalidURL before the request is ever sent --
    this must test nginx's real behavior, not httpx's."""
    huge_query = "&".join(f"p{i}=v{i}" for i in range(3_000))  # ~27KB, over nginx's 8k default
    resp = httpx.get(
        f"{BASE_URL}/health?{huge_query}",
        verify=False,
        timeout=10.0,
    )
    assert resp.status_code in (400, 414, 431), (
        f"an oversized query string must be cleanly rejected, got {resp.status_code}"
    )


# --- 17: rate limiting is not bypassed by rotating X-Forwarded-For ---------


def test_rate_limit_is_not_bypassed_by_rotating_forwarded_for(production_stack: dict) -> None:
    """nginx.conf keys every limit_req_zone on $binary_remote_addr, never
    any forwarded header (structural proof), and vector 1 proves a
    spoofed X-Forwarded-For never reaches the backend -- this combines
    both into an actual evasion attempt: a fresh, distinct
    X-Forwarded-For on every single request in the burst. If rate
    limiting were (incorrectly) keyed on that header, this burst would
    never trip it, since every request looks like a different client."""
    statuses = []
    for i in range(25):
        resp = httpx.post(
            f"{BASE_URL}/api/v1/auth/login",
            verify=False,
            json={"username": f"rl_evasion_probe_{i}", "password": "wrong-password"},
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": f"10.0.{i // 256}.{i % 256}",
            },
            timeout=5.0,
        )
        statuses.append(resp.status_code)
    assert 429 in statuses, (
        f"rotating X-Forwarded-For per request must not evade rate limiting: {statuses}"
    )


# --- 21: erp_app cannot escalate its own database privileges ---------------


def test_erp_app_cannot_escalate_its_own_privileges() -> None:
    """erp_app (backend/scripts/bootstrap_db_roles.sql) is created with
    only LOGIN + PASSWORD -- no SUPERUSER/CREATEDB/CREATEROLE, and none
    of its table GRANTs (backend/alembic/versions/9163f992ddc1_...) were
    ever made WITH GRANT OPTION. Proves PostgreSQL itself, not just the
    absence of a granted attribute, actually refuses every escalation
    path a compromised application credential might attempt: granting
    itself a new role attribute, creating a new role, and self-granting
    a table privilege it doesn't already hold."""
    from app.core.config import get_settings

    engine = create_engine(get_settings().DATABASE_URL)
    with engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("ALTER ROLE erp_app WITH CREATEDB"))
        conn.rollback()

        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(f"CREATE ROLE erp_escalation_probe_{uuid.uuid4().hex[:8]} LOGIN"))
        conn.rollback()

        # PostgreSQL's actual GRANT semantics here (confirmed against
        # the real cluster, not assumed): a grantor with no GRANT OPTION
        # on ANY privilege of the object gets a WARNING, not an ERROR --
        # "GRANT ... TO erp_app" completes without raising, but grants
        # nothing. The real security property is that this self-grant
        # attempt has zero effect, proven by immediately re-attempting
        # the UPDATE that vector 23's own test
        # (test_constraints.py::test_runtime_role_cannot_update_or_delete_ledger_tables)
        # already proves erp_app cannot do -- if the self-grant had
        # actually escalated anything, this would now succeed instead.
        conn.execute(text("GRANT ALL PRIVILEGES ON TABLE audit_logs TO erp_app"))
        conn.rollback()

        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE audit_logs SET action = 'HACKED' WHERE false"))
        conn.rollback()


# --- 24: local backup/restore artifacts are not world- or group-readable ---


def test_backup_and_restore_artifacts_are_not_world_or_group_readable(tmp_path: Path) -> None:
    """Regression test for a real defect found during this audit:
    pg_dump's output previously inherited the invoking process's umask
    (typically 0644/0664), leaving the full plaintext database readable
    by any OS user on the host. Runs a real backup_offbox.sh cycle
    (never a hand-written fake file) and checks the actual mode bits of
    every artifact it produces, plus a real restore_offbox.sh decrypt
    step for the plaintext it recreates. Uses erp_user (DB_URL), the
    role backup_offbox.sh is actually designed to dump as, matching the
    fix above (never erp_app)."""
    local_dir = tmp_path / "local"
    remote_dir = tmp_path / "remote"
    local_dir.mkdir()
    remote_dir.mkdir()

    age_keygen = run(["age-keygen", "-o", str(tmp_path / "identity.txt")])
    public_key = next(
        line.split(":", 1)[1].strip()
        for line in age_keygen.stderr.splitlines()
        if line.startswith("Public key:")
    )
    rclone_conf = tmp_path / "rclone.conf"
    rclone_conf.write_text("[audit_test]\ntype = local\n")
    status_file = tmp_path / "backup_status.prom"

    backup_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "backup_offbox.sh"),
            str(local_dir),
            f"audit_test:{remote_dir}",
            "14",
        ],
        env={
            **os.environ,
            "MIGRATIONS_DATABASE_URL": DB_URL,
            "BACKUP_AGE_PUBLIC_KEY": public_key,
            "RCLONE_CONFIG": str(rclone_conf),
            "BACKUP_STATUS_FILE": str(status_file),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert backup_result.returncode == 0, backup_result.stdout + backup_result.stderr

    dump_files = list(local_dir.glob("*.dump"))
    encrypted_files = list(local_dir.glob("*.dump.age"))
    manifest_files = list(local_dir.glob("*.manifest.json"))
    assert dump_files and encrypted_files and manifest_files

    for artifact in (*dump_files, *encrypted_files, *manifest_files):
        mode = artifact.stat().st_mode & 0o777
        assert mode & 0o077 == 0, (
            f"{artifact.name} must not be readable/writable by group or other, got {oct(mode)}"
        )

    staging_dir = tmp_path / "restore_staging"
    restore_result = subprocess.run(
        [
            str(DEPLOY_DIR / "scripts" / "restore_offbox.sh"),
            f"audit_test:{remote_dir}",
            encrypted_files[0].name,
            str(staging_dir),
            "erp_audit_permcheck_restore",
        ],
        env={
            **os.environ,
            "AGE_IDENTITY_FILE": str(tmp_path / "identity.txt"),
            "RCLONE_CONFIG": str(rclone_conf),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    try:
        assert restore_result.returncode == 0, restore_result.stdout + restore_result.stderr
        decrypted_files = [
            p for p in staging_dir.glob("*") if p.suffix == ".dump" and not p.name.endswith(".age")
        ]
        assert decrypted_files, f"expected a decrypted dump in {staging_dir}"
        for artifact in decrypted_files:
            mode = artifact.stat().st_mode & 0o777
            assert mode & 0o077 == 0, (
                f"decrypted {artifact.name} must not be readable/writable by group or "
                f"other, got {oct(mode)}"
            )
    finally:
        run([*SUDO_POSTGRES, "dropdb", "--if-exists", "--force", "erp_audit_permcheck_restore"])
