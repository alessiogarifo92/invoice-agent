"""LLMGateway with fake providers, the in-memory ledger, a fake clock and a fake sleep."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.config import Settings
from app.llm.errors import (
    AuthError,
    InvalidRequest,
    QuotaExhausted,
    QuotaUnavailable,
    RateLimited,
    SchemaError,
    TransientError,
)
from app.llm.gateway import LLMGateway
from app.llm.ledger import InMemoryLedger, LLMCall
from app.llm.types import AssistantTurn, Limits, ToolCall, ToolSpec, Usage
from tests.llm_support import FAKE_KEY, FakeProvider, Invoice

NOW = datetime(2026, 11, 14, 15, 30, tzinfo=UTC)
GOOD = '{"invoice_number": "INV-1", "total": 10}'
INVOICE_TEXT = "Invoice INV-1 from Acme, IBAN NL00SYNT0000000000"
LOOKUP = ToolSpec("get_vendor", "Look up a vendor", {"type": "object", "properties": {}})
PROPOSE = ToolSpec("propose_payment", "Final proposal", {"type": "object", "properties": {}})


class FakeClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Harness:
    def __init__(
        self,
        primary: FakeProvider,
        fallback: FakeProvider | None = None,
        **settings: Any,
    ):
        self.primary = primary
        self.fallback = fallback
        self.ledger = InMemoryLedger()
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self.exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.settings = Settings(gemini_api_key=FAKE_KEY, groq_api_key=FAKE_KEY, **settings)
        self.gateway = LLMGateway(
            primary,
            fallback,
            self.ledger,
            self.settings,
            tracer_provider.get_tracer("test"),
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def extract(self, **kw: Any) -> Any:
        return self.gateway.complete_structured("extract", "rules", INVOICE_TEXT, Invoice, **kw)

    def seed(self, provider: str, n: int, *, at: datetime = NOW, token: str | None = None) -> None:
        for _ in range(n):
            self.ledger.record(
                LLMCall(
                    provider=provider,
                    model="m",
                    purpose="seed",
                    status="ok",
                    created_at=at,
                    demo_token_hash=token,
                )
            )

    def rows(self) -> list[LLMCall]:
        return [row for row in self.ledger.calls if row.purpose != "seed"]


def _gemini(*script: Any) -> FakeProvider:
    return FakeProvider("gemini", *script)


def _groq(*script: Any) -> FakeProvider:
    return FakeProvider("groq", *script)


def _quota(provider: str, **kw: Any) -> QuotaExhausted:
    return QuotaExhausted(provider, status=429, **kw)


# --- success, ledger, models --------------------------------------------------------------


def test_success_records_one_ok_row_with_tokens_and_the_extract_model() -> None:
    h = Harness(_gemini(GOOD))
    result = h.extract(invoice_id="0b5c2b7e-0000-4000-8000-000000000001", demo_token_hash="tok")

    assert result.parsed == Invoice(invoice_number="INV-1", total=10)
    assert h.primary.calls[0]["model"] == "gemini-3.5-flash-lite"
    (row,) = h.rows()
    assert (row.provider, row.model, row.purpose, row.status) == (
        "gemini",
        "gemini-3.5-flash-lite",
        "extract",
        "ok",
    )
    assert (row.input_tokens, row.output_tokens, row.error_class) == (100, 20, None)
    assert row.invoice_id == "0b5c2b7e-0000-4000-8000-000000000001"
    assert row.demo_token_hash == "tok" and row.created_at == NOW
    assert row.latency_ms is not None and row.latency_ms >= 0


def test_failed_call_still_writes_a_row_with_the_error_class() -> None:
    h = Harness(_gemini(InvalidRequest("gemini", status=400)))
    with pytest.raises(InvalidRequest):
        h.extract(invoice_id="0b5c2b7e-0000-4000-8000-000000000001")
    (row,) = h.rows()
    assert row.status == "error" and row.error_class == "InvalidRequest"
    assert row.invoice_id == "0b5c2b7e-0000-4000-8000-000000000001"


@pytest.mark.parametrize("text", ["not json", '{"invoice_number": "INV-1"}'])
def test_schema_error_is_surfaced_not_retried_and_keeps_tokens(text: str) -> None:
    h = Harness(_gemini(text), _groq(GOOD))
    with pytest.raises(SchemaError):
        h.extract()
    (row,) = h.rows()
    assert row.status == "error" and row.error_class == "SchemaError"
    assert (row.input_tokens, row.output_tokens) == (100, 20)
    assert h.sleeps == [] and h.fallback is not None and h.fallback.calls == []


@pytest.mark.parametrize("error", [InvalidRequest("gemini", status=400), AuthError("gemini")])
def test_client_errors_are_not_retried_and_do_not_fall_back(error: Exception) -> None:
    h = Harness(_gemini(error), _groq(GOOD))
    with pytest.raises(type(error)):
        h.extract()
    assert len(h.primary.calls) == 1 and h.sleeps == []
    assert h.fallback is not None and h.fallback.calls == []


# --- retries ------------------------------------------------------------------------------


def test_rate_limited_is_retried_after_retry_after_with_the_cap() -> None:
    h = Harness(
        _gemini(
            RateLimited("gemini", 5, from_provider=True),
            RateLimited("gemini", 45, from_provider=True),
            GOOD,
        )
    )
    assert h.extract().parsed.invoice_number == "INV-1"
    assert h.sleeps == [5, 20]
    assert [r.status for r in h.rows()] == ["error", "error", "ok"]
    assert h.rows()[0].error_class == "RateLimited"


def test_rate_limited_stops_after_two_retries() -> None:
    h = Harness(_gemini(*[RateLimited("gemini", 1, from_provider=True) for _ in range(3)]), _groq())
    with pytest.raises(RateLimited):
        h.extract()
    assert h.sleeps == [1, 1] and len(h.primary.calls) == 3 and len(h.rows()) == 3
    assert h.fallback is not None and h.fallback.calls == []


def test_transient_errors_are_retried_with_small_backoff_then_raised() -> None:
    h = Harness(_gemini(TransientError("gemini", status=503), TransientError("gemini"), GOOD))
    h.extract()
    assert h.sleeps == [1.0, 2.0]

    h = Harness(_gemini(*[TransientError("gemini", status=529) for _ in range(3)]))
    with pytest.raises(TransientError):
        h.extract()
    assert len(h.primary.calls) == 3


def test_unannounced_429_waits_the_default_once_then_falls_back() -> None:
    h = Harness(
        _gemini(
            RateLimited("gemini", 10, from_provider=False),
            RateLimited("gemini", 10, from_provider=False),
        ),
        _groq(GOOD),
    )
    result = h.extract()
    assert result.provider == "groq"
    assert h.sleeps == [10]
    # A guess is not a confirmed daily quota: the primary is not blocked for the day.
    assert h.ledger.exhausted_until("gemini") is None


# --- fallback -----------------------------------------------------------------------------


def test_primary_quota_falls_back_once_and_blocks_the_primary_until_pacific_midnight() -> None:
    h = Harness(_gemini(_quota("gemini")), _groq(GOOD))
    result = h.extract()
    assert result.provider == "groq" and result.model == "openai/gpt-oss-20b"
    # Gemini resets at midnight Pacific: 2026-11-15 00:00 PST is 08:00 UTC.
    assert h.ledger.exhausted_until("gemini") == datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    assert [(r.provider, r.status) for r in h.rows()] == [("gemini", "error"), ("groq", "ok")]
    assert h.rows()[0].error_class == "QuotaExhausted"
    assert h.sleeps == []


def test_both_quotas_exhausted_raise_quota_unavailable_after_exactly_two_rows() -> None:
    h = Harness(_gemini(_quota("gemini")), _groq(_quota("groq")))
    with pytest.raises(QuotaUnavailable) as info:
        h.extract()
    assert info.value.reason == "providers" and info.value.providers == ["gemini", "groq"]
    assert len(h.rows()) == 2
    assert len(h.primary.calls) == 1
    assert h.fallback is not None and len(h.fallback.calls) == 1
    assert h.ledger.exhausted_until("groq") == datetime(2026, 11, 15, tzinfo=UTC)


def test_no_fallback_configured_raises_quota_unavailable() -> None:
    h = Harness(_gemini(_quota("gemini")))
    with pytest.raises(QuotaUnavailable):
        h.extract()
    assert len(h.rows()) == 1


def test_fallback_errors_never_go_back_to_the_primary() -> None:
    h = Harness(
        _gemini(_quota("gemini")),
        _groq(*[RateLimited("groq", 2, from_provider=True) for _ in range(3)]),
    )
    with pytest.raises(RateLimited):
        h.extract()
    assert len(h.primary.calls) == 1
    assert h.fallback is not None and len(h.fallback.calls) == 3


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_quota("gemini", reset_in_s=90), NOW + timedelta(seconds=90)),
        (
            _quota("gemini", reset_at=datetime(2026, 11, 20, 8, tzinfo=UTC)),
            datetime(2026, 11, 20, 8, tzinfo=UTC),
        ),
        (_quota("gemini", period="month"), datetime(2026, 12, 1, tzinfo=UTC)),
    ],
)
def test_exhausted_until_uses_the_time_the_error_carries(
    error: QuotaExhausted, expected: datetime
) -> None:
    h = Harness(_gemini(error), _groq(GOOD))
    h.extract()
    assert h.ledger.exhausted_until("gemini") == expected


def test_month_fallback_rolls_over_the_year() -> None:
    h = Harness(_gemini(_quota("gemini", period="month")))
    h.clock.now = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
    with pytest.raises(QuotaUnavailable):
        h.extract()
    assert h.ledger.exhausted_until("gemini") == datetime(2027, 1, 1, tzinfo=UTC)


# --- caps ---------------------------------------------------------------------------------


def test_soft_cap_stops_the_primary_before_any_request() -> None:
    h = Harness(_gemini(), _groq(GOOD), llm_daily_soft_cap_gemini=3)
    h.seed("gemini", 3)
    assert h.extract().provider == "groq"
    assert h.primary.calls == []
    assert [r.provider for r in h.rows()] == ["groq"]
    # A soft cap is our own count: it never writes budget_state.
    assert h.ledger.exhausted_until("gemini") is None


def test_soft_cap_on_both_providers_raises_quota_unavailable_without_requests() -> None:
    h = Harness(_gemini(), _groq(), llm_daily_soft_cap_gemini=2, llm_daily_soft_cap_groq=1)
    h.seed("gemini", 2)
    h.seed("groq", 1)
    with pytest.raises(QuotaUnavailable):
        h.extract()
    assert h.rows() == []


def test_rows_from_yesterday_do_not_count_toward_the_soft_cap() -> None:
    h = Harness(_gemini(GOOD), llm_daily_soft_cap_gemini=2)
    h.seed("gemini", 5, at=datetime(2026, 11, 13, 23, 59, 59, tzinfo=UTC))
    h.seed("gemini", 1)
    assert h.extract().provider == "gemini"


def test_exhausted_until_in_the_future_skips_the_provider() -> None:
    h = Harness(_gemini(), _groq(GOOD))
    h.ledger.set_exhausted_until("gemini", NOW + timedelta(minutes=1))
    assert h.extract().provider == "groq" and h.primary.calls == []


def test_exhausted_until_in_the_past_does_not() -> None:
    h = Harness(_gemini(GOOD), _groq())
    h.ledger.set_exhausted_until("gemini", NOW - timedelta(seconds=1))
    assert h.extract().provider == "gemini"


def test_demo_token_sub_cap_blocks_only_that_token() -> None:
    h = Harness(_gemini(GOOD), _groq(GOOD), llm_daily_cap_per_demo_token=2)
    h.seed("gemini", 1, token="tok-a")
    h.seed("groq", 1, token="tok-a")
    with pytest.raises(QuotaUnavailable) as info:
        h.extract(demo_token_hash="tok-a")
    assert info.value.reason == "demo_token_cap"
    assert h.primary.calls == [] and h.fallback is not None and h.fallback.calls == []
    assert h.extract(demo_token_hash="tok-b").provider == "gemini"


def test_demo_token_sub_cap_can_be_turned_off() -> None:
    h = Harness(_gemini(GOOD), llm_daily_cap_per_demo_token=None)
    h.seed("gemini", 100, token="tok-a")
    assert h.extract(demo_token_hash="tok-a").provider == "gemini"


# --- spans --------------------------------------------------------------------------------


def test_one_span_per_attempt_with_genai_attributes_and_no_prompt_or_key() -> None:
    h = Harness(_gemini(TransientError("gemini", status=503), GOOD))
    h.extract()
    spans = h.exporter.get_finished_spans()
    assert [s.name for s in spans] == ["llm.extract", "llm.extract"]
    failed, ok = (dict(s.attributes or {}) for s in spans)
    assert ok["gen_ai.system"] == "gemini"
    assert ok["gen_ai.request.model"] == "gemini-3.5-flash-lite"
    assert ok["gen_ai.operation.name"] == "chat"
    assert ok["gen_ai.usage.input_tokens"] == 100 and ok["gen_ai.usage.output_tokens"] == 20
    assert ok["langfuse.observation.type"] == "generation"
    assert ok["llm.input_chars"] == len("rules") + len(INVOICE_TEXT)
    assert "error.type" not in ok
    assert failed["error.type"] == "TransientError"
    assert spans[0].status.status_code == StatusCode.ERROR
    for span in spans:
        dumped = json.dumps(dict(span.attributes or {})) + str(span.status.description)
        dumped += json.dumps([e.name for e in span.events])
        assert FAKE_KEY not in dumped and INVOICE_TEXT not in dumped and "IBAN" not in dumped


# --- run_tools ----------------------------------------------------------------------------


def _step(*calls: ToolCall, text: str = "") -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=list(calls), usage=Usage(50, 5), raw=None)


def _tools(h: Harness, limits: Limits | None = None) -> Any:
    return h.gateway.run_tools(
        "agent",
        "rules",
        INVOICE_TEXT,
        [LOOKUP, PROPOSE],
        {"get_vendor": lambda args: {"name": "Acme"}},
        "propose_payment",
        limits or Limits(),
        invoice_id="0b5c2b7e-0000-4000-8000-000000000001",
    )


def test_run_tools_records_a_row_and_span_per_step_with_the_agent_model() -> None:
    h = Harness(
        _gemini(
            _step(ToolCall("1", "get_vendor", {})),
            _step(ToolCall("2", "propose_payment", {"action": "approve"})),
        )
    )
    result = _tools(h)
    assert result.stop_reason == "completed" and result.proposal == {"action": "approve"}
    assert result.provider == "gemini" and result.model == "gemini-3.6-flash"
    assert [c["model"] for c in h.primary.calls] == ["gemini-3.6-flash"] * 2
    assert [(r.purpose, r.status) for r in h.rows()] == [("agent", "ok"), ("agent", "ok")]
    assert [s.name for s in h.exporter.get_finished_spans()] == ["llm.agent", "llm.agent"]


def test_run_tools_retries_a_step_without_rerunning_the_loop() -> None:
    h = Harness(
        _gemini(
            _step(ToolCall("1", "get_vendor", {})),
            TransientError("gemini", status=500),
            _step(ToolCall("2", "propose_payment", {"action": "clarify"})),
        )
    )
    result = _tools(h)
    assert result.stop_reason == "completed" and len(result.tool_calls) == 2
    assert h.sleeps == [1.0] and len(h.rows()) == 3


def test_run_tools_quota_mid_loop_restarts_once_on_the_fallback() -> None:
    h = Harness(
        _gemini(_step(ToolCall("1", "get_vendor", {})), _quota("gemini")),
        _groq(_step(ToolCall("g", "propose_payment", {"action": "approve"}))),
    )
    result = _tools(h)
    assert result.provider == "groq" and result.model == "openai/gpt-oss-120b"
    assert result.stop_reason == "completed"
    # The fallback starts a fresh conversation: Gemini's history is not portable.
    assert h.fallback is not None and h.fallback.calls[0]["history"] == []


def test_run_tools_both_quotas_raise_quota_unavailable() -> None:
    h = Harness(_gemini(_quota("gemini")), _groq(_quota("groq")))
    with pytest.raises(QuotaUnavailable):
        _tools(h)
    assert len(h.rows()) == 2


def test_run_tools_soft_cap_reached_mid_loop_moves_to_the_fallback() -> None:
    h = Harness(
        _gemini(_step(ToolCall("1", "get_vendor", {}))),
        _groq(_step(ToolCall("g", "propose_payment", {"action": "block"}))),
        llm_daily_soft_cap_gemini=1,
    )
    result = _tools(h)
    assert result.provider == "groq" and len(h.primary.calls) == 1


def test_run_tools_non_quota_error_ends_the_loop_with_error() -> None:
    h = Harness(_gemini(AuthError("gemini", status=401)), _groq())
    result = _tools(h)
    assert result.stop_reason == "error" and result.error_class == "AuthError"
    assert h.fallback is not None and h.fallback.calls == []


def test_gemini_daily_reset_is_pacific_midnight() -> None:
    from datetime import UTC, datetime

    from app.llm.gateway import next_pacific_midnight

    # 2026-11-14 16:00 UTC is 08:00 PST: reset at 2026-11-15 00:00 PST = 08:00 UTC.
    assert next_pacific_midnight(datetime(2026, 11, 14, 16, 0, tzinfo=UTC)) == datetime(
        2026, 11, 15, 8, 0, tzinfo=UTC
    )
    # 07:30 UTC is still 23:30 PST the previous day: reset 30 minutes later.
    assert next_pacific_midnight(datetime(2026, 11, 15, 7, 30, tzinfo=UTC)) == datetime(
        2026, 11, 15, 8, 0, tzinfo=UTC
    )
    # Summer (PDT, UTC-7).
    assert next_pacific_midnight(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == datetime(
        2026, 7, 2, 7, 0, tzinfo=UTC
    )
