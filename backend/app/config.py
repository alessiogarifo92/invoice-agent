from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from the environment. Never hard-code secrets here."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Restricted app role (INSERT/SELECT only on audit tables). Used by the API.
    database_url: str = "postgresql+psycopg://app:app@localhost:5432/invoice_agent"
    # Owner role, used only by migrations. Never used by the API process.
    owner_database_url: str | None = None
    # Comma-separated list of allowed browser origins (Cloudflare Pages URL, dev server).
    cors_origins: str = "http://localhost:5173"
    app_version: str = "0.1.0"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
