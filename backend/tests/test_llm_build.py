"""build_gateway and the LLM settings: provider choice, fallback rule, models, env names."""

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.llm.anthropic import AnthropicProvider
from app.llm.gateway import GatewayConfigError, LLMGateway, build_gateway
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.ledger import InMemoryLedger, SqlLedger
from tests.llm_support import FAKE_KEY


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("building a gateway must not make requests")


def _build(**settings: Any) -> LLMGateway:
    return build_gateway(
        Settings(_env_file=None, **settings),  # type: ignore[call-arg]
        http_client=httpx.Client(transport=httpx.MockTransport(_no_network)),
        ledger=InMemoryLedger(),
    )


def _names(gateway: LLMGateway) -> list[str]:
    return [p.name for p in gateway.providers]


def test_default_is_gemini_with_groq_fallback_when_its_key_is_set() -> None:
    gateway = _build(gemini_api_key=FAKE_KEY, groq_api_key=FAKE_KEY)
    assert _names(gateway) == ["gemini", "groq"]
    assert isinstance(gateway.providers[0], GeminiProvider)
    assert isinstance(gateway.providers[1], GroqProvider)


def test_no_groq_key_means_no_fallback() -> None:
    assert _names(_build(gemini_api_key=FAKE_KEY)) == ["gemini"]


def test_fallback_none_turns_it_off() -> None:
    gateway = _build(gemini_api_key=FAKE_KEY, groq_api_key=FAKE_KEY, llm_fallback_provider="none")
    assert _names(gateway) == ["gemini"]


def test_fallback_equal_to_primary_is_dropped() -> None:
    gateway = _build(llm_provider="groq", groq_api_key=FAKE_KEY, llm_fallback_provider="groq")
    assert _names(gateway) == ["groq"]


def test_primary_without_key_is_a_config_error() -> None:
    with pytest.raises(GatewayConfigError):
        _build(groq_api_key=FAKE_KEY)


def test_anthropic_is_never_the_implicit_fallback() -> None:
    gateway = _build(gemini_api_key=FAKE_KEY, anthropic_api_key=FAKE_KEY)
    assert _names(gateway) == ["gemini"]


def test_anthropic_as_explicit_fallback_needs_its_key() -> None:
    settings: dict[str, Any] = {
        "gemini_api_key": FAKE_KEY,
        "llm_fallback_provider": "anthropic",
        "llm_model_extract_anthropic": "claude-x",
        "llm_model_agent_anthropic": "claude-y",
    }
    assert _names(_build(**settings)) == ["gemini"]
    gateway = _build(anthropic_api_key=FAKE_KEY, **settings)
    assert _names(gateway) == ["gemini", "anthropic"]
    assert isinstance(gateway.providers[1], AnthropicProvider)


def test_anthropic_primary_takes_models_from_env_only() -> None:
    with pytest.raises(GatewayConfigError):
        _build(llm_provider="anthropic", anthropic_api_key=FAKE_KEY)
    gateway = _build(
        llm_provider="anthropic",
        anthropic_api_key=FAKE_KEY,
        llm_model_extract="claude-x",
        llm_model_agent="claude-y",
    )
    assert _names(gateway) == ["anthropic"]


def test_model_overrides_apply_to_the_primary_only() -> None:
    settings = Settings(_env_file=None, llm_model_extract="gemini-custom")  # type: ignore[call-arg]
    assert settings.llm_models("gemini") == ("gemini-custom", "gemini-3.6-flash")
    assert settings.llm_models("groq") == ("openai/gpt-oss-20b", "openai/gpt-oss-120b")
    assert settings.llm_models("anthropic") == (None, None)


def test_settings_read_the_documented_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "LLM_PROVIDER": "groq",
        "LLM_FALLBACK_PROVIDER": "gemini",
        "LLM_MODEL_EXTRACT_GROQ": "g-extract",
        "LLM_MODEL_AGENT_GEMINI": "gem-agent",
        "LLM_DAILY_SOFT_CAP_GEMINI": "11",
        "LLM_DAILY_SOFT_CAP_GROQ": "22",
        "LLM_DAILY_SOFT_CAP_ANTHROPIC": "33",
        "LLM_DAILY_CAP_PER_DEMO_TOKEN": "4",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "groq" and settings.llm_fallback_provider == "gemini"
    assert settings.llm_models("groq")[0] == "g-extract"
    assert settings.llm_models("gemini")[1] == "gem-agent"
    caps = [settings.llm_daily_soft_cap(p) for p in ("gemini", "groq", "anthropic")]
    assert caps == [11, 22, 33]
    assert settings.llm_daily_cap_per_demo_token == 4


def test_default_caps() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    caps = [settings.llm_daily_soft_cap(p) for p in ("gemini", "groq", "anthropic")]
    assert caps == [200, 800, 50] and settings.llm_daily_cap_per_demo_token == 40


def test_default_ledger_is_sql_and_building_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.db import get_engine

    monkeypatch.setenv("DATABASE_URL", "postgresql://app:x@127.0.0.1:1/none")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        gateway = build_gateway(
            Settings(_env_file=None, gemini_api_key=FAKE_KEY),  # type: ignore[call-arg]
            http_client=httpx.Client(transport=httpx.MockTransport(_no_network)),
        )
        assert isinstance(gateway._ledger, SqlLedger)
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
