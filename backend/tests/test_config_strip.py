import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "raw",
    ["﻿key-123", "key-123\n", "  key-123\r\n", "﻿ key-123 \t"],
)
def test_api_keys_lose_bom_and_whitespace(raw: str) -> None:
    settings = Settings(gemini_api_key=raw, groq_api_key=raw, langfuse_secret_key=raw)
    assert settings.gemini_api_key == "key-123"
    assert settings.groq_api_key == "key-123"
    assert settings.langfuse_secret_key == "key-123"


def test_key_from_environment_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "﻿key-from-env\n")
    monkeypatch.setenv("DATABASE_URL", "﻿postgresql+psycopg://u:p@h/db ")
    settings = Settings()
    assert settings.gemini_api_key == "key-from-env"
    assert settings.database_url == "postgresql+psycopg://u:p@h/db"
    # The stripped key is now a valid HTTP header value.
    settings.gemini_api_key.encode("ascii")


def test_inner_characters_are_kept() -> None:
    assert Settings(gemini_api_key="a b").gemini_api_key == "a b"
    assert Settings(gemini_api_key=None).gemini_api_key is None
