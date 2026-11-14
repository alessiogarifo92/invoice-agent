import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import main as main_module
from app.config import Settings
from app.llm_ping import ping
from app.main import create_app, run_startup_ping
from app.observability import (
    build_tracer_provider,
    langfuse_auth_header,
    langfuse_otlp_endpoint,
)

FAKE_KEY = "test-gemini-key-not-real"

GOOD_BODY = {
    "candidates": [{"content": {"parts": [{"text": "pong"}]}}],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 1},
}


def _tracer() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def _only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def _all_span_text(span: ReadableSpan) -> str:
    return json.dumps(dict(span.attributes or {})) + str(span.status.description)


def test_ping_success_records_genai_attributes() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        return httpx.Response(200, json=GOOD_BODY)

    provider, exporter = _tracer()
    settings = Settings(gemini_api_key=FAKE_KEY, llm_model_extract="gemini-test")
    result = ping(settings, _client(httpx.MockTransport(handler)), provider.get_tracer("t"))

    assert result.ok and result.text == "pong"
    assert (result.input_tokens, result.output_tokens) == (7, 1)
    assert seen["url"].endswith("/models/gemini-test:generateContent")
    assert seen["key"] == FAKE_KEY
    span = _only_span(exporter)
    attrs = dict(span.attributes or {})
    assert attrs["gen_ai.system"] == "gemini"
    assert attrs["gen_ai.request.model"] == "gemini-test"
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert attrs["gen_ai.usage.output_tokens"] == 1
    # The API key never ends up on the span.
    assert FAKE_KEY not in _all_span_text(span)


@pytest.mark.parametrize("status", [400, 429, 500])
def test_ping_http_error_is_reported_not_raised(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"echo {FAKE_KEY}"}})

    provider, exporter = _tracer()
    settings = Settings(gemini_api_key=FAKE_KEY)
    result = ping(settings, _client(httpx.MockTransport(handler)), provider.get_tracer("t"))

    assert not result.ok
    assert result.error == f"http_{status}"
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert FAKE_KEY not in _all_span_text(span) and FAKE_KEY not in result.error


def test_ping_network_error_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider, exporter = _tracer()
    result = ping(
        Settings(gemini_api_key=FAKE_KEY),
        _client(httpx.MockTransport(handler)),
        provider.get_tracer("t"),
    )
    assert not result.ok and result.error == "ConnectError"
    assert _only_span(exporter).status.status_code == StatusCode.ERROR


def test_ping_non_json_body_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    provider, _ = _tracer()
    result = ping(
        Settings(gemini_api_key=FAKE_KEY),
        _client(httpx.MockTransport(handler)),
        provider.get_tracer("t"),
    )
    assert not result.ok and result.error == "JSONDecodeError"


def test_ping_unexpected_shape_is_ok_with_empty_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    provider, _ = _tracer()
    result = ping(
        Settings(gemini_api_key=FAKE_KEY),
        _client(httpx.MockTransport(handler)),
        provider.get_tracer("t"),
    )
    assert result.ok and result.text == "" and result.input_tokens is None


def test_ping_without_key_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected without a key")

    provider, exporter = _tracer()
    result = ping(
        Settings(gemini_api_key=None),
        _client(httpx.MockTransport(handler)),
        provider.get_tracer("t"),
    )
    assert not result.ok and result.error == "missing_api_key"
    assert _only_span(exporter).status.status_code == StatusCode.ERROR


def test_langfuse_endpoint_and_auth_header() -> None:
    assert (
        langfuse_otlp_endpoint("https://cloud.langfuse.com/")
        == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    )
    header = langfuse_auth_header("pk-lf-1", "sk-lf-2")
    assert header.startswith("Basic ")
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == "pk-lf-1:sk-lf-2"


def test_tracing_is_off_without_both_keys() -> None:
    assert build_tracer_provider(Settings(langfuse_public_key=None)) is None
    assert (
        build_tracer_provider(Settings(langfuse_public_key="pk", langfuse_secret_key=None)) is None
    )
    provider = build_tracer_provider(Settings(langfuse_public_key="pk", langfuse_secret_key="sk"))
    assert provider is not None
    provider.shutdown()


def test_run_startup_ping_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exporter = _tracer()
    flushed: list[bool] = []

    def fake_flush(timeout_millis: int = 30000) -> bool:
        flushed.append(True)
        return True

    monkeypatch.setattr(provider, "force_flush", fake_flush)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GOOD_BODY)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler))
    )
    result = run_startup_ping(Settings(gemini_api_key=FAKE_KEY), provider)
    assert result.ok and flushed == [True]
    assert len(exporter.get_finished_spans()) == 1


def test_startup_does_not_ping_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(main_module, "run_startup_ping", lambda *a: calls.append(a))
    settings = Settings(
        llm_ping_on_startup=False, langfuse_public_key="pk", langfuse_secret_key="sk"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
    assert calls == []


def test_startup_pings_once_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(main_module, "run_startup_ping", lambda *a: calls.append(a))
    settings = Settings(
        llm_ping_on_startup=True,
        gemini_api_key=FAKE_KEY,
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        app.state.startup_ping_thread.join(timeout=5)
    assert len(calls) == 1
