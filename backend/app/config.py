from functools import lru_cache
from typing import Literal

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

    # LLM (phase 2 amendment: Gemini free tier is the primary provider, Groq the fallback,
    # Anthropic optional and off by default). Keys are CI / hosting secrets only.
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None
    llm_provider: Literal["gemini", "groq", "anthropic"] = "gemini"
    # Unset: groq when GROQ_API_KEY is set, else no fallback. "none" turns it off explicitly.
    llm_fallback_provider: Literal["gemini", "groq", "anthropic", "none"] | None = None
    # Optional overrides for the primary provider's models (LLM_MODEL_EXTRACT/_AGENT).
    llm_model_extract: str | None = None
    llm_model_agent: str | None = None
    # Per-provider models. Groq defaults: production models on console.groq.com/docs/models
    # (checked 2026-11-14) with strict structured outputs and tool use.
    llm_model_extract_gemini: str = "gemini-3.5-flash-lite"
    llm_model_agent_gemini: str = "gemini-3.6-flash"
    llm_model_extract_groq: str = "openai/gpt-oss-20b"
    llm_model_agent_groq: str = "openai/gpt-oss-120b"
    # Anthropic models come from the environment only (no default model is assumed).
    llm_model_extract_anthropic: str | None = None
    llm_model_agent_anthropic: str | None = None
    # Daily soft caps (requests per provider per UTC day), below each free quota.
    llm_daily_soft_cap_gemini: int = 200
    llm_daily_soft_cap_groq: int = 800
    llm_daily_soft_cap_anthropic: int = 50
    # Requests per demo token per UTC day, across providers. Unset (None) turns it off.
    llm_daily_cap_per_demo_token: int | None = 40
    # One traced throwaway call at startup (walking skeleton, T0). Off by default:
    # set it once on the deployed service, check the Langfuse trace, then unset it.
    llm_ping_on_startup: bool = False

    # Langfuse, reached over OTLP/HTTP. Tracing is off when either key is missing.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    def llm_models(self, provider: str) -> tuple[str | None, str | None]:
        """(extract model, agent model) for a provider; the overrides apply to the primary."""
        extract = getattr(self, f"llm_model_extract_{provider}")
        agent = getattr(self, f"llm_model_agent_{provider}")
        if provider == self.llm_provider:
            extract = self.llm_model_extract or extract
            agent = self.llm_model_agent or agent
        return extract, agent

    def llm_daily_soft_cap(self, provider: str) -> int:
        cap: int = getattr(self, f"llm_daily_soft_cap_{provider}")
        return cap

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
