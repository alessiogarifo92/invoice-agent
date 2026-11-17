"""Gemini adapter: request shape and response parsing over a mocked transport."""

from typing import Any

import httpx
import pytest

from app.llm.errors import QuotaExhausted, RateLimited, SchemaError, TransientError
from app.llm.gemini import GEMINI_BASE_URL, GeminiProvider
from app.llm.types import Limits, ToolCall, ToolCallRecord, ToolResultsTurn, ToolSpec, Usage
from tests.llm_support import FAKE_KEY, Invoice, Recorder

USAGE = {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 12}
TOOL = ToolSpec(
    "get_vendor",
    "Look up a vendor by id",
    {"type": "object", "properties": {"vendor_id": {"type": "string"}}, "required": ["vendor_id"]},
)


def _reply(*parts: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": list(parts)}}],
            "usageMetadata": USAGE,
        },
    )


def _provider(recorder: Recorder) -> GeminiProvider:
    return GeminiProvider(FAKE_KEY, recorder.client())


def test_structured_request_shape() -> None:
    recorder = Recorder(_reply({"text": '{"invoice_number": "INV-1", "total": 12.5}'}))
    result = _provider(recorder).complete_structured("system rules", "invoice text", Invoice, "g-1")

    request = recorder.requests[0]
    assert str(request.url) == f"{GEMINI_BASE_URL}/models/g-1:generateContent"
    assert request.method == "POST"
    assert request.headers["x-goog-api-key"] == FAKE_KEY
    # The key travels in the header only.
    assert FAKE_KEY not in str(request.url) and FAKE_KEY not in request.content.decode()
    body = recorder.body()
    assert body["systemInstruction"] == {"parts": [{"text": "system rules"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "invoice text"}]}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["properties"].keys() == {"invoice_number", "total"}
    assert "tools" not in body

    assert result.parsed == Invoice(invoice_number="INV-1", total=12.5)
    assert result.raw_text == '{"invoice_number": "INV-1", "total": 12.5}'
    # Thinking tokens are billed as output.
    assert result.usage == Usage(120, 42)
    assert (result.provider, result.model) == ("gemini", "g-1")


def test_structured_skips_thought_parts() -> None:
    recorder = Recorder(
        _reply(
            {"text": "pondering", "thought": True},
            {"text": '{"invoice_number": "A", '},
            {"text": '"total": 1}'},
        )
    )
    result = _provider(recorder).complete_structured("s", "u", Invoice, "g-1")
    assert result.parsed.invoice_number == "A"


@pytest.mark.parametrize("text", ["not json", '{"invoice_number": "A"}', ""])
def test_structured_invalid_output_is_schema_error_with_usage(text: str) -> None:
    recorder = Recorder(_reply({"text": text}))
    with pytest.raises(SchemaError) as info:
        _provider(recorder).complete_structured("s", "u", Invoice, "g-1")
    assert info.value.usage == Usage(120, 42)
    # The model's output never ends up in the message.
    assert str(info.value) == "gemini: schema_error"


def test_no_candidates_is_schema_error() -> None:
    recorder = Recorder(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(SchemaError):
        _provider(recorder).complete_structured("s", "u", Invoice, "g-1")


def test_http_errors_are_classified() -> None:
    per_minute = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}],
        }
    }
    per_day = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}],
                }
            ],
        }
    }
    recorder = Recorder(
        httpx.Response(429, json=per_minute),
        httpx.Response(429, json=per_day),
        httpx.Response(503, text="<html>"),
        httpx.ConnectError("down"),
    )
    provider = _provider(recorder)
    with pytest.raises(RateLimited):
        provider.complete_structured("s", "u", Invoice, "g")
    with pytest.raises(QuotaExhausted):
        provider.complete_structured("s", "u", Invoice, "g")
    with pytest.raises(TransientError):
        provider.complete_structured("s", "u", Invoice, "g")
    with pytest.raises(TransientError):
        provider.complete_structured("s", "u", Invoice, "g")


def test_chat_step_sends_tools_and_parses_function_calls() -> None:
    call_part = {
        "functionCall": {"id": "fc-1", "name": "get_vendor", "args": {"vendor_id": "V1"}},
        "thoughtSignature": "sig-A",
    }
    recorder = Recorder(_reply({"text": "Checking."}, call_part))
    turn = _provider(recorder).chat_step("s", "u", [], [TOOL], "g-agent")

    body = recorder.body()
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_vendor",
                    "description": "Look up a vendor by id",
                    "parametersJsonSchema": TOOL.parameters,
                }
            ]
        }
    ]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    assert "generationConfig" not in body
    assert turn.text == "Checking."
    assert turn.tool_calls == [ToolCall("fc-1", "get_vendor", {"vendor_id": "V1"})]
    assert turn.usage == Usage(120, 42)


def test_history_echoes_model_parts_verbatim_and_returns_function_responses() -> None:
    call_part = {
        "functionCall": {"id": "fc-1", "name": "get_vendor", "args": {"vendor_id": "V1"}},
        "thoughtSignature": "sig-A",
    }
    no_id_part = {"functionCall": {"name": "get_vendor", "args": {}}}
    recorder = Recorder(_reply(call_part, no_id_part), _reply({"text": "done"}))
    provider = _provider(recorder)
    first = provider.chat_step("s", "u", [], [TOOL], "g")
    results = ToolResultsTurn(
        [
            ToolCallRecord(first.tool_calls[0], output={"name": "Acme"}),
            ToolCallRecord(first.tool_calls[1], output={"error": "unknown"}, is_error=True),
        ]
    )
    provider.chat_step("s", "u", [first, results], [TOOL], "g")

    contents = recorder.body()["contents"]
    assert contents[1] == {"role": "model", "parts": [call_part, no_id_part]}
    assert contents[2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "get_vendor",
                    "id": "fc-1",
                    "response": {"result": {"name": "Acme"}},
                }
            },
            # The model sent no id for this call, so none is invented in the reply.
            {
                "functionResponse": {
                    "name": "get_vendor",
                    "response": {"error": {"error": "unknown"}},
                }
            },
        ],
    }


def test_run_tools_drives_the_shared_loop() -> None:
    propose = ToolSpec("propose_payment", "Propose", {"type": "object", "properties": {}})
    recorder = Recorder(
        _reply({"functionCall": {"id": "1", "name": "get_vendor", "args": {"vendor_id": "V"}}}),
        _reply({"functionCall": {"id": "2", "name": "propose_payment", "args": {"a": "approve"}}}),
    )
    result = _provider(recorder).run_tools(
        "s",
        "u",
        [TOOL, propose],
        {"get_vendor": lambda args: {"ok": True}},
        "propose_payment",
        Limits(),
        "g",
    )
    assert result.stop_reason == "completed" and result.proposal == {"a": "approve"}
    assert result.usage == Usage(240, 84)
