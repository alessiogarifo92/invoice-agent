"""Walking-skeleton LLM call: one Gemini request, traced as a GenAI span.

Throwaway by design (T0). The provider-agnostic gateway arrives in T5.
Run by hand with: uv run python -m app.llm_ping
"""

import time
from dataclasses import dataclass

import httpx
from opentelemetry.trace import Status, StatusCode, Tracer

from app.config import Settings, get_settings

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PING_PROMPT = "Reply with the single word: pong"


@dataclass(frozen=True)
class PingResult:
    ok: bool
    model: str
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    error: str = ""


def _extract_text(body: dict[str, object]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
    parts = content.get("parts", []) if isinstance(content, dict) else []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _usage(body: dict[str, object]) -> tuple[int | None, int | None]:
    usage = body.get("usageMetadata")
    if not isinstance(usage, dict):
        return None, None
    return usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


def ping(settings: Settings, client: httpx.Client, tracer: Tracer) -> PingResult:
    """Make one Gemini call inside a GenAI span. Never raises; errors go in the result."""
    model = settings.llm_model_extract
    with tracer.start_as_current_span("llm.ping") as span:
        span.set_attribute("gen_ai.system", "gemini")
        span.set_attribute("gen_ai.operation.name", "generate_content")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("langfuse.observation.type", "generation")
        if not settings.gemini_api_key:
            span.set_status(Status(StatusCode.ERROR, "missing_api_key"))
            return PingResult(ok=False, model=model, error="missing_api_key")
        start = time.monotonic()
        try:
            response = client.post(
                f"{GEMINI_BASE_URL}/models/{model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key},
                json={"contents": [{"parts": [{"text": PING_PROMPT}]}]},
                timeout=30.0,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            if response.status_code != 200:
                # Status code only: the body can echo request details, never log it.
                error = f"http_{response.status_code}"
                span.set_status(Status(StatusCode.ERROR, error))
                return PingResult(ok=False, model=model, latency_ms=latency_ms, error=error)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            error = type(exc).__name__
            span.set_status(Status(StatusCode.ERROR, error))
            return PingResult(ok=False, model=model, error=error)
        text = _extract_text(body)
        input_tokens, output_tokens = _usage(body)
        if input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        if output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        span.set_attribute("gen_ai.response.text_length", len(text))
        return PingResult(
            ok=True,
            model=model,
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )


def main() -> int:
    from opentelemetry import trace

    from app.observability import build_tracer_provider

    settings = get_settings()
    provider = build_tracer_provider(settings)
    tracer = provider.get_tracer(__name__) if provider else trace.get_tracer(__name__)
    with httpx.Client() as client:
        result = ping(settings, client, tracer)
    if provider:
        provider.force_flush()
        provider.shutdown()
    print(f"ok={result.ok} model={result.model} latency_ms={result.latency_ms} {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
