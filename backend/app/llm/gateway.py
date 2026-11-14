"""The single entry point for every LLM call: quota guard, retries, fallback, ledger, spans.

Phase 2 amendment, "Spend control becomes quota control":
- before each provider request, the per-provider daily soft cap (llm_calls rows of the
  current UTC day), budget_state.exhausted_until and the per-demo-token sub-cap are checked;
- a daily-quota error is never retried: it marks the provider exhausted and falls through to
  the fallback once; when the fallback is out too, QuotaUnavailable tells the caller to park
  the invoice in awaiting_quota (see `mark_awaiting_quota`);
- a per-minute 429 with a delay, a 5xx or a 529 is retried (at most twice per request).
Every provider request writes one llm_calls row and one `llm.<purpose>` span, failures too.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from sqlalchemy import Connection, text

from app.config import Settings
from app.db import get_engine
from app.llm.anthropic import AnthropicProvider, anthropic_client
from app.llm.errors import (
    ProviderBlocked,
    QuotaExhausted,
    QuotaUnavailable,
    RateLimited,
    TransientError,
)
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.ledger import Ledger, LLMCall, SqlLedger
from app.llm.provider import Provider, drive_tool_loop
from app.llm.types import (
    AssistantTurn,
    Limits,
    StructuredResult,
    T,
    ToolHandler,
    ToolLoopResult,
    ToolSpec,
    Turn,
    Usage,
)

# Retries per provider request, shared by RateLimited and TransientError.
MAX_RETRIES = 2
# A provider's retry-after is honoured up to this long; beyond it we still wait only this.
MAX_RATE_LIMIT_SLEEP_S = 20.0
TRANSIENT_BACKOFF_S = (1.0, 2.0)


class GatewayConfigError(ValueError):
    """The LLM settings cannot build a gateway (missing key or model)."""


class _HasUsage(Protocol):
    @property
    def usage(self) -> Usage: ...


R = TypeVar("R", bound=_HasUsage)


@dataclass(frozen=True)
class _Context:
    purpose: str
    invoice_id: str | None
    demo_token_hash: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


_PACIFIC = ZoneInfo("America/Los_Angeles")


def next_pacific_midnight(now: datetime) -> datetime:
    """Gemini's free-tier daily quota resets at midnight Pacific time, not UTC."""
    local = now.astimezone(_PACIFIC)
    tomorrow = (local + timedelta(days=1)).date()
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_PACIFIC)
    return midnight.astimezone(UTC)


def _first_of_next_month(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


class LLMGateway:
    def __init__(
        self,
        primary: Provider,
        fallback: Provider | None,
        ledger: Ledger,
        settings: Settings,
        tracer: Tracer,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._primary = primary
        self._fallback = (
            fallback if fallback is not None and fallback.name != primary.name else None
        )
        self._ledger = ledger
        self._settings = settings
        self._tracer = tracer
        self._clock = clock
        self._sleep = sleep

    @property
    def providers(self) -> list[Provider]:
        return [self._primary] + ([self._fallback] if self._fallback else [])

    def complete_structured(
        self,
        purpose: str,
        system: str,
        user: str,
        schema: type[T],
        *,
        invoice_id: str | UUID | None = None,
        demo_token_hash: str | None = None,
    ) -> StructuredResult[T]:
        """One structured completion on the extract model; raises a classified LLMError."""
        ctx = _Context(purpose, _str_id(invoice_id), demo_token_hash)

        def run(provider: Provider) -> StructuredResult[T]:
            model = self._model(provider, "extract")
            return self._request(
                provider,
                model,
                ctx,
                len(system) + len(user),
                lambda: provider.complete_structured(system, user, schema, model),
            )

        return self._route(run)

    def run_tools(
        self,
        purpose: str,
        system: str,
        user: str,
        tools: Sequence[ToolSpec],
        tool_handlers: Mapping[str, ToolHandler],
        terminal_tool: str,
        limits: Limits | None = None,
        *,
        invoice_id: str | UUID | None = None,
        demo_token_hash: str | None = None,
    ) -> ToolLoopResult:
        """The tool loop on the agent model. Quota failures raise; other errors stop with
        stop_reason "error". On a quota failure mid-loop the fallback starts afresh: each
        provider's conversation history (thought signatures, thinking blocks) is its own."""
        ctx = _Context(purpose, _str_id(invoice_id), demo_token_hash)

        def run(provider: Provider) -> ToolLoopResult:
            model = self._model(provider, "agent")

            def step(system: str, user: str, history: Sequence[Turn]) -> AssistantTurn:
                return self._request(
                    provider,
                    model,
                    ctx,
                    len(system) + len(user),
                    lambda: provider.chat_step(system, user, history, tools, model),
                )

            return drive_tool_loop(
                step,
                system=system,
                user=user,
                tools=tools,
                tool_handlers=tool_handlers,
                terminal_tool=terminal_tool,
                limits=limits or Limits(),
                clock=lambda: self._clock().timestamp(),
                provider=provider.name,
                model=model,
            )

        return self._route(run)

    def _route(self, run: Callable[[Provider], R]) -> R:
        # Primary, then the fallback once. The fallback never hands back to the primary.
        tried: list[str] = []
        for provider in self.providers:
            tried.append(provider.name)
            try:
                return run(provider)
            except QuotaExhausted as exc:
                if exc.confirmed:
                    self._ledger.set_exhausted_until(provider.name, self._reset_time(exc))
        raise QuotaUnavailable(tried, "providers")

    def _reset_time(self, exc: QuotaExhausted) -> datetime:
        # Phase 2 amendment / phase 3 "Spend control": the time the error carries, else the
        # next 00:00 UTC for a daily quota, or the 1st of next month for a monthly spend limit.
        now = self._clock()
        if exc.reset_at is not None:
            return exc.reset_at
        if exc.reset_in_s is not None:
            return now + timedelta(seconds=exc.reset_in_s)
        if exc.period == "month":
            return _first_of_next_month(now)
        if exc.provider == "gemini":
            return next_pacific_midnight(now)
        return _day_bounds(now)[1]

    def _model(self, provider: Provider, role: str) -> str:
        extract, agent = self._settings.llm_models(provider.name)
        model = extract if role == "extract" else agent
        if not model:
            raise GatewayConfigError(f"no {role} model configured for {provider.name}")
        return model

    def _check_quota(self, provider: Provider, ctx: _Context) -> None:
        now = self._clock()
        since, until = _day_bounds(now)
        token_cap = self._settings.llm_daily_cap_per_demo_token
        if ctx.demo_token_hash is not None and token_cap is not None:
            used = self._ledger.count_calls(
                since=since, until=until, demo_token_hash=ctx.demo_token_hash
            )
            if used >= token_cap:
                # Counted across providers, so a fallback would not help.
                raise QuotaUnavailable([p.name for p in self.providers], "demo_token_cap")
        exhausted_until = self._ledger.exhausted_until(provider.name)
        if exhausted_until is not None and exhausted_until > now:
            raise ProviderBlocked(provider.name, "exhausted_until")
        used = self._ledger.count_calls(since=since, until=until, provider=provider.name)
        if used >= self._settings.llm_daily_soft_cap(provider.name):
            raise ProviderBlocked(provider.name, "soft_cap")

    def _request(
        self,
        provider: Provider,
        model: str,
        ctx: _Context,
        input_chars: int,
        send: Callable[[], R],
    ) -> R:
        """One provider request with its retries; each attempt is checked, traced, recorded."""
        retries = 0
        guessed_rate_limit = False
        while True:
            self._check_quota(provider, ctx)
            try:
                return self._attempt(provider, model, ctx, input_chars, retries, send)
            except RateLimited as exc:
                if not exc.from_provider:
                    # A 429 with no delay and no daily signal: wait the default once, then
                    # treat it like a quota failure (fallback, but no budget_state).
                    if guessed_rate_limit:
                        raise QuotaExhausted(
                            provider.name, status=exc.status, confirmed=False
                        ) from None
                    guessed_rate_limit = True
                if retries >= MAX_RETRIES:
                    raise
                delay = min(exc.retry_after_s, MAX_RATE_LIMIT_SLEEP_S)
            except TransientError:
                if retries >= MAX_RETRIES:
                    raise
                delay = TRANSIENT_BACKOFF_S[retries]
            retries += 1
            self._sleep(delay)

    def _attempt(
        self,
        provider: Provider,
        model: str,
        ctx: _Context,
        input_chars: int,
        attempt: int,
        send: Callable[[], R],
    ) -> R:
        created_at = self._clock()
        start = time.perf_counter()
        # Exceptions are recorded by hand: the default would attach the traceback to the span.
        with self._tracer.start_as_current_span(
            f"llm.{ctx.purpose}", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("gen_ai.system", provider.name)
            span.set_attribute("gen_ai.operation.name", provider.operation_name)
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("langfuse.observation.type", "generation")
            span.set_attribute("llm.purpose", ctx.purpose)
            span.set_attribute("llm.attempt", attempt)
            # Lengths only: invoice text never goes on a span.
            span.set_attribute("llm.input_chars", input_chars)
            try:
                result = send()
            except Exception as exc:
                usage = getattr(exc, "usage", None) or Usage()
                error_class = type(exc).__name__
                _set_usage(span, usage)
                span.set_attribute("error.type", error_class)
                span.set_status(Status(StatusCode.ERROR, error_class))
                self._record(provider, model, ctx, created_at, start, usage, error_class)
                raise
            _set_usage(span, result.usage)
            self._record(provider, model, ctx, created_at, start, result.usage, None)
            return result

    def _record(
        self,
        provider: Provider,
        model: str,
        ctx: _Context,
        created_at: datetime,
        start: float,
        usage: Usage,
        error_class: str | None,
    ) -> None:
        self._ledger.record(
            LLMCall(
                provider=provider.name,
                model=model,
                purpose=ctx.purpose,
                status="error" if error_class else "ok",
                created_at=created_at,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=int((time.perf_counter() - start) * 1000),
                error_class=error_class,
                invoice_id=ctx.invoice_id,
                demo_token_hash=ctx.demo_token_hash,
            )
        )


def _set_usage(span: Span, usage: Usage) -> None:
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)


def _str_id(invoice_id: str | UUID | None) -> str | None:
    return None if invoice_id is None else str(invoice_id)


def mark_awaiting_quota(conn: Connection, invoice_id: str | UUID) -> bool:
    """Park an invoice until quota is back. Runs in the caller's transaction.

    Bumps `version` (the If-Match token), so a reviewer holding the old version gets a
    conflict. Returns False when no such invoice exists.
    """
    result = conn.execute(
        text(
            "UPDATE invoices SET status = 'awaiting_quota', version = version + 1, "
            "updated_at = now() WHERE id = CAST(:id AS uuid)"
        ),
        {"id": str(invoice_id)},
    )
    return result.rowcount == 1


def _build_provider(name: str, settings: Settings, client: httpx.Client) -> Provider | None:
    """The adapter for `name`, or None when its API key is not set."""
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key, client) if settings.gemini_api_key else None
    if name == "groq":
        return GroqProvider(settings.groq_api_key, client) if settings.groq_api_key else None
    if name == "anthropic":
        if not settings.anthropic_api_key:
            return None
        if not all(settings.llm_models("anthropic")):
            raise GatewayConfigError(
                "Anthropic needs LLM_MODEL_EXTRACT_ANTHROPIC and LLM_MODEL_AGENT_ANTHROPIC"
            )
        return AnthropicProvider(anthropic_client(settings.anthropic_api_key))
    raise GatewayConfigError(f"unknown LLM provider {name!r}")


def build_gateway(
    settings: Settings,
    *,
    http_client: httpx.Client | None = None,
    ledger: Ledger | None = None,
    tracer: Tracer | None = None,
) -> LLMGateway:
    """The gateway from settings: LLM_PROVIDER (needs its key), plus the fallback.

    The fallback is LLM_FALLBACK_PROVIDER, or groq when GROQ_API_KEY is set; "none" turns it
    off, and a fallback whose key is missing is skipped. Anthropic is used only when chosen
    explicitly, as provider or fallback, with ANTHROPIC_API_KEY set.
    """
    client = http_client or httpx.Client()
    primary = _build_provider(settings.llm_provider, settings, client)
    if primary is None:
        raise GatewayConfigError(f"LLM_PROVIDER={settings.llm_provider} needs its API key")
    fallback_name = settings.llm_fallback_provider
    if fallback_name is None:
        fallback_name = "groq" if settings.groq_api_key else "none"
    fallback = (
        None
        if fallback_name in ("none", primary.name)
        else _build_provider(fallback_name, settings, client)
    )
    return LLMGateway(
        primary,
        fallback,
        ledger or SqlLedger(get_engine()),  # Lazy engine: nothing connects until a call.
        settings,
        tracer or trace.get_tracer("invoice-agent.llm"),
    )
