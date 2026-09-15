"""M12 Phase 2: configuration/environment safety. Proves the fail-closed
production validator in app.core.config.Settings actually fails closed
-- a `production` boot with a dev-shaped secret, a placeholder database
password, or a wildcard CORS origin must raise at construction time,
before the app could ever serve a request -- and proves the same values
remain perfectly usable in `development`/`staging` so local dev and CI
are never broken by this check.

Settings() reads real environment variables via pydantic-settings, so
every test here uses monkeypatch.setenv/delenv rather than passing
kwargs directly -- kwargs would bypass the env-var-driven behavior this
class is actually used with in production (a real deploy sets
environment variables, it doesn't construct Settings(...) in Python).
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _clear_relevant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "ENVIRONMENT",
        "SECRET_KEY",
        "DATABASE_URL",
        "MIGRATIONS_DATABASE_URL",
        "CORS_ORIGINS",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Settings() reads a real .env file by default (model_config
    # env_file=".env") -- the repo's own backend/.env would otherwise
    # leak into these tests and hide the exact env-var values each test
    # sets. Point env_file at a file that doesn't exist so only the
    # environment variables this test explicitly sets are in play.
    # model_config is a plain dict (SettingsConfigDict), so it's patched
    # with setitem, not setattr.
    monkeypatch.setitem(Settings.model_config, "env_file", "/nonexistent/.env")


def test_default_settings_construct_cleanly_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_relevant_env(monkeypatch)
    settings = Settings()
    assert settings.ENVIRONMENT == "development"
    assert settings.SECRET_KEY == "dev-only-insecure-secret-key-change-me"


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_dev_insecure_secret_key_is_allowed_outside_production(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """The whole point of a FAIL-CLOSED check is that it only closes the
    door that matters -- development and staging must stay exactly as
    permissive as they are today, or this becomes a check nobody can
    develop against."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
    settings = Settings()
    assert settings.ENVIRONMENT == environment


def test_production_with_dev_secret_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings()


def test_production_with_short_secret_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not just the literal known default -- any short/guessable key must
    be rejected, since an operator "changing" the key to something
    equally weak (e.g. "prod-secret") must not slip through a check that
    only compares against one hardcoded string."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "too-short")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings()


def test_production_with_placeholder_database_password_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:CHANGE_ME_IN_PRODUCTION@db:5432/erp_prod"
    )
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_production_with_placeholder_migrations_database_password_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    monkeypatch.setenv(
        "MIGRATIONS_DATABASE_URL",
        "postgresql+psycopg://erp_user:CHANGE_ME_IN_PRODUCTION@db:5432/erp_prod",
    )
    with pytest.raises(ValidationError, match="MIGRATIONS_DATABASE_URL"):
        Settings()


def test_production_with_unrotated_dev_database_password_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M13 Phase 9: a real gap found during this phase -- the dev-fixture
    password ("erp_app_password", from this repo's own backend/.env.example
    default) contains no CHANGE_ME_IN_PRODUCTION marker, so simply
    forgetting to override DATABASE_URL from its Settings-class default
    while flipping ENVIRONMENT to production previously passed every
    existing check. A dev credential must never silently become a
    production one."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:erp_app_password@localhost:5432/erp_dev"
    )
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_production_with_unrotated_dev_migrations_database_password_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    monkeypatch.setenv(
        "MIGRATIONS_DATABASE_URL",
        "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev",
    )
    with pytest.raises(ValidationError, match="MIGRATIONS_DATABASE_URL"):
        Settings()


def test_production_with_a_genuinely_rotated_password_is_not_falsely_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev-password check must match the known insecure values
    exactly -- it must not become so broad that it rejects a real
    rotated password that merely shares a substring (e.g. "password")
    with the values it's guarding against."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://erp_app:Tr0ub4dor-and-a-real-random-password-99@db:5432/erp_prod",
    )
    settings = Settings()
    assert settings.is_production is True


def test_production_with_wildcard_cors_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings()


def test_production_with_safe_config_constructs_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive case: a genuinely production-safe configuration must
    NOT be rejected -- proves this is a real fail-closed gate, not a
    check that simply always fails in production."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    monkeypatch.setenv(
        "MIGRATIONS_DATABASE_URL",
        "postgresql+psycopg://erp_user:another-real-rotated-password@db:5432/erp_prod",
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    settings = Settings()
    assert settings.is_production is True


def test_secret_key_never_appears_in_a_logged_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a validation failure that legitimately needs to explain WHAT
    is wrong must never echo the secret value itself back into an
    exception message or log line -- only the fact that it's insecure."""
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    secret = "dev-only-insecure-secret-key-change-me"
    monkeypatch.setenv("SECRET_KEY", secret)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://erp_app:a-real-rotated-password@db:5432/erp_prod"
    )
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    # the error explains the problem without ever repeating a real
    # secret's value back (moot here since this IS the known-insecure
    # default, but the message shape must not template the value in)
    assert secret not in str(exc_info.value)
