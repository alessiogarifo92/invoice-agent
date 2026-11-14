"""Fakes shared by the LLM tests: a recording HTTP transport and a scripted provider."""

import json
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from app.llm.errors import LLMError
from app.llm.provider import ToolLoopMixin, parse_structured
from app.llm.types import AssistantTurn, StructuredResult, T, ToolSpec, Turn, Usage

FAKE_KEY = "test-llm-key-not-real-0123456789"


class Invoice(BaseModel):
    invoice_number: str
    total: float


class Recorder:
    """httpx.MockTransport that answers with canned responses and keeps every request."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def body(self, index: int = -1) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


class FakeProvider(ToolLoopMixin):
    """A provider whose every request is scripted: a result, a turn, or an LLMError."""

    operation_name = "chat"

    def __init__(self, name: str, *script: Any):
        self.name = name
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def _next(self, **call: Any) -> Any:
        self.calls.append(call)
        item = self.script.pop(0)
        if isinstance(item, LLMError):
            raise item
        return item

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]:
        text = self._next(op="structured", model=model, system=system, user=user)
        usage = Usage(100, 20)
        return StructuredResult(
            parsed=parse_structured(self.name, schema, text, usage),
            raw_text=text,
            usage=usage,
            provider=self.name,
            model=model,
            latency_ms=5,
        )

    def chat_step(
        self,
        system: str,
        user: str,
        history: Sequence[Turn],
        tools: Sequence[ToolSpec],
        model: str,
    ) -> AssistantTurn:
        turn: AssistantTurn = self._next(op="chat", model=model, history=list(history))
        return turn
