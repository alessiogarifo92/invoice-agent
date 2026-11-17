"""The CORS allowlist (phase 3: the Pages app calls the API cross-origin). No database:
preflights are answered by the middleware, and /healthz never touches the DB."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app

PAGES = "https://invoice-agent.pages.dev"
DEV = "http://localhost:5173"
DECISION = "/proposals/00000000-0000-0000-0000-000000000000/decision"


def _client(origins: str = f"{PAGES},{DEV}") -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, cors_origins=origins)))  # type: ignore[call-arg]


def _preflight(client: TestClient, origin: str, method: str, headers: str) -> Any:
    return client.options(
        DECISION,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_a_listed_origin_is_echoed_and_the_etag_is_exposed() -> None:
    response = _client().get("/healthz", headers={"Origin": PAGES})
    assert response.headers["access-control-allow-origin"] == PAGES
    assert "etag" in response.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers


def test_a_non_listed_origin_gets_no_allow_origin_header() -> None:
    client = _client()
    for origin in ("https://evil.example", f"{PAGES}.evil.example", "null"):
        response = client.get("/healthz", headers={"Origin": origin})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


def test_preflight_for_the_decision_endpoint_allows_if_match_and_authorization() -> None:
    response = _preflight(_client(), PAGES, "POST", "authorization, content-type, if-match")
    assert response.status_code == 200
    headers = response.headers
    assert headers["access-control-allow-origin"] == PAGES
    assert "POST" in headers["access-control-allow-methods"]
    allowed = {h.strip().lower() for h in headers["access-control-allow-headers"].split(",")}
    assert {"authorization", "if-match", "content-type"} <= allowed
    assert "access-control-allow-credentials" not in headers


def test_preflight_from_a_non_listed_origin_is_refused() -> None:
    response = _preflight(_client(), "https://evil.example", "POST", "authorization, if-match")
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("method", "headers"),
    [("DELETE", "authorization"), ("POST", "x-custom-header")],
)
def test_preflight_for_an_unlisted_method_or_header_is_refused(method: str, headers: str) -> None:
    assert _preflight(_client(), PAGES, method, headers).status_code == 400


def test_a_trailing_slash_in_the_setting_still_matches_the_origin() -> None:
    # Browsers never send a trailing slash in Origin; a pasted Pages URL often has one.
    settings = Settings(_env_file=None, cors_origins=f"{PAGES}/")  # type: ignore[call-arg]
    assert settings.cors_origin_list == [PAGES]


@pytest.mark.parametrize("value", ["*", f"{PAGES},*", "https://*.pages.dev"])
def test_a_wildcard_origin_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(_env_file=None, cors_origins=value)  # type: ignore[call-arg]
