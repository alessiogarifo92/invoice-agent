"""Groq adapter (OpenAI-compatible chat completions) and the strict-schema transform."""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.llm.errors import AuthError, QuotaExhausted, RateLimited, SchemaError
from app.llm.groq import GROQ_CHAT_URL, GroqProvider
from app.llm.schema import strict_json_schema
from app.llm.types import ToolCall, ToolCallRecord, ToolResultsTurn, ToolSpec, Usage
from tests.llm_support import FAKE_KEY, Invoice, Recorder

TOOL = ToolSpec(
    "get_vendor",
    "Look up a vendor by id",
    {"type": "object", "properties": {"vendor_id": {"type": "string"}}},
)
USAGE = {"prompt_tokens": 90, "completion_tokens": 15, "total_tokens": 105}


def _reply(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": USAGE,
        },
    )


class Line(BaseModel):
    sku: str
    note: str | None = None


class Nested(BaseModel):
    lines: list[Line]
    currency: str = "EUR"


def test_strict_schema_closes_every_object_and_requires_every_field() -> None:
    schema = strict_json_schema(Nested.model_json_schema())
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["lines", "currency"]
    line = schema["$defs"]["Line"]
    assert line["additionalProperties"] is False and line["required"] == ["sku", "note"]
    assert "default" not in json.dumps(schema)
    # The input is left untouched.
    assert "additionalProperties" not in Nested.model_json_schema()


def test_structured_request_shape() -> None:
    recorder = Recorder(
        _reply({"role": "assistant", "content": '{"invoice_number": "INV-9", "total": 3}'})
    )
    result = GroqProvider(FAKE_KEY, recorder.client()).complete_structured(
        "system rules", "invoice text", Invoice, "openai/gpt-oss-20b"
    )

    request = recorder.requests[0]
    assert str(request.url) == GROQ_CHAT_URL
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in request.content.decode() and FAKE_KEY not in str(request.url)
    body = recorder.body()
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["messages"] == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "invoice text"},
    ]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "Invoice"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert "tools" not in body

    assert result.parsed == Invoice(invoice_number="INV-9", total=3)
    assert result.usage == Usage(90, 15) and result.provider == "groq"


def test_structured_schema_mismatch_is_schema_error() -> None:
    recorder = Recorder(_reply({"role": "assistant", "content": '{"total": "lots"}'}))
    with pytest.raises(SchemaError) as info:
        GroqProvider(FAKE_KEY, recorder.client()).complete_structured("s", "u", Invoice, "m")
    assert info.value.usage == Usage(90, 15)


def test_structured_null_content_is_schema_error() -> None:
    recorder = Recorder(_reply({"role": "assistant", "content": None}))
    with pytest.raises(SchemaError):
        GroqProvider(FAKE_KEY, recorder.client()).complete_structured("s", "u", Invoice, "m")


def test_http_errors_are_classified() -> None:
    rpd = {
        "error": {
            "message": "Rate limit reached on requests per day (RPD). Please try again in 5m.",
            "code": "rate_limit_exceeded",
        }
    }
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "4"}, json={"error": {"message": "RPM"}}),
        httpx.Response(429, headers={"retry-after": "300"}, json=rpd),
        httpx.Response(401, json={"error": {"message": "Invalid API Key"}}),
    )
    provider = GroqProvider(FAKE_KEY, recorder.client())
    with pytest.raises(RateLimited):
        provider.complete_structured("s", "u", Invoice, "m")
    with pytest.raises(QuotaExhausted) as info:
        provider.complete_structured("s", "u", Invoice, "m")
    assert info.value.reset_in_s == 300.0
    with pytest.raises(AuthError):
        provider.complete_structured("s", "u", Invoice, "m")


def test_chat_step_sends_openai_tools_and_parses_tool_calls() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "reasoning": "internal",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_vendor", "arguments": '{"vendor_id": "V1"}'},
            }
        ],
    }
    recorder = Recorder(_reply(message))
    turn = GroqProvider(FAKE_KEY, recorder.client()).chat_step("s", "u", [], [TOOL], "m")

    body = recorder.body()
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_vendor",
                "description": "Look up a vendor by id",
                "parameters": TOOL.parameters,
            },
        }
    ]
    assert body["tool_choice"] == "auto"
    assert "response_format" not in body
    assert turn.text == ""
    assert turn.tool_calls == [ToolCall("call_1", "get_vendor", {"vendor_id": "V1"})]
    assert turn.usage == Usage(90, 15)


def test_unparsable_tool_arguments_become_an_empty_object() -> None:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "get_vendor", "arguments": "{"}}
        ],
    }
    turn = GroqProvider(FAKE_KEY, Recorder(_reply(message)).client()).chat_step(
        "s", "u", [], [TOOL], "m"
    )
    assert turn.tool_calls[0].arguments == {}


def test_history_replays_assistant_tool_calls_and_tool_messages() -> None:
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_vendor", "arguments": '{"vendor_id": "V1"}'},
        }
    ]
    recorder = Recorder(
        _reply({"role": "assistant", "content": None, "reasoning": "r", "tool_calls": tool_calls}),
        _reply({"role": "assistant", "content": "All good."}),
    )
    provider = GroqProvider(FAKE_KEY, recorder.client())
    first = provider.chat_step("s", "u", [], [TOOL], "m")
    results = ToolResultsTurn([ToolCallRecord(first.tool_calls[0], output={"name": "Acme"})])
    final = provider.chat_step("s", "u", [first, results], [TOOL], "m")

    messages = recorder.body()["messages"]
    # Only role/content/tool_calls go back: the reasoning field is not replayed.
    assert messages[2] == {"role": "assistant", "content": None, "tool_calls": tool_calls}
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "get_vendor",
        "content": '{"name": "Acme"}',
    }
    assert final.text == "All good." and final.tool_calls == []
