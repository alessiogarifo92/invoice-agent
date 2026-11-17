"""Anthropic adapter (official SDK, injected client): request shape and parsing."""

from typing import Any, cast

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from app.llm.anthropic import AnthropicProvider, anthropic_client
from app.llm.errors import QuotaExhausted, RateLimited, SchemaError, TransientError
from app.llm.types import ToolCall, ToolCallRecord, ToolResultsTurn, ToolSpec, Usage
from tests.llm_support import FAKE_KEY, Invoice

TOOL = ToolSpec(
    "get_vendor",
    "Look up a vendor by id",
    {"type": "object", "properties": {"vendor_id": {"type": "string"}}},
)


def _message(*content: dict[str, Any], stop_reason: str = "end_turn") -> Message:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 70, "output_tokens": 9},
        }
    )


class FakeMessages:
    def __init__(self, *replies: Message | Exception):
        self.replies = list(replies)
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.kwargs.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, *replies: Message | Exception):
        self.messages = FakeMessages(*replies)


def _provider(fake: FakeClient) -> AnthropicProvider:
    return AnthropicProvider(cast(anthropic.Anthropic, fake))


def _status_error(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, headers=headers or {}, json=body, request=request)
    return anthropic.APIStatusError("error", response=response, body=body)


def test_factory_disables_sdk_retries() -> None:
    client = anthropic_client(FAKE_KEY)
    assert client.max_retries == 0


def test_structured_request_shape_and_parsing() -> None:
    fake = FakeClient(_message({"type": "text", "text": '{"invoice_number": "X", "total": 2}'}))
    result = _provider(fake).complete_structured("rules", "invoice", Invoice, "claude-test")

    kwargs = fake.messages.kwargs[0]
    assert kwargs["model"] == "claude-test"
    assert kwargs["system"] == "rules"
    assert kwargs["messages"] == [{"role": "user", "content": "invoice"}]
    schema_format = kwargs["output_config"]["format"]
    assert schema_format["type"] == "json_schema"
    assert schema_format["schema"]["additionalProperties"] is False
    assert kwargs["max_tokens"] > 0
    assert "tools" not in kwargs
    assert result.parsed == Invoice(invoice_number="X", total=2)
    assert result.usage == Usage(70, 9) and result.provider == "anthropic"


def test_refusal_without_json_is_schema_error() -> None:
    fake = FakeClient(_message(stop_reason="refusal"))
    with pytest.raises(SchemaError) as info:
        _provider(fake).complete_structured("s", "u", Invoice, "m")
    assert info.value.usage == Usage(70, 9)


def test_sdk_errors_are_classified() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake = FakeClient(
        _status_error(
            400,
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "You have reached your specified API usage limits.",
                },
            },
        ),
        _status_error(
            429,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            {"retry-after": "3"},
        ),
        _status_error(529, {"type": "error", "error": {"type": "overloaded_error"}}),
        anthropic.APIConnectionError(request=request),
    )
    provider = _provider(fake)
    with pytest.raises(QuotaExhausted):
        provider.complete_structured("s", "u", Invoice, "m")
    with pytest.raises(RateLimited):
        provider.complete_structured("s", "u", Invoice, "m")
    with pytest.raises(TransientError):
        provider.complete_structured("s", "u", Invoice, "m")
    with pytest.raises(TransientError):
        provider.complete_structured("s", "u", Invoice, "m")


def test_chat_step_sends_strict_tools_and_parses_tool_use() -> None:
    fake = FakeClient(
        _message(
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Looking up."},
            {"type": "tool_use", "id": "tu_1", "name": "get_vendor", "input": {"vendor_id": "V"}},
            stop_reason="tool_use",
        )
    )
    turn = _provider(fake).chat_step("s", "u", [], [TOOL], "m")

    kwargs = fake.messages.kwargs[0]
    assert kwargs["tool_choice"] == {"type": "auto"}
    (tool,) = kwargs["tools"]
    assert tool["name"] == "get_vendor" and tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert "output_config" not in kwargs
    assert turn.text == "Looking up."
    assert turn.tool_calls == [ToolCall("tu_1", "get_vendor", {"vendor_id": "V"})]
    assert turn.usage == Usage(70, 9)


def test_history_replays_content_blocks_and_tool_results() -> None:
    first_reply = _message(
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "get_vendor", "input": {}},
        stop_reason="tool_use",
    )
    fake = FakeClient(first_reply, _message({"type": "text", "text": "ok"}))
    provider = _provider(fake)
    first = provider.chat_step("s", "u", [], [TOOL], "m")
    results = ToolResultsTurn(
        [ToolCallRecord(first.tool_calls[0], output={"error": "unknown_tool"}, is_error=True)]
    )
    provider.chat_step("s", "u", [first, results], [TOOL], "m")

    messages = fake.messages.kwargs[1]["messages"]
    # Thinking blocks go back unchanged, as the SDK returned them.
    assert messages[1] == {"role": "assistant", "content": first_reply.content}
    assert messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": '{"error": "unknown_tool"}',
                "is_error": True,
            }
        ],
    }
