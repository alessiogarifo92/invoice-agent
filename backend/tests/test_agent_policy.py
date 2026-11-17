"""The policy gate and the T7 agent stub: the agent can raise the rule floor, never lower it."""

import itertools
import uuid

import pytest

from app.agent import AgentDecision, AgentRequest, forged_refs, policy_gate, triage
from app.rules.checks import flag
from app.rules.flags import ACTION_ORDER, Action, rule_floor

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


# --- T8 acceptance: the gate over real rule floors, and the evidence check -------------------


def _floor(*codes: str) -> Action:
    return rule_floor([flag(code, f"{code} raised") for code in codes])


@pytest.mark.parametrize(
    "code", ["iban_not_current", "iban_in_text_not_current", "injection_suspected"]
)
@pytest.mark.parametrize("agent", ACTIONS)
def test_an_iban_change_or_an_injection_is_at_least_clarify(code: str, agent: Action) -> None:
    final = policy_gate(_floor(code), agent)
    assert ACTION_ORDER[final] >= ACTION_ORDER["clarify"]


@pytest.mark.parametrize("agent", ACTIONS)
def test_a_confirmed_duplicate_is_blocked_whatever_the_agent_says(agent: Action) -> None:
    assert policy_gate(_floor("duplicate_confirmed", "iban_not_current"), agent) == "block"


@pytest.mark.parametrize("floor", ACTIONS)
@pytest.mark.parametrize("agent", ACTIONS)
def test_the_agent_never_lowers_the_floor_even_with_valid_evidence(
    floor: Action, agent: Action
) -> None:
    final = policy_gate(floor, agent, ["ev-1"], ["ev-1", "ev-2"])
    assert ACTION_ORDER[final] == max(ACTION_ORDER[floor], ACTION_ORDER[agent])


@pytest.mark.parametrize(
    ("floor", "agent", "final"),
    [
        ("approve", "approve", "clarify"),
        ("clarify", "approve", "clarify"),
        ("approve", "block", "clarify"),  # a forged citation discards the whole proposal
        ("block", "approve", "block"),  # ... and the floor still holds
    ],
)
def test_a_forged_evidence_ref_forces_clarify(floor: Action, agent: Action, final: Action) -> None:
    assert policy_gate(floor, agent, ["ev-1", "ev-forged"], ["ev-1"]) == final


def test_citing_nothing_forges_nothing() -> None:
    assert policy_gate("approve", "approve", [], []) == "approve"
    assert forged_refs([], ["ev-1"]) == ()


def test_forged_refs_lists_each_unknown_ref_once_in_order() -> None:
    cited = ["ev-2", "INV-SEED-024", "ev-1", "INV-SEED-024", "ev-9"]
    assert forged_refs(cited, {"ev-1", "ev-2"}) == ("INV-SEED-024", "ev-9")
