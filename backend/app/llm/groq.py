"""Groq adapter over its OpenAI-compatible chat completions endpoint (no SDK).

Checked against console.groq.com/docs (2026-11-14): `response_format` json_schema with
`strict: true` is supported on openai/gpt-oss-20b and openai/gpt-oss-120b (the defaults);
tools use the OpenAI `tools` / `tool_calls` format with `function.arguments` as a JSON string,
and results go back as `role: tool` messages carrying `tool_call_id`.
"""

import json
import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.llm.errors import classify_groq
from app.llm.http import DEFAULT_TIMEOUT_S, post_json
from app.llm.provider import ToolLoopMixin, parse_structured, tool_output_json
from app.llm.schema import strict_json_schema
from app.llm.types import AssistantTurn, StructuredResult, T, ToolCall, ToolSpec, Turn, Usage

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _usage(body: dict[str, Any]) -> Usage:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    return Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


def _message(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def _arguments(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class GroqProvider(ToolLoopMixin):
    name = "groq"
    operation_name = "chat"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        url: str = GROQ_CHAT_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._client = client
        self._url = url
        self._timeout_s = timeout_s

    def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        return post_json(
            self._client,
            self.name,
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            body=body,
            classify=classify_groq,
            timeout_s=self._timeout_s,
        )

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(schema.model_json_schema()),
                },
            },
        }
        start = time.monotonic()
        reply = self._chat(body)
        latency_ms = int((time.monotonic() - start) * 1000)
        usage = _usage(reply)
        text = str(_message(reply).get("content") or "")
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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for turn in history:
            if isinstance(turn, AssistantTurn):
                messages.append(turn.raw)
            else:
                messages.extend(
                    {
                        "role": "tool",
                        "tool_call_id": record.call.id,
                        "name": record.call.name,
                        "content": tool_output_json(record.output),
                    }
                    for record in turn.results
                )
        body = {
            "model": model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ],
            "tool_choice": "auto",
        }
        reply = self._chat(body)
        message = _message(reply)
        raw_calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
        calls = []
        for call in raw_calls:
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            calls.append(
                ToolCall(
                    id=str(call.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=_arguments(function.get("arguments")),
                )
            )
        # Replayed as-is on the next step, minus provider extras such as `reasoning`.
        raw: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        return AssistantTurn(
            text=str(message.get("content") or ""), tool_calls=calls, usage=_usage(reply), raw=raw
        )
