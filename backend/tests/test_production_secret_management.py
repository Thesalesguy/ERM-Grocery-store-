"""M13 Phase 9: production secret management.

Extends, does not replace, test_config_safety.py's fail-closed
production Settings validator (that file already covers: missing/weak/
placeholder SECRET_KEY, placeholder and now unrotated-dev-password
DATABASE_URL/MIGRATIONS_DATABASE_URL, wildcard CORS). This file covers
the remaining, genuinely different concerns docs/M13_DESIGN.md Section
9 lists: a WRONG (not just a known-insecure) database credential at
connection time must fail closed without leaking the credential
anywhere, and secrets must never reach a Docker image layer, a
migration file, or the frontend bundle -- verified against the real
files in this repository, not merely asserted.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"


def test_a_wrong_database_password_fails_closed_without_leaking_the_password() -> None:
    """Not a KNOWN-insecure password (that's test_config_safety.py's
    job) -- an arbitrary WRONG credential, the shape of a typo'd or
    stale secret at deploy time. Must raise cleanly (no hang, no silent
    fallback to some other behavior) and the resulting exception's own
    message -- which app/api/v1/endpoints/health.py's health_db and
    app/core/exceptions.py's catch-all handler both log via
    logger.exception -- must never contain the password itself."""
    wrong_password = (
        "totally-wrong-password-xyz-99"  # noqa: S105 -- test fixture, not a real secret
    )
    engine = create_engine(f"postgresql+psycopg://erp_app:{wrong_password}@localhost:5432/erp_dev")
    with pytest.raises(SQLAlchemyError) as exc_info:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    assert wrong_password not in str(exc_info.value)


def test_backend_dockerfile_never_copies_an_env_file_or_accepts_a_secret_build_arg() -> None:
    dockerfile = (_BACKEND_DIR / "Dockerfile").read_text()
    assert "ARG" not in dockerfile, (
        "the Dockerfile must never accept build args -- a secret passed as one "
        "would be recoverable from `docker history`"
    )
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY") or stripped.startswith("ADD"):
            assert ".env" not in stripped, f"Dockerfile explicitly copies an env file: {line!r}"


def test_backend_dockerignore_excludes_every_env_file_except_the_example() -> None:
    lines = {line.strip() for line in (_BACKEND_DIR / ".dockerignore").read_text().splitlines()}
    assert ".env" in lines
    assert ".env.*" in lines, "must exclude every dotenv variant, not just the bare .env"
    assert "!.env.example" in lines, "the example template itself should still be copyable"


def test_no_migration_file_contains_a_hardcoded_credential() -> None:
    """Every migration in this repo is schema/privilege DDL only -- the
    one exception, backend/scripts/bootstrap_db_roles.sql, deliberately
    lives OUTSIDE alembic/versions/ specifically so it is never bundled
    into deployed application code (docs/M13_DESIGN.md Section 9)."""
    versions_dir = _BACKEND_DIR / "alembic" / "versions"
    migration_files = list(versions_dir.glob("*.py"))
    assert len(migration_files) >= 10, "expected many migration files, found suspiciously few"

    suspicious_substrings = ["erp_app_password", "erp_password", "CHANGE_ME_IN_PRODUCTION"]
    for path in migration_files:
        content = path.read_text()
        for substring in suspicious_substrings:
            assert substring not in content, f"{path.name} contains {substring!r}"


def test_frontend_env_example_holds_no_secret_shaped_variable() -> None:
    """The frontend bundle only ever holds VITE_-prefixed values, all of
    which Vite inlines into the built JS and ships to every browser --
    structurally incapable of holding a real secret, but verify the
    actual file matches that claim rather than trusting the design doc's
    own description of it."""
    env_example = (_REPO_ROOT / "frontend" / ".env.example").read_text()
    forbidden_name_fragments = ["SECRET", "PASSWORD", "PRIVATE_KEY", "TOKEN"]
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        var_name = line.split("=", 1)[0]
        for fragment in forbidden_name_fragments:
            assert fragment not in var_name.upper(), (
                f"frontend/.env.example defines {var_name!r}, which looks like a "
                "secret -- the frontend bundle is public, this would ship to "
                "every browser"
            )
