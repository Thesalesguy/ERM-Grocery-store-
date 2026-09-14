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
