"""The agent stage: the tool-using triage agent (T8) and its interface.

Phase 3, "Agent": the agent runs only on a flagged invoice (the pipeline decides), triages
and explains, and can only raise the rule floor (the policy gate). `TriageAgent` runs one
tool loop through the gateway (ledger, caps, fallback; QuotaUnavailable propagates so the
worker parks the invoice), with the tools of app.agent.tools bound to the invoice.

`decide` maps the loop's outcome to a decision, failing closed: a limit reached, invalid
tool arguments beyond the retry, a refusal or any reply without a proposal, a provider
error, a proposal outside the schema, or a cited ref this run never returned: `clarify`,
with a fixed rationale of ours rather than the model's prose.

`triage` is the no-op agent kept from T7: the Worker's default when none is wired in (the
pipeline tests); `build_worker` wires `TriageAgent`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, text

from app.agent.policy import forged_refs
from app.agent.prompt import AGENT_SYSTEM_PROMPT, FlagLine, build_agent_message
from app.agent.tools import PROPOSE, Evidence, InvoiceTools, PaymentProposal, load_scope, read_only
from app.extraction.service import load_stored_extraction
from app.llm.gateway import LLMGateway
from app.llm.types import Limits, ToolLoopResult
from app.rules.flags import Action

PURPOSE = "agent"


@dataclass(frozen=True)
class AgentRequest:
    """What the agent stage knows about the invoice under review."""

    invoice_id: UUID
    run_id: UUID
    proposal_id: UUID
    rule_floor: Action
    flag_codes: tuple[str, ...]
    # The document's visible text (hidden glyphs excluded), as the extraction saw it.
    invoice_text: str = ""


@dataclass(frozen=True)
class AgentDecision:
    action: Action
    # Attacker-influenced prose (phase 3): stored and shown as plain text only.
    rationale: str
    evidence_refs: tuple[str, ...] = ()
    model: str | None = None
    provider: str | None = None
    # "completed", another ToolLoopResult stop reason, "invalid_proposal" or "forged_evidence".
    stop_reason: str = "completed"
    # The model's own action before vetting; None when it never proposed one.
    proposed_action: Action | None = None
    forged_refs: tuple[str, ...] = ()
    # Everything this run's tools returned: the only refs a proposal may cite.
    evidence: tuple[Evidence, ...] = ()
    tool_calls: tuple[str, ...] = ()

    @property
    def returned_refs(self) -> tuple[str, ...]:
        return tuple(e.ref for e in self.evidence)


Agent = Callable[[AgentRequest], AgentDecision | None]

_REVIEW = " A person must review the invoice."
# Our own words for every way the agent can fail to propose: never the model's prose.
STOPPED: dict[str, str] = {
    "max_tool_calls": "The agent reached its tool-call limit before proposing an action." + _REVIEW,
    "timeout": "The agent ran out of time before proposing an action." + _REVIEW,
    "no_proposal": "The agent ended without proposing an action." + _REVIEW,
    "invalid_arguments": "The agent kept calling its tools with invalid arguments." + _REVIEW,
    "error": "The agent's model call failed." + _REVIEW,
    "invalid_proposal": "The agent's proposal did not match the proposal schema." + _REVIEW,
    "forged_evidence": "The agent's proposal cited evidence that its tools never returned, "
    "so it was set aside." + _REVIEW,
}


def decide(result: ToolLoopResult, evidence: Sequence[Evidence]) -> AgentDecision:
    """The decision handed to the policy gate for one tool loop's outcome."""

    def clarify(reason: str) -> AgentDecision:
        return AgentDecision(
            "clarify",
            STOPPED[reason],
            model=result.model or None,
            provider=result.provider or None,
            stop_reason=reason,
            evidence=tuple(evidence),
            tool_calls=tuple(r.call.name for r in result.tool_calls),
        )

    if result.stop_reason != "completed" or result.proposal is None:
        return clarify(result.stop_reason if result.stop_reason in STOPPED else "no_proposal")
    try:
        proposal = PaymentProposal.model_validate(result.proposal)
    except ValidationError:
        return clarify("invalid_proposal")
    forged = forged_refs(proposal.evidence_refs, {e.ref for e in evidence})
    if forged:
        return replace(
            clarify("forged_evidence"), proposed_action=proposal.action, forged_refs=forged
        )
    return replace(
        clarify("no_proposal"),
        action=proposal.action,
        rationale=proposal.rationale,
        evidence_refs=tuple(dict.fromkeys(proposal.evidence_refs)),
        stop_reason="completed",
        proposed_action=proposal.action,
    )


def _run_flags(conn: Connection, invoice_id: UUID, run_id: UUID) -> list[FlagLine]:
    owner = conn.execute(
        text("SELECT invoice_id FROM processing_runs WHERE id = :id"), {"id": run_id}
    ).scalar_one()
    if owner != invoice_id:
        raise ValueError(f"run {run_id} does not belong to invoice {invoice_id}")
    rows = conn.execute(
        text(
            "SELECT code, severity, evidence FROM flags WHERE run_id = :id ORDER BY created_at, id"
        ),
        {"id": run_id},
    ).all()
    return [(r.code, r.severity, (r.evidence or {}).get("message")) for r in rows]


class TriageAgent:
    """The tool-using agent over one engine and gateway; one call reviews one invoice."""

    def __init__(
        self,
        engine: Engine,
        gateway: LLMGateway,
        limits: Limits | None = None,
        *,
        ref_tag: str | None = None,
    ):
        self._engine = engine
        self._gateway = gateway
        self._limits = limits or Limits()
        # Fixed only in tests; otherwise each run draws its own random tag.
        self._ref_tag = ref_tag

    @property
    def limits(self) -> Limits:
        return self._limits

    def __call__(self, request: AgentRequest) -> AgentDecision:
        with read_only(self._engine) as conn:
            flags = _run_flags(conn, request.invoice_id, request.run_id)
            stored = load_stored_extraction(conn, request.run_id)
            scope = load_scope(conn, str(request.invoice_id), stored.fields)
        tools = InvoiceTools(self._engine, scope, ref_tag=self._ref_tag)
        message = build_agent_message(
            rule_floor=request.rule_floor,
            flags=flags,
            fields=stored.fields,
            invoice_text=request.invoice_text,
            max_tool_calls=self._limits.max_tool_calls,
        )
        result = self._gateway.run_tools(
            PURPOSE,
            AGENT_SYSTEM_PROMPT,
            message,
            tools.specs,
            tools.handlers,
            PROPOSE,
            self._limits,
            invoice_id=request.invoice_id,
        )
        return decide(result, tools.evidence)


def triage(request: AgentRequest) -> AgentDecision | None:
    """The agent that never runs (T7): the proposal keeps the rule floor."""
    return None
