"""Provider-neutral request and response types for the LLM gateway."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

StopReason = Literal["completed", "max_tool_calls", "timeout", "no_proposal", "error"]

# A tool handler gets the parsed arguments and returns a JSON-serialisable result.
ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True)
class StructuredResult[M: BaseModel]:
    """One structured completion, validated against the caller's Pydantic schema."""

    parsed: M
    raw_text: str
    usage: Usage
    provider: str
    model: str
    latency_ms: int


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `parameters` is a JSON schema describing an object."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRecord:
    """A tool call and what was sent back to the model for it."""

    call: ToolCall
    output: Any
    is_error: bool = False


@dataclass(frozen=True)
class AssistantTurn:
    """One model reply inside a tool loop.

    `raw` is the provider's own message, echoed back verbatim on the next step: Gemini's
    thought signatures and Anthropic's thinking blocks must be returned unchanged.
    """

    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None


@dataclass(frozen=True)
class ToolResultsTurn:
    """The results of the tool calls of the preceding assistant turn."""

    results: list[ToolCallRecord]


Turn = AssistantTurn | ToolResultsTurn


@dataclass(frozen=True)
class Limits:
    """Agent loop limits (phase 3, "Agent"): reaching either one ends the loop."""

    max_tool_calls: int = 8
    timeout_s: float = 120.0


@dataclass(frozen=True)
class ToolLoopResult:
    """The outcome of a tool loop. T8 maps every stop_reason except `completed` to clarify."""

    stop_reason: StopReason
    proposal: dict[str, Any] | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_text: str = ""
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    # Classified error code when stop_reason is "error" (never a provider response body).
    error_class: str | None = None
