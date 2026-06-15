from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Secrets we ship as placeholders/defaults — never acceptable in production,
# because the same key signs both the JWTs and the session cookies. A known
# key lets anyone forge tokens for any user (including admins).
_INSECURE_SECRETS = {
    "",
    "dev-insecure-change-me",
    "change-me-please-use-a-long-random-string",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TimeHub"
    app_env: str = "production"
    log_level: str = "info"

    secret_key: str = Field(default="dev-insecure-change-me")
    access_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    # Database — three ways to configure, in order of precedence:
    #   1. DATABASE_URL set explicitly (SQLite for dev, custom Postgres host, ...)
    #   2. POSTGRES_USER + POSTGRES_PASSWORD + POSTGRES_HOST + POSTGRES_DB set
    #      → URL gets constructed with proper escaping (URL-unsafe password chars
    #        like /, @, : are handled correctly)
    #   3. SQLite fallback at ./data/timehub.sqlite
    database_url: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None

    initial_admin_email: str | None = None
    initial_admin_password: str | None = None
    initial_admin_name: str = "Admin"

    cors_origins: str = "*"

    # --- AI-assisted CSV mapping ---
    anthropic_api_key: str | None = None
    ai_mapping_model: str = "claude-sonnet-4-6"
    ai_mapping_max_sample_lines: int = 15

    @model_validator(mode="after")
    def _guard_secret_key(self) -> "Settings":
        if self.app_env.strip().lower() == "production" and self.secret_key.strip() in _INSECURE_SECRETS:
            raise ValueError(
                "SECRET_KEY ist nicht gesetzt oder nutzt einen unsicheren Platzhalter. "
                "In Produktion einen langen Zufallswert über die Umgebungsvariable "
                "SECRET_KEY setzen, z. B.:\n"
                '    python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
                "Für lokale Entwicklung alternativ APP_ENV=dev setzen."
            )
        return self

    @model_validator(mode="after")
    def _resolve_database_url(self) -> "Settings":
        if self.database_url:
            return self
        if all([self.postgres_user, self.postgres_password, self.postgres_host, self.postgres_db]):
            self.database_url = URL.create(
                drivername="postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            ).render_as_string(hide_password=False)
        else:
            self.database_url = "sqlite:///./data/timehub.sqlite"
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
