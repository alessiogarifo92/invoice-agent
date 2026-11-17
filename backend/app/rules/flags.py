"""Flags raised by the rules, their severity, and the action floor they impose.

Severity (docs/decisions.md, "Rules") is what the reviewer reads first; the action floor is
what the policy gate enforces: `final_action = max(rule_floor, agent_action)`.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["approve", "clarify", "block"]
Severity = Literal["info", "warning", "critical"]

ACTION_ORDER: dict[Action, int] = {"approve": 0, "clarify": 1, "block": 2}


@dataclass(frozen=True)
class Flag:
    code: str
    severity: Severity
    action_floor: Action
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def max_action(*actions: Action) -> Action:
    return max(actions, key=ACTION_ORDER.__getitem__, default="approve")


def rule_floor(flags: Iterable[Flag]) -> Action:
    """The strictest action floor among `flags`; `approve` only when nothing was flagged."""
    return max_action(*(f.action_floor for f in flags))
