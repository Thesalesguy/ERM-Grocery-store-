"""Environment-based application configuration.

All values are sourced from environment variables (see .env.example). Nothing
sensitive has a production-usable default — the defaults here are only safe
for local development.
"""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that are fine for local development but must never reach a
# production boot — kept as an explicit set (not just "is it the
# literal default") so a production deploy that merely SHORTENED the
# key to something equally guessable still gets caught by the length
# check below, and so this set can grow if another known-insecure value
# is ever discovered without changing the check's shape.
_KNOWN_INSECURE_SECRET_KEYS = {
    "dev-only-insecure-secret-key-change-me",
    "changeme",
    "secret",
}
_MIN_PRODUCTION_SECRET_KEY_LENGTH = 32
_DATABASE_PLACEHOLDER_MARKER = "CHANGE_ME_IN_PRODUCTION"

# M13 Phase 9: the DEV-fixture passwords this repo's own .env.example /
# local setup uses (distinct from bootstrap_db_roles.sql's
# CHANGE_ME_IN_PRODUCTION marker above, which only catches a cluster
# that was bootstrapped but never had its password rotated). Found as a
# real gap during this phase: setting ENVIRONMENT=production while
# simply forgetting to override DATABASE_URL/MIGRATIONS_DATABASE_URL
# from their Settings-class dev defaults passed the placeholder check
# above (neither dev password contains that marker string) and would
# have booted "in production" against a well-known, publicly-visible
# development credential. Never let a dev credential silently become a
# production one.
_KNOWN_INSECURE_DATABASE_PASSWORDS = {"erp_app_password", "erp_password"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Grocery ERP/POS"
    ENVIRONMENT: str = "development"  # development | staging | production
    API_V1_PREFIX: str = "/api/v1"

    # Postgres connection string the running application uses at request
    # time. This should be the restricted "erp_app" runtime role, NOT the
    # schema-owning role migrations run as — see
    # docs/M1_DATABASE_DESIGN.md "Audit-log & ledger write protection" and
    # backend/scripts/bootstrap_db_roles.sql.
    DATABASE_URL: str = "postgresql+psycopg://erp_app:erp_app_password@localhost:5432/erp_dev"

    # Connection string Alembic uses to run migrations (DDL, plus the
    # privilege-grant DCL in the M1 runtime-role migration). This must be
    # the schema-owning role (e.g. erp_user), which needs CREATE/ALTER
    # TABLE rights the restricted erp_app role deliberately lacks. Falls
    # back to DATABASE_URL when unset, for throwaway/local setups that
    # haven't split the two roles yet — but real environments should
    # always set this explicitly to the owner role's connection string.
    MIGRATIONS_DATABASE_URL: str | None = None

    # JWT signing key (see docs/TECHNICAL_BLUEPRINT.md Section H).
    # Must be overridden with a long random value outside of development.
    SECRET_KEY: str = "dev-only-insecure-secret-key-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # Comma-separated list of allowed browser origins for the frontend SPA.
    CORS_ORIGINS: str = "http://localhost:5173"

    LOG_LEVEL: str = "INFO"

    # M12 Phase 7: application-layer half of the M2-deferred request-size
    # gap (docs/M12_DESIGN.md Section 14). 2MB comfortably covers the
    # largest legitimate payload in this app (a multi-line sale/purchase
    # order submission) while still rejecting an oversized body with a
    # clean 413 before it is ever read into memory. The proxy-layer half
    # (`client_max_body_size`) is a separate, documented production
    # control for whenever nginx is actually stood up.
    MAX_REQUEST_BODY_BYTES: int = 2 * 1024 * 1024

    # M13 Phase 8: explicit, documented connection-pool bounds rather than
    # relying on SQLAlchemy's own implicit defaults (which happen to be
    # the same numbers -- 5 and 10 -- but as an unstated default, not a
    # value anyone chose). Each running process can hold at most
    # DB_POOL_SIZE + DB_MAX_OVERFLOW connections open; with N uvicorn
    # workers that's N * 15 by default. A production deploy running
    # multiple workers must size these (or PostgreSQL's own
    # max_connections) so the application alone cannot exhaust the
    # cluster's connection limit -- see docs/M13_HARDENING_AUDIT.md
    # Section 8 for the worked example against this deployment's default
    # max_connections=100.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    @field_validator("ENVIRONMENT")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        allowed = {"development", "staging", "production"}
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}, got {value!r}")
        return value

    @property
    def migrations_database_url(self) -> str:
        return self.MIGRATIONS_DATABASE_URL or self.DATABASE_URL

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @model_validator(mode="after")
    def _forbid_insecure_production_config(self) -> "Settings":
        """M12 Phase 2: fail closed. A `production` boot with a dev-shaped
        secret, a placeholder database password, or a wildcard CORS origin
        must crash immediately at startup (Settings() construction, before
        the app can serve a single request) rather than run insecurely.
        `development`/`staging` are deliberately exempt — the whole point
        is that switching ENVIRONMENT is what makes these checks bite, not
        that the checks apply unconditionally (which would just make local
        dev unusable)."""
        if not self.is_production:
            return self

        if (
            self.SECRET_KEY in _KNOWN_INSECURE_SECRET_KEYS
            or len(self.SECRET_KEY) < _MIN_PRODUCTION_SECRET_KEY_LENGTH
        ):
            raise ValueError(
                "SECRET_KEY must be a long, random value "
                f"(>= {_MIN_PRODUCTION_SECRET_KEY_LENGTH} characters, not a known "
                "development default) before starting in production"
            )
        if _DATABASE_PLACEHOLDER_MARKER in self.DATABASE_URL:
            raise ValueError(
                "DATABASE_URL still contains the bootstrap placeholder password "
                f"({_DATABASE_PLACEHOLDER_MARKER!r}); rotate erp_app's password "
                "(see backend/scripts/bootstrap_db_roles.sql) before starting in "
                "production"
            )
        if self.MIGRATIONS_DATABASE_URL and _DATABASE_PLACEHOLDER_MARKER in (
            self.MIGRATIONS_DATABASE_URL
        ):
            raise ValueError(
                "MIGRATIONS_DATABASE_URL still contains the bootstrap placeholder "
                "password; rotate it before starting in production"
            )
        for known_password in _KNOWN_INSECURE_DATABASE_PASSWORDS:
            if known_password in self.DATABASE_URL:
                raise ValueError(
                    f"DATABASE_URL still contains the known development password "
                    f"{known_password!r}; a production deploy must never reuse a "
                    "development credential"
                )
            if self.MIGRATIONS_DATABASE_URL and known_password in self.MIGRATIONS_DATABASE_URL:
                raise ValueError(
                    f"MIGRATIONS_DATABASE_URL still contains the known development "
                    f"password {known_password!r}; a production deploy must never "
                    "reuse a development credential"
                )
        if "*" in self.cors_origins_list:
            raise ValueError(
                "CORS_ORIGINS must not be a wildcard in production (also "
                "incompatible with allow_credentials=True); list explicit "
                "frontend origins"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
