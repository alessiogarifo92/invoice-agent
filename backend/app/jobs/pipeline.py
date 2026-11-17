"""The invoice pipeline, one committed stage at a time (phase 3, "Flow", step 2).

    extract (LLM call + grounding) -> rules (matching + rules + rule floor)
        -> agent (tool loop, flagged invoices only) -> propose (policy gate, pending_approval)

Each stage commits, in one transaction taken after `lock_owned`: its writes, its output in
jobs.stage_outputs, the next stage, and one audit event. A restarted job resumes at the stage
recorded on its row, so a completed stage is never redone and an extraction is never paid
for twice. The LLM call (and later the agent) runs outside any transaction and outside the
row lock; only its result is committed. A new job for an invoice whose extraction is already
stored for the current prompt version reuses it too (`find_stored_extraction`).

Errors propagate to the worker, which decides: QuotaUnavailable parks the invoice, LeaseLost
abandons the job, Stopping hands it back, anything else is retried or fails the job.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, bindparam, text

from app.agent import Agent, AgentDecision, AgentRequest, policy_gate
from app.agent.prompt import AGENT_PROMPT_VERSION
from app.audit import append_event
from app.extraction.service import (
    FLAG_POLICY,
    ExtractionFlag,
    StoredExtraction,
    extract_invoice,
    find_stored_extraction,
    load_stored_extraction,
    persist_extraction,
)
from app.ingestion import ExtractedDocument, extract_document
from app.ingestion.hidden import visible_text
from app.jobs import queue
from app.llm.gateway import LLMGateway
from app.matching.context import build_context, document_signals
from app.matching.sql import SqlMatcher
from app.rules.facts import parse_facts
from app.rules.flags import Action, Flag
from app.rules.persist import persist_rule_results
from app.rules.runner import run_rules

ACTOR = queue.ACTOR
STAGES = ("extract", "rules", "agent", "propose")


class Stopping(Exception):
    """The worker is shutting down: the job is handed back at the next stage boundary."""


@dataclass
class _Run:
    engine: Engine
    job: queue.Job
    owner: str
    gateway: LLMGateway
    agent: Agent
    outputs: dict[str, Any]
    _document: ExtractedDocument | None = field(default=None, repr=False)

    @property
    def invoice_id(self) -> UUID:
        return self.job.invoice_id

    def document(self) -> ExtractedDocument:
        """The stored PDF, parsed once per run (pdfplumber is CPU-bound; this is the worker's
        thread, never the event loop)."""
        if self._document is None:
            with self.engine.connect() as conn:
                pdf = conn.execute(
                    text("SELECT pdf FROM invoices WHERE id = :id"), {"id": self.invoice_id}
                ).scalar_one()
            self._document = extract_document(bytes(pdf))
        return self._document

    def commit(
        self,
        conn: Connection,
        stage: str,
        output: dict[str, Any],
        next_stage: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Store this stage's output, move the job on (or finish it), append the event."""
        outputs = {**self.outputs, stage: output}
        if next_stage is None:
            queue.complete(conn, self.job.id, outputs)
        else:
            queue.advance(conn, self.job.id, next_stage, outputs)
        append_event(
            conn,
            ACTOR,
            event_type,
            "invoice",
            str(self.invoice_id),
            {"job_id": str(self.job.id), **payload},
        )
        self.outputs = outputs


def _extraction_output(stored: StoredExtraction, reused: bool) -> dict[str, Any]:
    return {
        "run_id": stored.run_id,
        "provider": stored.provider,
        "model": stored.model,
        "prompt_version": stored.prompt_version,
        "extracted": stored.fields is not None,
        "flags": [f.code for f in stored.flags],
        "action_floor": stored.action_floor,
        "reused": reused,
    }


def _extract(run: _Run) -> str:
    with run.engine.connect() as conn:
        stored = find_stored_extraction(conn, run.invoice_id)
    outcome = None
    if stored is None:
        # The only paid call of the pipeline so far; QuotaUnavailable propagates from here.
        outcome = extract_invoice(run.gateway, run.document(), run.invoice_id)
    with run.engine.begin() as conn:
        queue.lock_owned(conn, run.job.id, run.owner)
        if outcome is not None:
            stored = load_stored_extraction(conn, persist_extraction(conn, run.invoice_id, outcome))
        if stored is None:  # pragma: no cover - either found above or just persisted
            raise RuntimeError(f"invoice {run.invoice_id} has no extraction run")
        output = _extraction_output(stored, reused=outcome is None)
        run.commit(conn, "extract", output, "rules", "extraction.completed", output)
    return "rules"


def _as_rule_flag(flag: ExtractionFlag) -> Flag:
    return Flag(flag.code, flag.severity, flag.action_floor, flag.message, dict(flag.evidence))


def _existing_proposal(conn: Connection, run_id: str) -> tuple[UUID, Action, list[str]] | None:
    """The run's proposal and its rule flag codes, when an earlier job already wrote them."""
    proposal = conn.execute(
        text(
            "SELECT id, rule_floor FROM proposals WHERE run_id = CAST(:run_id AS uuid) "
            "ORDER BY created_at, id LIMIT 1"
        ),
        {"run_id": run_id},
    ).one_or_none()
    if proposal is None:
        return None
    codes = conn.execute(
        text(
            "SELECT code FROM flags WHERE run_id = CAST(:run_id AS uuid) "
            "AND code NOT IN :extraction_codes ORDER BY created_at, id"
        ).bindparams(bindparam("extraction_codes", expanding=True)),
        {"run_id": run_id, "extraction_codes": list(FLAG_POLICY)},
    ).scalars()
    return proposal.id, proposal.rule_floor, list(codes)


def _rules(run: _Run) -> str:
    run_id = str(run.outputs["extract"]["run_id"])
    # The PDF signals are deterministic and free: recomputed on a resume, never stored.
    findings, sweep = document_signals(run.document())
    with run.engine.begin() as conn:
        queue.lock_owned(conn, run.job.id, run.owner)
        stored = load_stored_extraction(conn, run_id)
        existing = _existing_proposal(conn, run_id)
        if existing is not None:
            proposal_id, floor, codes = existing
        else:
            facts = parse_facts(stored.fields or {})
            context = build_context(SqlMatcher(conn), facts, str(run.invoice_id), findings, sweep)
            flags = run_rules(facts, context)
            saved = persist_rule_results(
                conn,
                run.invoice_id,
                run_id,
                flags,
                extraction_flags=[_as_rule_flag(f) for f in stored.flags],
            )
            proposal_id, floor, codes = saved.proposal_id, saved.rule_floor, [f.code for f in flags]
        output = {
            "proposal_id": str(proposal_id),
            "rule_floor": floor,
            "flags": codes,
            "reused": existing is not None,
        }
        run.commit(conn, "rules", output, "agent", "rules.evaluated", {"run_id": run_id, **output})
    return "agent"


def _agent_output(decision: AgentDecision | None, skipped: str | None) -> dict[str, Any]:
    """The agent stage's stored output: the decision and every tool result it may cite."""
    if decision is None:
        return {"action": None, "skipped": skipped}
    return {
        "action": decision.action,
        "rationale": decision.rationale,
        "evidence_refs": list(decision.evidence_refs),
        "model": decision.model,
        "provider": decision.provider,
        "prompt_version": AGENT_PROMPT_VERSION,
        "stop_reason": decision.stop_reason,
        "proposed_action": decision.proposed_action,
        "forged_refs": list(decision.forged_refs),
        "tool_calls": list(decision.tool_calls),
        "evidence": [
            {"ref": e.ref, "tool": e.tool, "records": list(e.records), "output": e.output}
            for e in decision.evidence
        ],
        "skipped": None,
    }


def _completed_payload(output: dict[str, Any]) -> dict[str, Any]:
    """agent.completed: the action, the refs and the record ids they stand for. The prose
    (attacker-influenced) and the tool outputs stay out of the chain, in the stage output."""
    keys = (
        "action",
        "proposed_action",
        "stop_reason",
        "model",
        "provider",
        "prompt_version",
        "evidence_refs",
        "forged_refs",
        "tool_calls",
    )
    return {
        **{key: output[key] for key in keys},
        "evidence": [
            {"ref": e["ref"], "tool": e["tool"], "records": e["records"]}
            for e in output["evidence"]
        ],
    }


def _agent(run: _Run) -> str:
    rules = run.outputs["rules"]
    flag_codes = tuple([*run.outputs["extract"]["flags"], *rules["flags"]])
    decision: AgentDecision | None = None
    # Phase 3: the agent runs only on a flagged invoice.
    skipped = "no_flags"
    if flag_codes:
        # The tool loop's LLM calls; QuotaUnavailable propagates from here.
        decision = run.agent(
            AgentRequest(
                invoice_id=run.invoice_id,
                run_id=UUID(run.outputs["extract"]["run_id"]),
                proposal_id=UUID(rules["proposal_id"]),
                rule_floor=rules["rule_floor"],
                flag_codes=flag_codes,
                invoice_text=visible_text(run.document()),
            )
        )
        skipped = "no_decision"
    output = _agent_output(decision, None if decision is not None else skipped)
    with run.engine.begin() as conn:
        queue.lock_owned(conn, run.job.id, run.owner)
        if decision is None:
            run.commit(conn, "agent", output, "propose", "agent.skipped", {"reason": skipped})
        else:
            payload = _completed_payload(output)
            run.commit(conn, "agent", output, "propose", "agent.completed", payload)
    return "propose"


def _propose(run: _Run) -> None:
    rules = run.outputs["rules"]
    agent = run.outputs["agent"]
    floor: Action = rules["rule_floor"]
    agent_action: Action | None = agent["action"]
    # The gate checks the citations again, whatever agent produced them: only refs that
    # this run's tools returned are kept, and any other one makes the agent's action clarify.
    returned = {e["ref"] for e in agent.get("evidence", [])}
    cited: list[str] = agent.get("evidence_refs", [])
    final = policy_gate(floor, agent_action, cited, returned)
    refs = [ref for ref in cited if ref in returned]
    with run.engine.begin() as conn:
        queue.lock_owned(conn, run.job.id, run.owner)
        conn.execute(
            text(
                "UPDATE proposals SET agent_action = :agent_action, final_action = :final, "
                "rationale = COALESCE(:rationale, rationale), "
                "evidence_refs = CAST(:refs AS jsonb) WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": rules["proposal_id"],
                "agent_action": agent_action,
                "final": final,
                "rationale": agent.get("rationale"),
                "refs": json.dumps(refs),
            },
        )
        conn.execute(
            text(
                "UPDATE processing_runs SET finished_at = now(), agent_model = :model "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": run.outputs["extract"]["run_id"], "model": agent.get("model")},
        )
        queue.set_invoice_status(conn, run.invoice_id, "pending_approval")
        payload = {
            "proposal_id": rules["proposal_id"],
            "rule_floor": floor,
            "agent_action": agent_action,
            "final_action": final,
            "evidence_refs": refs,
        }
        run.commit(conn, "propose", {"final_action": final}, None, "proposal.created", payload)


_STAGE_FUNCTIONS: dict[str, Callable[[_Run], str | None]] = {
    "extract": _extract,
    "rules": _rules,
    "agent": _agent,
    "propose": _propose,
}


def run_pipeline(
    engine: Engine,
    job: queue.Job,
    owner: str,
    gateway: LLMGateway,
    agent: Agent,
    check: Callable[[], None],
) -> None:
    """Run `job` from its recorded stage to the end. `check` runs before every stage and
    raises LeaseLost or Stopping when the worker must not go on."""
    run = _Run(engine, job, owner, gateway, agent, dict(job.stage_outputs))
    stage: str | None = job.stage
    while stage is not None:
        check()
        if stage not in _STAGE_FUNCTIONS:
            raise ValueError(f"job {job.id} is at an unknown stage {stage!r}")
        stage = _STAGE_FUNCTIONS[stage](run)
