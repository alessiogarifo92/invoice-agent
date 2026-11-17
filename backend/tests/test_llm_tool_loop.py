"""The provider-independent tool loop, driven by a scripted model."""

from collections.abc import Sequence
from typing import Any

import pytest

from app.llm.errors import InvalidRequest, QuotaExhausted
from app.llm.provider import InvalidToolArguments, drive_tool_loop
from app.llm.types import (
    AssistantTurn,
    Limits,
    ToolCall,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    Usage,
)

LOOKUP = ToolSpec("get_vendor", "Look up a vendor", {"type": "object", "properties": {}})
PROPOSE = ToolSpec("propose_payment", "Final proposal", {"type": "object", "properties": {}})


def _call(name: str, n: int = 0, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"c{n}", name=name, arguments=arguments)


def _malformed(name: str, n: int = 0) -> ToolCall:
    return ToolCall(id=f"c{n}", name=name, arguments={}, malformed=True)


def _strict(args: dict[str, Any]) -> Any:
    if args:
        raise InvalidToolArguments([{"loc": next(iter(args)), "type": "extra_forbidden"}])
    return {"name": "Acme"}


def _turn(*calls: ToolCall, text: str = "") -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=list(calls), usage=Usage(10, 2))


class ScriptedModel:
    """Returns the scripted turns in order and records the history it was shown."""

    def __init__(self, *turns: AssistantTurn | Exception):
        self.turns = list(turns)
        self.histories: list[list[Turn]] = []

    def __call__(self, system: str, user: str, history: Sequence[Turn]) -> AssistantTurn:
        self.histories.append(list(history))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _recording(sink: list[Any], output: Any) -> Any:
    def handler(args: dict[str, Any]) -> Any:
        sink.append(args)
        return output

    return handler


def _run(model: ScriptedModel, handlers: dict[str, Any], **kw: Any) -> Any:
    return drive_tool_loop(
        model,
        system="sys",
        user="usr",
        tools=[LOOKUP, PROPOSE],
        tool_handlers=handlers,
        terminal_tool="propose_payment",
        limits=kw.pop("limits", Limits()),
        clock=kw.pop("clock", FakeClock()),
        provider="fake",
        model="m",
    )


def test_terminal_tool_completes_with_its_arguments_as_proposal() -> None:
    seen: list[dict[str, Any]] = []
    model = ScriptedModel(
        _turn(_call("get_vendor", 1, vendor_id="V1")),
        _turn(_call("propose_payment", 2, action="approve")),
    )
    result = _run(model, {"get_vendor": _recording(seen, {"name": "Acme"})})

    assert result.stop_reason == "completed"
    assert result.proposal == {"action": "approve"}
    assert seen == [{"vendor_id": "V1"}]
    assert [r.call.name for r in result.tool_calls] == ["get_vendor", "propose_payment"]
    assert result.tool_calls[0].output == {"name": "Acme"}
    assert result.usage == Usage(20, 4)
    # The second step saw the first reply and the tool result.
    history = model.histories[1]
    assert isinstance(history[0], AssistantTurn) and isinstance(history[1], ToolResultsTurn)
    assert history[1].results[0].output == {"name": "Acme"}


def test_ninth_tool_call_is_never_executed() -> None:
    executed: list[Any] = []
    turns = [_turn(_call("get_vendor", n)) for n in range(9)]
    model = ScriptedModel(*turns)
    result = _run(model, {"get_vendor": _recording(executed, "ok")})

    assert result.stop_reason == "max_tool_calls"
    assert result.proposal is None
    assert len(executed) == 8 and len(result.tool_calls) == 8


def test_terminal_call_beyond_the_limit_does_not_complete() -> None:
    turns = [_turn(_call("get_vendor", n)) for n in range(8)]
    model = ScriptedModel(*turns, _turn(_call("propose_payment", 9, action="approve")))
    result = _run(model, {"get_vendor": lambda args: "ok"})
    assert result.stop_reason == "max_tool_calls" and result.proposal is None


def test_parallel_calls_count_one_by_one_against_the_limit() -> None:
    executed: list[Any] = []
    model = ScriptedModel(_turn(*[_call("get_vendor", n) for n in range(3)]))
    result = _run(model, {"get_vendor": _recording(executed, "ok")}, limits=Limits(2, 60))
    assert result.stop_reason == "max_tool_calls" and len(executed) == 2


def test_timeout_stops_the_loop_before_the_next_step() -> None:
    clock = FakeClock()

    def slow_lookup(args: dict[str, Any]) -> str:
        clock.now += 50
        return "ok"

    model = ScriptedModel(*[_turn(_call("get_vendor", n)) for n in range(5)])
    result = _run(model, {"get_vendor": slow_lookup}, clock=clock, limits=Limits(8, 120))

    assert result.stop_reason == "timeout"
    assert len(result.tool_calls) == 3  # 0 -> 50 -> 100 -> 150: the 4th step never starts
    assert len(model.histories) == 3


def test_text_answer_without_tool_call_is_no_proposal() -> None:
    model = ScriptedModel(_turn(text="I think this is fine."))
    result = _run(model, {})
    assert result.stop_reason == "no_proposal"
    assert result.final_text == "I think this is fine." and result.proposal is None


def test_unknown_tool_is_an_error_result_sent_back_and_counts() -> None:
    model = ScriptedModel(
        _turn(_call("pay_now", 1)), _turn(_call("propose_payment", 2, action="clarify"))
    )
    result = _run(model, {}, limits=Limits(2, 60))

    assert result.stop_reason == "completed"
    first = result.tool_calls[0]
    assert first.is_error and first.output == {"error": "unknown_tool"}
    sent_back = model.histories[1][1]
    assert isinstance(sent_back, ToolResultsTurn) and sent_back.results[0].is_error


def test_failing_handler_is_an_error_result_without_the_exception_message() -> None:
    def broken(args: dict[str, Any]) -> str:
        raise RuntimeError("secret invoice detail")

    model = ScriptedModel(_turn(_call("get_vendor", 1)), _turn(text="done"))
    result = _run(model, {"get_vendor": broken})
    assert result.tool_calls[0].is_error
    assert result.tool_calls[0].output == {"error": "tool_failed", "type": "RuntimeError"}
    assert result.stop_reason == "no_proposal"


def test_classified_provider_error_mid_loop_ends_with_error() -> None:
    model = ScriptedModel(_turn(_call("get_vendor", 1)), InvalidRequest("fake", status=400))
    result = _run(model, {"get_vendor": lambda args: "ok"})
    assert result.stop_reason == "error"
    assert result.error_class == "InvalidRequest" and len(result.tool_calls) == 1


def test_quota_errors_propagate_so_the_gateway_can_fall_back() -> None:
    model = ScriptedModel(QuotaExhausted("fake", status=429))
    with pytest.raises(QuotaExhausted):
        _run(model, {})


def test_terminal_tool_must_be_offered() -> None:
    with pytest.raises(ValueError):
        drive_tool_loop(
            ScriptedModel(),
            system="s",
            user="u",
            tools=[LOOKUP],
            tool_handlers={},
            terminal_tool="propose_payment",
            limits=Limits(),
            clock=FakeClock(),
            provider="fake",
            model="m",
        )


# --- invalid arguments: one retry, then the loop stops ------------------------------------


def test_invalid_arguments_are_sent_back_once_for_a_retry() -> None:
    model = ScriptedModel(
        _turn(_call("get_vendor", 1, vendor_id="V-OTHER")),
        _turn(_call("get_vendor", 2)),
        _turn(_call("propose_payment", 3, action="clarify")),
    )
    result = _run(model, {"get_vendor": _strict})

    assert result.stop_reason == "completed"
    first, second, _ = result.tool_calls
    assert first.is_error
    assert first.output == {
        "error": "invalid_arguments",
        "problems": [{"loc": "vendor_id", "type": "extra_forbidden"}],
    }
    assert not second.is_error and second.output == {"name": "Acme"}
    sent_back = model.histories[1][1]
    assert isinstance(sent_back, ToolResultsTurn) and sent_back.results[0].is_error


def test_a_second_invalid_call_ends_the_loop_without_a_proposal() -> None:
    model = ScriptedModel(
        _turn(_call("get_vendor", 1, vendor_id="V-OTHER")),
        _turn(_call("get_vendor", 2, invoice_id="INV-OTHER")),
        _turn(_call("propose_payment", 3, action="approve")),
    )
    result = _run(model, {"get_vendor": _strict})

    assert result.stop_reason == "invalid_arguments" and result.proposal is None
    assert len(result.tool_calls) == 2 and len(model.histories) == 2


def test_the_invalid_argument_budget_is_a_limit() -> None:
    model = ScriptedModel(_turn(_call("get_vendor", 1, x=1)))
    result = _run(model, {"get_vendor": _strict}, limits=Limits(max_invalid_arguments=0))
    assert result.stop_reason == "invalid_arguments"


def test_malformed_arguments_never_reach_the_handler() -> None:
    seen: list[dict[str, Any]] = []
    model = ScriptedModel(
        _turn(_malformed("get_vendor", 1)), _turn(_call("propose_payment", 2, action="clarify"))
    )
    result = _run(model, {"get_vendor": _recording(seen, "ok")})

    assert seen == []
    assert result.tool_calls[0].is_error
    assert result.tool_calls[0].output == {"error": "invalid_arguments", "problems": []}
    assert result.stop_reason == "completed"


def test_a_terminal_handler_validates_the_proposal() -> None:
    def validate(args: dict[str, Any]) -> dict[str, Any]:
        if args.get("action") not in ("approve", "clarify", "block"):
            raise InvalidToolArguments([{"loc": "action", "type": "literal_error"}])
        return {"action": args["action"], "normalised": True}

    model = ScriptedModel(
        _turn(_call("propose_payment", 1, action="pay_now")),
        _turn(_call("propose_payment", 2, action="block")),
    )
    result = _run(model, {"propose_payment": validate})

    assert result.stop_reason == "completed"
    assert result.proposal == {"action": "block", "normalised": True}
    assert result.tool_calls[0].is_error and len(result.tool_calls) == 2


def test_a_malformed_terminal_call_is_not_a_proposal() -> None:
    model = ScriptedModel(_turn(_malformed("propose_payment", 1)), _turn(text="giving up"))
    result = _run(model, {})
    assert result.stop_reason == "no_proposal" and result.proposal is None
    assert result.tool_calls[0].is_error


def test_a_terminal_handler_that_fails_is_an_error_result() -> None:
    def broken(args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    model = ScriptedModel(_turn(_call("propose_payment", 1, action="approve")), _turn(text="ok"))
    result = _run(model, {"propose_payment": broken})
    assert result.stop_reason == "no_proposal"
    assert result.tool_calls[0].output == {"error": "tool_failed", "type": "RuntimeError"}
