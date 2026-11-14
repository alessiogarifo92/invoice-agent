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

    # Upload limits (phase 3 flow, step 1): PDF only, at most 2 MB and 5 pages.
    max_upload_bytes: int = 2_000_000
    max_pages: int = 5

    # LLM (phase 2 amendment: Gemini free tier is the primary provider).
    gemini_api_key: str | None = None
    llm_model_extract: str = "gemini-3.5-flash-lite"
    # One traced throwaway call at startup (walking skeleton, T0). Off by default:
    # set it once on the deployed service, check the Langfuse trace, then unset it.
    llm_ping_on_startup: bool = False

    # Langfuse, reached over OTLP/HTTP. Tracing is off when either key is missing.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
