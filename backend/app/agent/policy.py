"""The policy gate (phase 3, "Agent"): `final_action = max(rule_floor, agent_action)`.

The agent can raise the severity of a proposal, never lower it: an IBAN change or an
injection stays at least `clarify`, a confirmed duplicate stays `block`, whatever the agent
says. An action outside the vocabulary is refused rather than guessed (fail closed).
"""

from app.rules.flags import ACTION_ORDER, Action, max_action


def _checked(action: str, what: str) -> Action:
    if action not in ACTION_ORDER:
        raise ValueError(f"unknown action {action!r} for {what}")
    return action  # narrowed by the membership test


def policy_gate(rule_floor: Action, agent_action: Action | None) -> Action:
    """The final action; `agent_action` None means the agent did not run or did not decide."""
    floor = _checked(rule_floor, "the rule floor")
    if agent_action is None:
        return floor
    return max_action(floor, _checked(agent_action, "the agent"))
