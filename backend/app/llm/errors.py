"""Our own error classifier: HTTP status, headers and body -> a small set of classes.

Provider SDK retries are off (phase 3, "Spend control": `max_retries=0`); the gateway decides
what is retried from these classes alone. The rules, by class:

- RateLimited: a per-minute 429 carrying `retry-after` or Gemini's RetryInfo. Retried after
  that delay (phase 2 amendment: "A per-minute 429 with `retry-after` is retried").
- QuotaExhausted: a daily quota (Gemini per-day QuotaFailure, Groq RPD/TPD) or Anthropic's
  spend limits. Never retried: the gateway falls through to the fallback provider once
  (phase 2 amendment, "Spend control becomes quota control"; phase 3, "Spend control").
- TransientError: 5xx, 529 and network errors. Retried (phase 3, "Spend control").
- AuthError / InvalidRequest: every other 4xx. Not retried.
- SchemaError: the structured output does not validate. Not retried, surfaced (phase 3,
  "Agent": a schema failure is `clarify`).

Exception messages carry the provider, the HTTP status and our classified code only. Response
bodies can echo prompts, organisation ids or keys, so they never reach a message.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from app.llm.types import Usage

# A 429 with neither retry-after nor a daily signal: wait this long once, then give up on
# the provider for this call (it is then treated like a quota failure, see the gateway).
DEFAULT_RATE_LIMIT_WAIT_S = 10.0

Period = Literal["day", "month"]


class LLMError(Exception):
    code = "llm_error"

    def __init__(self, provider: str, *, status: int | None = None, usage: Usage | None = None):
        self.provider = provider
        self.status = status
        # Tokens the failed call still consumed (e.g. a reply that failed schema validation).
        self.usage = usage or Usage()
        where = f" http {status}" if status is not None else ""
        super().__init__(f"{provider}{where}: {self.code}")


class RateLimited(LLMError):
    code = "rate_limited"

    def __init__(
        self,
        provider: str,
        retry_after_s: float,
        *,
        from_provider: bool,
        status: int | None = 429,
    ):
        super().__init__(provider, status=status)
        self.retry_after_s = retry_after_s
        # False when the provider gave no delay and DEFAULT_RATE_LIMIT_WAIT_S is assumed.
        self.from_provider = from_provider


class QuotaExhausted(LLMError):
    code = "quota_exhausted"

    def __init__(
        self,
        provider: str,
        *,
        status: int | None = None,
        period: Period = "day",
        reset_at: datetime | None = None,
        reset_in_s: float | None = None,
        confirmed: bool = True,
    ):
        super().__init__(provider, status=status)
        self.period = period
        self.reset_at = reset_at
        self.reset_in_s = reset_in_s
        # True when the provider itself said so: only then is budget_state.exhausted_until set.
        self.confirmed = confirmed


class ProviderBlocked(QuotaExhausted):
    """Raised by the gateway before any request: soft cap reached or exhausted_until ahead."""

    code = "provider_blocked"

    def __init__(self, provider: str, reason: Literal["soft_cap", "exhausted_until"]):
        super().__init__(provider, confirmed=False)
        self.reason = reason


class QuotaUnavailable(LLMError):
    """No provider can take the call today. The caller sets the invoice to awaiting_quota."""

    code = "quota_unavailable"

    def __init__(self, providers: list[str], reason: Literal["providers", "demo_token_cap"]):
        super().__init__("+".join(providers) or "none")
        self.providers = providers
        self.reason = reason


class TransientError(LLMError):
    code = "transient"

    def __init__(self, provider: str, *, status: int | None = None, cause: str = ""):
        super().__init__(provider, status=status)
        if cause:
            # Exception class name only (e.g. ConnectError): its message may hold the URL.
            self.args = (f"{self.args[0]} ({cause})",)


class InvalidRequest(LLMError):
    code = "invalid_request"


class AuthError(LLMError):
    code = "auth_error"


class SchemaError(LLMError):
    code = "schema_error"


def network_error(provider: str, exc: BaseException) -> TransientError:
    return TransientError(provider, cause=type(exc).__name__)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # The HTTP-date form is not used by these providers; treat as absent.
    return seconds if seconds >= 0 else None


def _error_obj(body: Any) -> dict[str, Any]:
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _by_status(provider: str, status: int) -> LLMError:
    # Phase 3, "Spend control": 5xx and 529 are retried; every other 4xx is not.
    if status >= 500:
        return TransientError(provider, status=status)
    if status in (401, 403):
        return AuthError(provider, status=status)
    return InvalidRequest(provider, status=status)


def _rate_limited(provider: str, headers: Mapping[str, str], status: int) -> RateLimited:
    retry_after = _retry_after(headers)
    if retry_after is not None:
        return RateLimited(provider, retry_after, from_provider=True, status=status)
    return RateLimited(provider, DEFAULT_RATE_LIMIT_WAIT_S, from_provider=False, status=status)


_DURATION_S = re.compile(r"^(\d+(?:\.\d+)?)s$")
_PER_DAY = re.compile(r"per_?day", re.IGNORECASE)


def classify_gemini(status: int, headers: Mapping[str, str], body: Any) -> LLMError:
    error = _error_obj(body)
    details = [d for d in error.get("details", []) if isinstance(d, dict)]
    if status == 429:
        # Phase 2 amendment: a daily quota is never retried. Gemini names the exhausted quota
        # in QuotaFailure violations, e.g. quotaId GenerateRequestsPerDayPerProjectPerModel.
        for detail in details:
            if not str(detail.get("@type", "")).endswith("google.rpc.QuotaFailure"):
                continue
            for violation in detail.get("violations", []):
                if not isinstance(violation, dict):
                    continue
                names = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}"
                if _PER_DAY.search(names):
                    return QuotaExhausted("gemini", status=status, period="day")
        # Per-minute: RetryInfo.retryDelay (e.g. "34s") is Gemini's retry-after.
        for detail in details:
            if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
                match = _DURATION_S.match(str(detail.get("retryDelay", "")))
                if match:
                    return RateLimited(
                        "gemini", float(match.group(1)), from_provider=True, status=status
                    )
        return _rate_limited("gemini", headers, status)
    if status == 402:
        # Prepaid credit depleted: a quota condition, so fall through to the fallback.
        return QuotaExhausted("gemini", status=status, period="day")
    if status == 400 and any(d.get("reason") == "API_KEY_INVALID" for d in details):
        # Gemini answers a bad key with 400 INVALID_ARGUMENT, not 401.
        return AuthError("gemini", status=status)
    return _by_status("gemini", status)


_GROQ_DAILY = re.compile(r"per day|\((?:RPD|TPD)\)", re.IGNORECASE)
_GROQ_TRY_AGAIN = re.compile(
    r"try again in (?:(?P<h>\d+)h)?(?:(?P<m>\d+)m(?!s))?(?:(?P<s>\d+(?:\.\d+)?)s)?"
    r"|try again in (?P<ms>\d+(?:\.\d+)?)ms",
    re.IGNORECASE,
)


def _groq_reset_in_s(message: str) -> float | None:
    match = _GROQ_TRY_AGAIN.search(message)
    if not match:
        return None
    if match.group("ms"):
        return float(match.group("ms")) / 1000
    if not any(match.group(g) for g in ("h", "m", "s")):
        return None
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    return round(hours * 3600 + minutes * 60 + float(match.group("s") or 0), 3)


def classify_groq(status: int, headers: Mapping[str, str], body: Any) -> LLMError:
    if status == 429:
        message = str(_error_obj(body).get("message", ""))
        # Phase 2 amendment: Groq's daily limits (requests/tokens per day) are never retried,
        # even though the 429 also carries retry-after. Its "try again in" is the reset time.
        if _GROQ_DAILY.search(message):
            return QuotaExhausted(
                "groq", status=status, period="day", reset_in_s=_groq_reset_in_s(message)
            )
        return _rate_limited("groq", headers, status)
    return _by_status("groq", status)


_ANTHROPIC_WORKSPACE_LIMIT = re.compile(
    r"You have reached your specified .*API usage limits", re.IGNORECASE
)
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:\D{1,5}(\d{2}):(\d{2}))?")


def _anthropic_reset_at(message: str) -> datetime | None:
    match = _ISO_DATE.search(message)
    if not match:
        return None
    year, month, day, hour, minute = (int(g) if g else 0 for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


def classify_anthropic(status: int, headers: Mapping[str, str], body: Any) -> LLMError:
    error = _error_obj(body)
    message = str(error.get("message", ""))
    error_type = str(error.get("type", ""))
    # Phase 3, "Spend control": the workspace-limit 400 (narrow match) and the tier-cap 429
    # `enforced_spend_limit_reached` are never retried. Both are monthly spend limits: the
    # date comes from the message, else the 1st of next month UTC (set by the gateway).
    if status == 400 and _ANTHROPIC_WORKSPACE_LIMIT.search(message):
        return QuotaExhausted(
            "anthropic", status=status, period="month", reset_at=_anthropic_reset_at(message)
        )
    if status == 429 and "enforced_spend_limit_reached" in f"{error_type} {message}":
        return QuotaExhausted(
            "anthropic", status=status, period="month", reset_at=_anthropic_reset_at(message)
        )
    if status == 429:
        return _rate_limited("anthropic", headers, status)
    return _by_status("anthropic", status)
