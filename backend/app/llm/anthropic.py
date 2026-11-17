"""Anthropic adapter (off by default) over the official SDK, Messages API.

SDK retries are off (`max_retries=0`, phase 3 "Spend control"): the gateway's classifier
decides. Structured output uses `output_config.format` (json_schema); tools are strict and
`tool_choice` stays `auto`, since forced tool choice returns a 400 on current models.
"""

import time
from collections.abc import Sequence
from typing import Any, cast

import anthropic
from anthropic.types import Message, TextBlock, ToolUseBlock

from app.llm.errors import classify_anthropic, network_error
from app.llm.http import DEFAULT_TIMEOUT_S
from app.llm.provider import ToolLoopMixin, parse_structured, tool_output_json
from app.llm.schema import strict_json_schema
from app.llm.types import AssistantTurn, StructuredResult, T, ToolCall, ToolSpec, Turn, Usage

# Non-streaming ceiling; billing is per generated token, so a roomy cap costs nothing extra.
DEFAULT_MAX_TOKENS = 16000


def anthropic_client(api_key: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)


def _usage(message: Message) -> Usage:
    return Usage(message.usage.input_tokens, message.usage.output_tokens)


def _text(message: Message) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))


class AnthropicProvider(ToolLoopMixin):
    name = "anthropic"
    operation_name = "chat"

    def __init__(self, client: anthropic.Anthropic, *, max_tokens: int = DEFAULT_MAX_TOKENS):
        self._client = client
        self._max_tokens = max_tokens

    def _create(self, **kwargs: Any) -> Message:
        try:
            return cast(Message, self._client.messages.create(**kwargs))
        except anthropic.APIStatusError as exc:
            raise classify_anthropic(exc.status_code, exc.response.headers, exc.body) from None
        except anthropic.APIConnectionError as exc:
            raise network_error(self.name, exc) from None

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]:
        start = time.monotonic()
        message = self._create(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": strict_json_schema(schema.model_json_schema()),
                }
            },
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        usage = _usage(message)
        text = _text(message)
        return StructuredResult(
            parsed=parse_structured(self.name, schema, text, usage),
            raw_text=text,
            usage=usage,
            provider=self.name,
            model=model,
            latency_ms=latency_ms,
        )

    def chat_step(
        self,
        system: str,
        user: str,
        history: Sequence[Turn],
        tools: Sequence[ToolSpec],
        model: str,
    ) -> AssistantTurn:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for turn in history:
            if isinstance(turn, AssistantTurn):
                # The SDK's own blocks, unchanged: thinking blocks must be passed back as-is.
                messages.append({"role": "assistant", "content": turn.raw})
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": record.call.id,
                                "content": tool_output_json(record.output),
                                "is_error": record.is_error,
                            }
                            for record in turn.results
                        ],
                    }
                )
        message = self._create(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": (
                        strict_json_schema(tool.parameters) if tool.strict else tool.parameters
                    ),
                    "strict": tool.strict,
                }
                for tool in tools
            ],
            tool_choice={"type": "auto"},
        )
        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in message.content
            if isinstance(block, ToolUseBlock)
        ]
        return AssistantTurn(
            text=_text(message), tool_calls=calls, usage=_usage(message), raw=message.content
        )
