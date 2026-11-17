"""Error classifier: every branch per provider, on recorded-style response bodies."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.llm.errors import (
    AuthError,
    InvalidRequest,
    LLMError,
    QuotaExhausted,
    RateLimited,
    TransientError,
    classify_anthropic,
    classify_gemini,
    classify_groq,
    network_error,
)

SECRET_ECHO = "sk-echoed-secret-in-body"


def _gemini_429(*violations: str, retry_delay: str | None = None) -> dict[str, Any]:
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {
                    "quotaMetric": "generativelanguage.googleapis.com/"
                    "generate_content_free_tier_requests",
                    "quotaId": quota_id,
                    "quotaDimensions": {"location": "global", "model": "gemini-3.5-flash-lite"},
                    "quotaValue": "20",
                }
                for quota_id in violations
            ],
        },
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
    ]
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return {
        "error": {
            "code": 429,
            "message": f"You exceeded your current quota. {SECRET_ECHO}",
            "status": "RESOURCE_EXHAUSTED",
            "details": details,
        }
    }


def _groq_429(message: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "requests",
            "code": "rate_limit_exceeded",
        }
    }


def _anthropic(error_type: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _assert_no_body(error: LLMError) -> None:
    # The message names the provider, the status and our classified code: never the body.
    assert SECRET_ECHO not in str(error)


# --- Gemini -------------------------------------------------------------------------------


def test_gemini_per_day_quota_is_exhausted_not_retried() -> None:
    body = _gemini_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry_delay="43s")
    error = classify_gemini(429, {}, body)
    assert isinstance(error, QuotaExhausted)
    assert error.confirmed and error.period == "day" and error.reset_at is None
    assert error.code == "quota_exhausted" and error.status == 429
    _assert_no_body(error)


def test_gemini_per_day_wins_over_a_per_minute_violation() -> None:
    body = _gemini_429(
        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        retry_delay="5s",
    )
    assert isinstance(classify_gemini(429, {}, body), QuotaExhausted)


def test_gemini_per_minute_with_retry_delay_is_rate_limited() -> None:
    body = _gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", retry_delay="34.5s")
    error = classify_gemini(429, {}, body)
    assert isinstance(error, RateLimited)
    assert error.retry_after_s == 34.5 and error.from_provider
    _assert_no_body(error)


def test_gemini_429_without_any_signal_gets_the_default_wait() -> None:
    error = classify_gemini(429, {}, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
    assert isinstance(error, RateLimited)
    assert error.retry_after_s == 10.0 and not error.from_provider


def test_gemini_bad_api_key_400_is_an_auth_error() -> None:
    body = {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}
            ],
        }
    }
    assert isinstance(classify_gemini(400, {}, body), AuthError)


def test_gemini_plain_400_is_invalid_request() -> None:
    body = {"error": {"code": 400, "message": SECRET_ECHO, "status": "INVALID_ARGUMENT"}}
    error = classify_gemini(400, {}, body)
    assert isinstance(error, InvalidRequest)
    _assert_no_body(error)


@pytest.mark.parametrize("status", [500, 503, 504])
def test_gemini_5xx_is_transient(status: int) -> None:
    assert isinstance(
        classify_gemini(status, {}, {"error": {"status": "UNAVAILABLE"}}), TransientError
    )


def test_gemini_403_is_auth_error() -> None:
    error = classify_gemini(403, {}, {"error": {"status": "PERMISSION_DENIED"}})
    assert isinstance(error, AuthError)


# --- Groq ---------------------------------------------------------------------------------


def test_groq_per_minute_with_retry_after_header_is_rate_limited() -> None:
    body = _groq_429(
        "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` service tier "
        "`on_demand` on requests per minute (RPM): Limit 30, Used 30, Requested 1. "
        "Please try again in 2s."
    )
    error = classify_groq(429, {"retry-after": "2"}, body)
    assert isinstance(error, RateLimited)
    assert error.retry_after_s == 2.0 and error.from_provider
    assert "org_x" not in str(error)


@pytest.mark.parametrize(
    ("message", "reset_in_s"),
    [
        (
            "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` service "
            "tier `on_demand` on requests per day (RPD): Limit 1000, Used 1000, Requested 1. "
            "Please try again in 1m26.4s.",
            86.4,
        ),
        (
            "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service "
            "tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199990, Requested 900. "
            "Please try again in 1h2m3s.",
            3723.0,
        ),
        ("Rate limit reached on requests per day (RPD).", None),
    ],
)
def test_groq_daily_limit_is_exhausted_even_with_retry_after(
    message: str, reset_in_s: float | None
) -> None:
    error = classify_groq(429, {"retry-after": "86"}, _groq_429(message))
    assert isinstance(error, QuotaExhausted)
    assert error.period == "day" and error.reset_in_s == reset_in_s
    assert "org_x" not in str(error)


def test_groq_429_without_header_or_daily_signal_gets_the_default_wait() -> None:
    error = classify_groq(429, {}, _groq_429("Rate limit reached on requests per minute (RPM)."))
    assert isinstance(error, RateLimited) and not error.from_provider


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, AuthError),
        (400, InvalidRequest),
        (404, InvalidRequest),
        (413, InvalidRequest),
        (500, TransientError),
        (502, TransientError),
        (503, TransientError),
    ],
)
def test_groq_status_mapping(status: int, expected: type[LLMError]) -> None:
    body = {"error": {"message": SECRET_ECHO, "type": "invalid_request_error"}}
    error = classify_groq(status, {}, body)
    assert type(error) is expected
    _assert_no_body(error)


# --- Anthropic ----------------------------------------------------------------------------


def test_anthropic_workspace_limit_400_is_exhausted_until_the_stated_date() -> None:
    body = _anthropic(
        "invalid_request_error",
        "You have reached your specified workspace API usage limits. You will regain access on "
        "2026-12-01 at 00:00 UTC.",
    )
    error = classify_anthropic(400, {}, body)
    assert isinstance(error, QuotaExhausted)
    assert error.period == "month"
    assert error.reset_at == datetime(2026, 12, 1, 0, 0, tzinfo=UTC)


def test_anthropic_workspace_limit_without_date_falls_back_to_the_month() -> None:
    body = _anthropic("invalid_request_error", "You have reached your specified API usage limits.")
    error = classify_anthropic(400, {}, body)
    assert isinstance(error, QuotaExhausted)
    assert error.period == "month" and error.reset_at is None


def test_anthropic_enforced_spend_limit_429_is_exhausted_even_with_retry_after() -> None:
    body = _anthropic("enforced_spend_limit_reached", "Spend limit reached for this tier.")
    error = classify_anthropic(429, {"retry-after": "30"}, body)
    assert isinstance(error, QuotaExhausted) and error.period == "month"


def test_anthropic_rate_limit_with_retry_after_is_rate_limited() -> None:
    body = _anthropic("rate_limit_error", "Number of request tokens has exceeded your rate limit.")
    error = classify_anthropic(429, {"retry-after": "7"}, body)
    assert isinstance(error, RateLimited) and error.retry_after_s == 7.0


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (529, "overloaded_error", TransientError),
        (500, "api_error", TransientError),
        (401, "authentication_error", AuthError),
        (403, "permission_error", AuthError),
        (400, "invalid_request_error", InvalidRequest),
        (404, "not_found_error", InvalidRequest),
    ],
)
def test_anthropic_status_mapping(status: int, error_type: str, expected: type[LLMError]) -> None:
    error = classify_anthropic(status, {}, _anthropic(error_type, SECRET_ECHO))
    assert type(error) is expected
    _assert_no_body(error)


# --- shared -------------------------------------------------------------------------------


def test_network_error_is_transient_and_names_only_the_exception_class() -> None:
    request = httpx.Request("POST", "https://example.invalid/?key=" + SECRET_ECHO)
    error = network_error("groq", httpx.ConnectError(SECRET_ECHO, request=request))
    assert isinstance(error, TransientError)
    assert SECRET_ECHO not in str(error) and "ConnectError" in str(error)


def test_non_json_body_is_classified_by_status_alone() -> None:
    assert isinstance(classify_gemini(502, {}, None), TransientError)
    assert isinstance(classify_groq(429, {}, "<html>"), RateLimited)


def test_http_date_retry_after_is_ignored() -> None:
    error = classify_groq(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None)
    assert isinstance(error, RateLimited) and not error.from_provider


def test_gemini_402_prepay_depleted_is_quota() -> None:
    from app.llm.errors import QuotaExhausted, classify_gemini

    err = classify_gemini(402, {}, {"error": {"code": 402, "status": "FAILED_PRECONDITION"}})
    assert isinstance(err, QuotaExhausted)
