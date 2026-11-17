"""Proposal reads for the review screen, and the one transaction that records a decision.

`record_decision` runs the whole decision under the invoice row's lock: the If-Match check,
the state machine, the `approvals` row, the status change and its audit event commit
together or not at all. Two people deciding on the same ETag queue on that lock; the second
one then reads the bumped version and gets StaleProposal, so exactly one decision lands.

The ETag is `"<proposal id>.<invoice version>"`. `invoices.version` (0002) is bumped by
every status change, the pipeline's included, so any change a reviewer has not seen makes
their ETag stale; the proposal id keeps one proposal's ETag from matching another's.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.agent.text import plain_text
from app.audit import append_event
from app.extraction.service import FLAG_POLICY
from app.review.machine import Transition, decide

NOTE_MAX = 2000
EVENT_TYPE = "review.decided"

# A proposal is current while no newer one exists for its invoice (newest: created_at, id).
_IS_CURRENT = (
    "NOT EXISTS (SELECT 1 FROM proposals q WHERE q.invoice_id = p.invoice_id "
    "AND (q.created_at, q.id) > (p.created_at, p.id))"
)


class ProposalNotFound(Exception):
    pass


class StaleProposal(Exception):
    """If-Match does not carry the proposal's current ETag."""


@dataclass(frozen=True)
class FlagView:
    code: str
    severity: str
    # Rule flags store their message and floor in `evidence`; extraction flags store only
    # their details, so their message is None and their floor comes from FLAG_POLICY.
    message: str | None
    action_floor: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class EvidenceView:
    ref: str
    # None only if a kept ref has no stored tool result, which the pipeline never writes.
    tool: str | None
    records: list[str]
    output: dict[str, Any] | None


@dataclass(frozen=True)
class DecisionView:
    action: str
    decided_by: str
    note: str | None
    decided_at: datetime


@dataclass(frozen=True)
class ProposalView:
    id: UUID
    invoice_id: UUID
    invoice_status: str
    invoice_version: int
    rule_floor: str
    agent_action: str | None
    final_action: str
    rationale: str
    created_at: datetime
    is_current: bool
    flags: list[FlagView]
    evidence: list[EvidenceView]
    decision: DecisionView | None

    @property
    def etag(self) -> str:
        return proposal_etag(self.id, self.invoice_version)


@dataclass(frozen=True)
class DecisionResult:
    approval_id: UUID
    proposal_id: UUID
    invoice_id: UUID
    transition: Transition
    decided_by: str
    note: str | None
    decided_at: datetime
    invoice_version: int

    @property
    def etag(self) -> str:
        return proposal_etag(self.proposal_id, self.invoice_version)


def proposal_etag(proposal_id: UUID, invoice_version: int) -> str:
    return f'"{proposal_id}.{invoice_version}"'


def _flag(row: Any) -> FlagView:
    evidence: dict[str, Any] = dict(row.evidence or {})
    if row.code in FLAG_POLICY:
        return FlagView(row.code, row.severity, None, FLAG_POLICY[row.code][1], evidence)
    return FlagView(
        row.code,
        row.severity,
        evidence.get("message"),
        evidence.get("action_floor"),
        dict(evidence.get("details") or {}),
    )


def _evidence(refs: list[str], stage_outputs: dict[str, Any] | None) -> list[EvidenceView]:
    """Each kept ref, resolved to the tool result the agent stage stored under it."""
    agent = (stage_outputs or {}).get("agent") or {}
    stored = {e["ref"]: e for e in agent.get("evidence") or []}
    views = []
    for ref in refs:
        entry = stored.get(ref)
        if entry is None:
            views.append(EvidenceView(ref, None, [], None))
        else:
            views.append(
                EvidenceView(ref, entry["tool"], list(entry["records"]), dict(entry["output"]))
            )
    return views


def load_proposal(conn: Connection, proposal_id: UUID) -> ProposalView | None:
    """The proposal as the review screen reads it, in its own short transaction."""
    with conn.begin():
        row = conn.execute(
            text(
                "SELECT p.id, p.invoice_id, p.run_id, p.rule_floor, p.agent_action, "
                "p.final_action, p.rationale, p.evidence_refs, p.created_at, "
                f"i.status, i.version, {_IS_CURRENT} AS is_current "
                "FROM proposals p JOIN invoices i ON i.id = p.invoice_id WHERE p.id = :id"
            ),
            {"id": proposal_id},
        ).one_or_none()
        if row is None:
            return None
        flags = conn.execute(
            text(
                "SELECT code, severity, evidence FROM flags WHERE run_id = :run_id "
                "ORDER BY created_at, id"
            ),
            {"run_id": row.run_id},
        ).all()
        stage_outputs = conn.execute(
            text(
                "SELECT stage_outputs FROM jobs WHERE invoice_id = :invoice_id "
                "AND stage_outputs -> 'rules' ->> 'proposal_id' = :proposal_id "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"invoice_id": row.invoice_id, "proposal_id": str(proposal_id)},
        ).scalar_one_or_none()
        decision = conn.execute(
            text(
                "SELECT decision, decided_by, note, decided_at FROM approvals "
                "WHERE proposal_id = :id ORDER BY decided_at DESC, id DESC LIMIT 1"
            ),
            {"id": proposal_id},
        ).one_or_none()
    return ProposalView(
        id=row.id,
        invoice_id=row.invoice_id,
        invoice_status=row.status,
        invoice_version=row.version,
        rule_floor=row.rule_floor,
        agent_action=row.agent_action,
        final_action=row.final_action,
        rationale=row.rationale,
        created_at=row.created_at,
        is_current=row.is_current,
        flags=[_flag(f) for f in flags],
        evidence=_evidence(list(row.evidence_refs), stage_outputs),
        decision=None
        if decision is None
        else DecisionView(
            action=decision.decision,
            decided_by=decision.decided_by,
            note=decision.note,
            decided_at=decision.decided_at,
        ),
    )


def current_proposal_id(conn: Connection, invoice_id: UUID) -> UUID | None:
    """The invoice's newest proposal, read in its own short transaction."""
    with conn.begin():
        proposal_id: UUID | None = conn.execute(
            text(
                "SELECT id FROM proposals WHERE invoice_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"id": invoice_id},
        ).scalar_one_or_none()
    return proposal_id


def clean_note(note: str | None) -> str | None:
    """The note as stored: plain text (the T8 rules for prose), None when blank."""
    if note is None:
        return None
    return plain_text(note, NOTE_MAX) or None


def record_decision(
    conn: Connection,
    proposal_id: UUID,
    action: str,
    note: str | None,
    reviewer: str,
    if_match: Collection[str],
) -> DecisionResult:
    """One transaction: lock the invoice, check `if_match` against the current ETag, run the
    state machine, then write the approval, the new status and the audit event.

    Raises ProposalNotFound, StaleProposal, or the machine's ReviewError; nothing is written
    then. `reviewer` is the demo token's fingerprint, never the token.
    """
    note = clean_note(note)
    with conn.begin():
        invoice = conn.execute(
            text(
                "SELECT id, status, version FROM invoices "
                "WHERE id = (SELECT invoice_id FROM proposals WHERE id = :id) FOR UPDATE"
            ),
            {"id": proposal_id},
        ).one_or_none()
        if invoice is None:
            raise ProposalNotFound(f"proposal {proposal_id} does not exist")
        if proposal_etag(proposal_id, invoice.version) not in if_match:
            raise StaleProposal("the proposal changed since it was read: reload it")
        # A new statement, so a new snapshot under READ COMMITTED: it sees what the holder
        # of the lock before us committed.
        proposal = conn.execute(
            text(
                "SELECT p.rule_floor, p.agent_action, p.final_action, "
                f"{_IS_CURRENT} AS is_current, "
                "EXISTS (SELECT 1 FROM approvals a WHERE a.proposal_id = p.id) AS decided "
                "FROM proposals p WHERE p.id = :id"
            ),
            {"id": proposal_id},
        ).one()
        transition = decide(
            invoice.status,
            action,
            proposal.final_action,
            note,
            decided=proposal.decided,
            current=proposal.is_current,
        )
        approval_id, decided_at = conn.execute(
            text(
                "INSERT INTO approvals (proposal_id, decision, decided_by, note) "
                "VALUES (:proposal_id, :decision, :decided_by, :note) RETURNING id, decided_at"
            ),
            {
                "proposal_id": proposal_id,
                "decision": transition.action,
                "decided_by": reviewer,
                "note": note,
            },
        ).one()
        version: int = conn.execute(
            text(
                "UPDATE invoices SET status = :status, version = version + 1, "
                "updated_at = now() WHERE id = :id RETURNING version"
            ),
            {"id": invoice.id, "status": transition.to_status},
        ).scalar_one()
        append_event(
            conn,
            reviewer,
            EVENT_TYPE,
            "invoice",
            str(invoice.id),
            {
                "proposal_id": str(proposal_id),
                "approval_id": str(approval_id),
                "action": transition.action,
                "from_status": transition.from_status,
                "to_status": transition.to_status,
                "rule_floor": proposal.rule_floor,
                "agent_action": proposal.agent_action,
                "final_action": transition.proposed,
                "override": transition.override,
                "lowered": transition.lowered,
                "note": note,
            },
        )
    return DecisionResult(
        approval_id=approval_id,
        proposal_id=proposal_id,
        invoice_id=invoice.id,
        transition=transition,
        decided_by=reviewer,
        note=note,
        decided_at=decided_at,
        invoice_version=version,
    )
