"""Demo-token primitives (phase 3, "Public exposure"): pure, no database."""

import hashlib
import hmac

import pytest
from pydantic import ValidationError

from app.api import auth
from app.config import Settings
from tests.jobs_support import make_settings

TOKEN = "demo-token-for-tests-only"
OTHER = "another-demo-token-for-tests"


def _settings(**values: str) -> Settings:
    return make_settings(**values)


def test_token_hash_is_the_sha256_hex_of_the_utf8_token() -> None:
    assert auth.token_hash(TOKEN) == hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (f"Bearer {TOKEN}", TOKEN),
        (f"bearer {TOKEN}", TOKEN),
        (f"BEARER   {TOKEN}  ", TOKEN),
    ],
)
def test_bearer_token_reads_the_bearer_scheme(header: str, token: str) -> None:
    assert auth.bearer_token(header) == token


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        f"Basic {TOKEN}",
        TOKEN,
        f"Bearer {TOKEN} extra",
        "Bearer " + "x" * (auth.TOKEN_MAX + 1),
    ],
)
def test_bearer_token_rejects_anything_else(header: str | None) -> None:
    assert auth.bearer_token(header) is None


def test_match_token_returns_the_matching_hash() -> None:
    hashes = (auth.token_hash(OTHER), auth.token_hash(TOKEN))
    assert auth.match_token(TOKEN, hashes) == auth.token_hash(TOKEN)
    assert auth.match_token("wrong", hashes) is None
    assert auth.match_token(TOKEN, ()) is None


def test_match_token_compares_every_configured_hash_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[str, str]] = []

    def spy(a: str, b: str) -> bool:
        compared.append((a, b))
        return a == b

    monkeypatch.setattr(hmac, "compare_digest", spy)
    hashes = (auth.token_hash(TOKEN), auth.token_hash(OTHER))
    assert auth.match_token(TOKEN, hashes) == hashes[0]
    # No early exit on a match, and only hashes are ever compared, never the raw token.
    assert [b for _, b in compared] == list(hashes)
    assert all(a == auth.token_hash(TOKEN) for a, _ in compared)


def test_the_reviewer_id_is_a_short_fingerprint_of_the_hash() -> None:
    digest = auth.token_hash(TOKEN)
    assert auth.reviewer_id(digest) == f"demo:{digest[:12]}"
    assert TOKEN not in auth.reviewer_id(digest)


def test_settings_read_the_hashes_lowercased_and_stripped() -> None:
    a, b = auth.token_hash(TOKEN), auth.token_hash(OTHER)
    settings = _settings(demo_token_hashes=f"﻿ {a.upper()} , ,{b}\n")
    assert settings.demo_token_hash_list == (a, b)


def test_no_hashes_configured_means_no_token_is_valid() -> None:
    assert _settings().demo_token_hash_list == ()


@pytest.mark.parametrize("value", ["not-a-hash", "ab" * 31, TOKEN])
def test_settings_refuse_a_value_that_is_not_a_sha256_hex(value: str) -> None:
    # A raw token pasted where its hash belongs would otherwise silently never match.
    with pytest.raises(ValidationError, match="DEMO_TOKEN_HASHES"):
        _settings(demo_token_hashes=value)
