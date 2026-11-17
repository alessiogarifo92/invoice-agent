"""Gemini adapter over the REST generateContent endpoint (no SDK).

Field names checked against the Gemini API reference (ai.google.dev/api/generate-content):
structured output via generationConfig.responseMimeType + responseJsonSchema, tools via
functionDeclarations[].parametersJsonSchema, replies via functionCall / functionResponse parts.
"""

import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.llm.errors import classify_gemini
from app.llm.http import DEFAULT_TIMEOUT_S, post_json
from app.llm.provider import ToolLoopMixin, parse_structured
from app.llm.types import (
    AssistantTurn,
    StructuredResult,
    T,
    ToolCall,
    ToolCallRecord,
    ToolSpec,
    Turn,
    Usage,
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Marks an id we made up because the model sent none: it is never sent back to Gemini.
_LOCAL_ID_PREFIX = "local-"


def _usage(body: dict[str, Any]) -> Usage:
    usage = body.get("usageMetadata")
    if not isinstance(usage, dict):
        return Usage()
    output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    return Usage(int(usage.get("promptTokenCount") or 0), output)


def _parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return []
    content = candidates[0].get("content")
    parts = content.get("parts", []) if isinstance(content, dict) else []
    return [p for p in parts if isinstance(p, dict)]


def _text(parts: list[dict[str, Any]]) -> str:
    return "".join(str(p.get("text", "")) for p in parts if "text" in p and not p.get("thought"))


class GeminiProvider(ToolLoopMixin):
    name = "gemini"
    operation_name = "generate_content"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        base_url: str = GEMINI_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _generate(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        return post_json(
            self._client,
            self.name,
            f"{self._base_url}/models/{model}:generateContent",
            headers={"x-goog-api-key": self._api_key},
            body=body,
            classify=classify_gemini,
            timeout_s=self._timeout_s,
        )

    @staticmethod
    def _base_body(system: str, user: str) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]:
        body = self._base_body(system, user)
        body["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema.model_json_schema(),
        }
        start = time.monotonic()
        reply = self._generate(model, body)
        latency_ms = int((time.monotonic() - start) * 1000)
        usage = _usage(reply)
        text = _text(_parts(reply))
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
        body = self._base_body(system, user)
        for turn in history:
            if isinstance(turn, AssistantTurn):
                # Verbatim: Gemini rejects a functionCall whose thoughtSignature was dropped.
                body["contents"].append({"role": "model", "parts": turn.raw})
            else:
                body["contents"].append(
                    {"role": "user", "parts": [_function_response(r) for r in turn.results]}
                )
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parametersJsonSchema": tool.parameters,
                    }
                    for tool in tools
                ]
            }
        ]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        reply = self._generate(model, body)
        parts = _parts(reply)
        calls = []
        for index, part in enumerate(parts):
            call = part.get("functionCall")
            if not isinstance(call, dict):
                continue
            args = call.get("args")
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"{_LOCAL_ID_PREFIX}{index}"),
                    name=str(call.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
        return AssistantTurn(text=_text(parts), tool_calls=calls, usage=_usage(reply), raw=parts)


def _function_response(record: ToolCallRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": record.call.name}
    if not record.call.id.startswith(_LOCAL_ID_PREFIX):
        payload["id"] = record.call.id
    # functionResponse.response must be an object: wrap the output under result / error.
    payload["response"] = {"error" if record.is_error else "result": record.output}
    return {"functionResponse": payload}
