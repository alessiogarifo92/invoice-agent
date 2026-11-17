"""The policy gate and the T7 agent stub: the agent can raise the rule floor, never lower it."""

import itertools
import uuid

import pytest

from app.agent import AgentDecision, AgentRequest, policy_gate, triage
from app.rules.flags import ACTION_ORDER, Action

ACTIONS: tuple[Action, ...] = ("approve", "clarify", "block")


@pytest.mark.parametrize(("floor", "agent"), list(itertools.product(ACTIONS, ACTIONS)))
def test_the_final_action_is_the_stricter_of_floor_and_agent(floor: Action, agent: Action) -> None:
    final = policy_gate(floor, agent)
    assert ACTION_ORDER[final] == max(ACTION_ORDER[floor], ACTION_ORDER[agent])
    assert ACTION_ORDER[final] >= ACTION_ORDER[floor]


@pytest.mark.parametrize("floor", ACTIONS)
def test_an_agent_that_did_not_run_leaves_the_floor(floor: Action) -> None:
    assert policy_gate(floor, None) == floor


@pytest.mark.parametrize("floor", ["clarify", "block"])
def test_an_agent_saying_approve_never_lowers_a_flagged_invoice(floor: Action) -> None:
    assert policy_gate(floor, "approve") == floor


def test_the_agent_can_raise_a_clean_invoice() -> None:
    assert policy_gate("approve", "block") == "block"


def test_an_unknown_action_is_refused() -> None:
    # Fail closed: a value outside the vocabulary must never pass as an action.
    with pytest.raises(ValueError, match="unknown action"):
        policy_gate("approve", "pay")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown action"):
        policy_gate("maybe", None)  # type: ignore[arg-type]


def test_the_t7_stub_never_decides() -> None:
    request = AgentRequest(
        invoice_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        proposal_id=uuid.uuid4(),
        rule_floor="clarify",
        flag_codes=("iban_not_current",),
    )
    assert triage(request) is None


def test_a_decision_carries_its_evidence() -> None:
    decision = AgentDecision(action="clarify", rationale="IBAN changed.", evidence_refs=("f1",))
    assert decision.model is None and decision.evidence_refs == ("f1",)
