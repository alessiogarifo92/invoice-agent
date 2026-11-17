"""The policy gate (phase 3, "Agent"): `final_action = max(rule_floor, agent_action)`.

The agent can raise the severity of a proposal, never lower it: an IBAN change or an
injection stays at least `clarify`, a confirmed duplicate stays `block`, whatever the agent
says. An action outside the vocabulary is refused rather than guessed (fail closed).

A proposal is only as good as its evidence: when it cites an `evidence_ref` that this run's
tools did not return, the agent's action is discarded and counts as `clarify` (the floor
still holds, so a `block` floor stays `block`).
"""

from collections.abc import Collection, Iterable

from app.rules.flags import ACTION_ORDER, Action, max_action


def _checked(action: str, what: str) -> Action:
    if action not in ACTION_ORDER:
        raise ValueError(f"unknown action {action!r} for {what}")
    return action  # narrowed by the membership test


def forged_refs(cited: Iterable[str], returned: Collection[str]) -> tuple[str, ...]:
    """The cited refs that no tool of this run returned, each once, in citation order."""
    return tuple(dict.fromkeys(ref for ref in cited if ref not in returned))


def policy_gate(
    rule_floor: Action,
    agent_action: Action | None,
    evidence_refs: Iterable[str] = (),
    returned_refs: Collection[str] = (),
) -> Action:
    """The final action; `agent_action` None means the agent did not run or did not decide.

    `evidence_refs` are the refs the agent cited, `returned_refs` the ones this run's tools
    returned: a single unknown ref turns the agent's action into `clarify`.
    """
    floor = _checked(rule_floor, "the rule floor")
    if agent_action is None:
        return floor
    action = _checked(agent_action, "the agent")
    if forged_refs(evidence_refs, returned_refs):
        action = "clarify"
    return max_action(floor, action)
