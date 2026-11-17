"""The review screen's API: read a proposal (public), decide on it (demo token + If-Match).

GET returns the proposal's ETag; POST .../decision must send it back in If-Match. No
If-Match (or `*`, which proves nothing about what the reviewer saw) is 428, an ETag that is
no longer current is 412. "Approve" only sets `approved_for_payment`: nothing is ever paid.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from app.api.auth import DemoToken, require_demo_token
from app.api.common import ErrorResponse, error_response
from app.db import get_connection
from app.review import store
from app.review.machine import NoteRequired, ReviewError, UnknownAction

router = APIRouter(prefix="/proposals", tags=["proposals"])

# Refusals that are about the request itself; every other one is about the proposal's state.
_UNPROCESSABLE = (UnknownAction, NoteRequired)


class FlagOut(BaseModel):
    code: str
    severity: str
    message: str | None
    action_floor: str | None
    details: dict[str, Any]


class EvidenceOut(BaseModel):
    """A tool result the agent cited, as stored by the agent stage: plain data."""

    ref: str
    tool: str | None
    records: list[str]
    output: dict[str, Any] | None


class DecisionOut(BaseModel):
    action: str
    decided_by: str
    note: str | None
    decided_at: datetime


class ProposalOut(BaseModel):
    id: UUID
    invoice_id: UUID
    invoice_status: str
    rule_floor: str
    agent_action: str | None
    final_action: str
    # Attacker-influenced prose (phase 3, "Agent"): to be shown as plain text only.
    rationale: str
    created_at: datetime
    # False once the invoice has a newer proposal: this one can no longer be decided.
    is_current: bool
    flags: list[FlagOut]
    evidence: list[EvidenceOut]
    decision: DecisionOut | None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "clarify", "block"]
    # Plain text. Required when the action is less severe than the proposal.
    note: str | None = Field(default=None, max_length=store.NOTE_MAX)


class DecisionResponse(BaseModel):
    proposal_id: UUID
    invoice_id: UUID
    action: str
    proposed_action: str
    override: bool
    lowered: bool
    invoice_status: str
    decided_by: str
    note: str | None
    decided_at: datetime


def parse_if_match(header: str | None) -> frozenset[str] | None:
    """The strong entity tags listed in If-Match, or None when it names none (missing,
    blank, or `*`). Weak tags are dropped: If-Match uses the strong comparison."""
    if header is None:
        return None
    tags = [tag.strip() for tag in header.split(",") if tag.strip()]
    if not tags or "*" in tags:
        return None
    return frozenset(tag for tag in tags if not tag.startswith("W/"))


def _proposal_out(view: store.ProposalView) -> ProposalOut:
    return ProposalOut(
        id=view.id,
        invoice_id=view.invoice_id,
        invoice_status=view.invoice_status,
        rule_floor=view.rule_floor,
        agent_action=view.agent_action,
        final_action=view.final_action,
        rationale=view.rationale,
        created_at=view.created_at,
        is_current=view.is_current,
        flags=[
            FlagOut(
                code=f.code,
                severity=f.severity,
                message=f.message,
                action_floor=f.action_floor,
                details=f.details,
            )
            for f in view.flags
        ],
        evidence=[
            EvidenceOut(ref=e.ref, tool=e.tool, records=e.records, output=e.output)
            for e in view.evidence
        ],
        decision=None
        if view.decision is None
        else DecisionOut(
            action=view.decision.action,
            decided_by=view.decision.decided_by,
            note=view.decision.note,
            decided_at=view.decision.decided_at,
        ),
    )


@router.get("/{proposal_id}", response_model=ProposalOut, responses={404: {"model": ErrorResponse}})
def get_proposal(
    proposal_id: UUID,
    response: Response,
    conn: Annotated[Connection, Depends(get_connection)],
) -> ProposalOut | JSONResponse:
    """The proposal, its flags and the tool results it cites; the ETag header is the
    value POST .../decision expects in If-Match."""
    view = store.load_proposal(conn, proposal_id)
    if view is None:
        return error_response(404, "not_found", f"proposal {proposal_id} does not exist")
    response.headers["ETag"] = view.etag
    return _proposal_out(view)


@router.post(
    "/{proposal_id}/decision",
    response_model=DecisionResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        412: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        428: {"model": ErrorResponse},
    },
)
def decide_proposal(
    proposal_id: UUID,
    body: DecisionRequest,
    response: Response,
    conn: Annotated[Connection, Depends(get_connection)],
    token: Annotated[DemoToken, Depends(require_demo_token)],
    if_match: Annotated[str | None, Header()] = None,
) -> DecisionResponse | JSONResponse:
    """A person's decision. The new ETag comes back in the ETag header."""
    tags = parse_if_match(if_match)
    if tags is None:
        return error_response(
            428, "precondition_required", "send the proposal's ETag in an If-Match header"
        )
    try:
        result = store.record_decision(
            conn, proposal_id, body.action, body.note, token.reviewer, tags
        )
    except store.ProposalNotFound as exc:
        return error_response(404, "not_found", str(exc))
    except store.StaleProposal as exc:
        return error_response(412, "stale_proposal", str(exc))
    except ReviewError as exc:
        status = 422 if isinstance(exc, _UNPROCESSABLE) else 409
        return error_response(status, exc.code, str(exc))
    response.headers["ETag"] = result.etag
    t = result.transition
    return DecisionResponse(
        proposal_id=result.proposal_id,
        invoice_id=result.invoice_id,
        action=t.action,
        proposed_action=t.proposed,
        override=t.override,
        lowered=t.lowered,
        invoice_status=t.to_status,
        decided_by=result.decided_by,
        note=result.note,
        decided_at=result.decided_at,
    )
