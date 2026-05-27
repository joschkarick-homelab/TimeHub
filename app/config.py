from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TimeHub"
    app_env: str = "production"
    log_level: str = "info"

    secret_key: str = Field(default="dev-insecure-change-me")
    access_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    database_url: str = "sqlite:///./data/timehub.sqlite"

    initial_admin_email: str | None = None
    initial_admin_password: str | None = None
    initial_admin_name: str = "Admin"

    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
