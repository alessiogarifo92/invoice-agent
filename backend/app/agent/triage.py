"""The agent stage's interface, with the T7 stub: no LLM, no decision.

T8 replaces `triage` with the tool-using agent. Until then the pipeline calls this stub on
flagged invoices, records `agent_action` NULL, and the policy gate leaves the rule floor.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.rules.flags import Action


@dataclass(frozen=True)
class AgentRequest:
    """What the agent stage knows about the invoice under review."""

    invoice_id: UUID
    run_id: UUID
    proposal_id: UUID
    rule_floor: Action
    flag_codes: tuple[str, ...]


@dataclass(frozen=True)
class AgentDecision:
    action: Action
    # Attacker-influenced prose (phase 3): stored and shown as plain text only.
    rationale: str
    evidence_refs: tuple[str, ...] = ()
    model: str | None = None


Agent = Callable[[AgentRequest], AgentDecision | None]


def triage(request: AgentRequest) -> AgentDecision | None:
    """T7 stub: the agent never runs, so the proposal keeps the rule floor."""
    return None
