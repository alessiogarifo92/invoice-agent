"""The Provider interface and the tool loop, written once for every provider.

Each adapter implements two single-request primitives, `complete_structured` and
`chat_step`. The loop (`drive_tool_loop`) is provider-independent; the gateway runs it with
an instrumented step (retries, ledger row and span per request), and `ToolLoopMixin` gives
each adapter a plain, uninstrumented `run_tools` on top of the same driver.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from app.llm.errors import LLMError, QuotaExhausted, QuotaUnavailable, SchemaError
from app.llm.types import (
    AssistantTurn,
    Limits,
    StructuredResult,
    T,
    ToolCall,
    ToolCallRecord,
    ToolHandler,
    ToolLoopResult,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    Usage,
)

# One model request inside the loop: (system, user, history) -> the model's reply.
StepFn = Callable[[str, str, Sequence[Turn]], AssistantTurn]


class Provider(Protocol):
    name: str
    # OTel GenAI operation name for this provider's endpoint.
    operation_name: str

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]: ...

    def chat_step(
        self,
        system: str,
        user: str,
        history: Sequence[Turn],
        tools: Sequence[ToolSpec],
        model: str,
    ) -> AssistantTurn: ...

    def run_tools(
        self,
        system: str,
        user: str,
        tools: Sequence[ToolSpec],
        tool_handlers: Mapping[str, ToolHandler],
        terminal_tool: str,
        limits: Limits,
        model: str,
    ) -> ToolLoopResult: ...


class ToolLoopMixin:
    """`run_tools` for an adapter that implements `chat_step`."""

    name: str

    def chat_step(
        self,
        system: str,
        user: str,
        history: Sequence[Turn],
        tools: Sequence[ToolSpec],
        model: str,
    ) -> AssistantTurn:
        raise NotImplementedError

    def run_tools(
        self,
        system: str,
        user: str,
        tools: Sequence[ToolSpec],
        tool_handlers: Mapping[str, ToolHandler],
        terminal_tool: str,
        limits: Limits,
        model: str,
    ) -> ToolLoopResult:
        def step(system: str, user: str, history: Sequence[Turn]) -> AssistantTurn:
            return self.chat_step(system, user, history, tools, model)

        return drive_tool_loop(
            step,
            system=system,
            user=user,
            tools=tools,
            tool_handlers=tool_handlers,
            terminal_tool=terminal_tool,
            limits=limits,
            clock=time.monotonic,
            provider=self.name,
            model=model,
        )


def parse_structured(provider: str, schema: type[T], text: str, usage: Usage) -> T:
    """Validate the model's JSON against the caller's schema, or raise SchemaError."""
    try:
        return schema.model_validate_json(text)
    except ValidationError:
        # Covers invalid JSON too. The validation detail can quote invoice text: not kept.
        raise SchemaError(provider, usage=usage) from None


def tool_output_json(output: Any) -> str:
    return json.dumps(output, default=str)


def drive_tool_loop(
    step: StepFn,
    *,
    system: str,
    user: str,
    tools: Sequence[ToolSpec],
    tool_handlers: Mapping[str, ToolHandler],
    terminal_tool: str,
    limits: Limits,
    clock: Callable[[], float],
    provider: str,
    model: str,
) -> ToolLoopResult:
    """Run the model until it calls `terminal_tool`, a limit is hit, or it stops calling tools.

    Phase 3, "Agent": at most `limits.max_tool_calls` tool calls (the terminal one included)
    and a timeout; reaching either, or ending without the terminal tool, is not a proposal.
    Quota errors propagate, so the gateway can fall through to the fallback provider; any
    other classified error ends the loop with stop_reason "error".
    """
    if terminal_tool not in {tool.name for tool in tools}:
        raise ValueError(f"terminal tool {terminal_tool!r} is not among the offered tools")
    deadline = clock() + limits.timeout_s
    history: list[Turn] = []
    records: list[ToolCallRecord] = []
    usage = Usage()

    def result(stop_reason: Any, **kw: Any) -> ToolLoopResult:
        return ToolLoopResult(
            stop_reason=stop_reason,
            proposal=kw.pop("proposal", None),
            tool_calls=list(records),
            usage=usage,
            provider=provider,
            model=model,
            **kw,
        )

    while True:
        if clock() >= deadline:
            return result("timeout")
        try:
            turn = step(system, user, history)
        except (QuotaExhausted, QuotaUnavailable):
            raise
        except LLMError as exc:
            return result("error", error_class=type(exc).__name__)
        usage = usage + turn.usage
        if not turn.tool_calls:
            return result("no_proposal", final_text=turn.text)
        history.append(turn)
        results: list[ToolCallRecord] = []
        for call in turn.tool_calls:
            if len(records) >= limits.max_tool_calls:
                return result("max_tool_calls")
            if call.name == terminal_tool:
                records.append(ToolCallRecord(call, output=None))
                return result("completed", proposal=call.arguments, final_text=turn.text)
            if clock() >= deadline:
                return result("timeout")
            record = _execute(call, tool_handlers)
            records.append(record)
            results.append(record)
        history.append(ToolResultsTurn(results))


def _execute(call: ToolCall, handlers: Mapping[str, ToolHandler]) -> ToolCallRecord:
    handler = handlers.get(call.name)
    if handler is None:
        # Sent back to the model as an error result; still counts toward the limit.
        return ToolCallRecord(call, output={"error": "unknown_tool"}, is_error=True)
    try:
        return ToolCallRecord(call, output=handler(call.arguments))
    except Exception as exc:  # noqa: BLE001 - any handler failure becomes an error result
        return ToolCallRecord(
            call, output={"error": "tool_failed", "type": type(exc).__name__}, is_error=True
        )
