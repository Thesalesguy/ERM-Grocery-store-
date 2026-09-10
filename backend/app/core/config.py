"""Environment-based application configuration.

All values are sourced from environment variables (see .env.example). Nothing
sensitive has a production-usable default — the defaults here are only safe
for local development.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Grocery ERP/POS"
    ENVIRONMENT: str = "development"  # development | staging | production
    API_V1_PREFIX: str = "/api/v1"

    # Postgres connection string, e.g.:
    # postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev
    DATABASE_URL: str = "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_dev"

    # Used later for JWT signing (see docs/TECHNICAL_BLUEPRINT.md Section H).
    # Must be overridden with a long random value outside of development.
    SECRET_KEY: str = "dev-only-insecure-secret-key-change-me"

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
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
