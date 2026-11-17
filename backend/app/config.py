from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A key copied from a file saved by Windows Notepad can start with a byte-order mark,
# which HTTP headers cannot carry (seen on the first live CI eval).
_STRIP_CHARS = "\ufeff \t\r\n"


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

    # Background worker (phase 3, A2): a thread in the web process that runs the pipeline.
    # Off by default, so tests and local API runs never start it; the deployed service sets
    # WORKER_ENABLED=true. The heartbeat must be shorter than the lease.
    worker_enabled: bool = False
    worker_poll_interval_s: float = 2.0
    worker_lease_s: float = 60.0
    worker_heartbeat_s: float = 15.0
    worker_max_attempts: int = 3
    # Retry n waits n times this long (quota waits use the reset time instead).
    worker_retry_backoff_s: float = 30.0

    # Triage agent limits (phase 3, "Agent"): tool calls per run, the terminal one included,
    # and a wall-clock budget checked before every model request and every tool call.
    # Reaching either makes the agent's action clarify.
    agent_max_tool_calls: int = 8
    agent_timeout_s: float = 120.0

    # Langfuse, reached over OTLP/HTTP. Tracing is off when either key is missing.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @field_validator(
        "database_url",
        "owner_database_url",
        "gemini_api_key",
        "groq_api_key",
        "anthropic_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_host",
        mode="before",
    )
    @classmethod
    def _strip_pasted_noise(cls, value: object) -> object:
        """Drop a byte-order mark and surrounding whitespace from pasted secrets and URLs."""
        if isinstance(value, str):
            return value.strip(_STRIP_CHARS)
        return value

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
