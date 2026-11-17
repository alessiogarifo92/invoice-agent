"""Deterministic invoice rules: pure functions of (InvoiceFacts, RuleContext), fail closed."""

from app.rules.context import RuleContext
from app.rules.facts import InvoiceFacts, parse_facts
from app.rules.flags import Action, Flag, Severity, max_action, rule_floor
from app.rules.runner import RULES, run_rules

__all__ = [
    "RULES",
    "Action",
    "Flag",
    "InvoiceFacts",
    "RuleContext",
    "Severity",
    "max_action",
    "parse_facts",
    "rule_floor",
    "run_rules",
]
