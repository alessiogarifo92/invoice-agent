"""The human-review state machine (phase 3, "review"): pure, no database.

A person decides on a proposal once, while its invoice is `pending_approval`. Each action
has exactly one target status, and none of them pays anything: `approve` only marks the
invoice `approved_for_payment` (phase 3, "Flow", step 4).

The person may override the proposal (docs/decisions.md is silent on human overrides, so
T9 allows them): any action other than the proposal's `final_action` is an override, and an
action *less* severe than it ("lowered") needs a note saying why. Both actions go into the
audit event, so an override is never silent.

Every refusal is a ReviewError with a stable `code`, mapped to an HTTP status by the API.
"""

from dataclasses import dataclass

from app.rules.flags import ACTION_ORDER, Action

REVIEW_STATUS = "pending_approval"

# (current invoice status, human action) -> new invoice status. Nothing else is legal.
TRANSITIONS: dict[tuple[str, Action], str] = {
    (REVIEW_STATUS, "approve"): "approved_for_payment",
    (REVIEW_STATUS, "clarify"): "clarification_requested",
    (REVIEW_STATUS, "block"): "blocked",
}


class ReviewError(Exception):
    code = "review_error"


class UnknownAction(ReviewError):
    code = "unknown_action"


class InvalidTransition(ReviewError):
    code = "invalid_transition"


class AlreadyDecided(ReviewError):
    code = "already_decided"


class ProposalSuperseded(ReviewError):
    code = "proposal_superseded"


class NoteRequired(ReviewError):
    code = "note_required"


@dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    action: Action
    # The proposal's final_action, which the person followed or overrode.
    proposed: Action
    # The action differs from the proposal.
    override: bool
    # The action is less severe than the proposal (an override that needs a note).
    lowered: bool


def _action(value: str) -> Action | None:
    for action in ACTION_ORDER:
        if action == value:
            return action
    return None


def decide(
    status: str,
    action: str,
    proposed: str,
    note: str | None,
    *,
    decided: bool = False,
    current: bool = True,
) -> Transition:
    """The transition for a person's `action` on a proposal, or the ReviewError refusing it.

    `status` is the invoice's current status, `proposed` the proposal's final_action,
    `decided` whether the proposal already has a decision, `current` whether it is still
    the invoice's newest proposal. `note` is the plain-text note, blank meaning none.
    """
    chosen = _action(action)
    if chosen is None:
        raise UnknownAction(f"unknown action {action!r}: expected approve, clarify or block")
    proposal = _action(proposed)
    if proposal is None:
        raise ValueError(f"the proposal carries an unknown action {proposed!r}")
    if decided:
        raise AlreadyDecided("this proposal has already been decided")
    if not current:
        raise ProposalSuperseded("a newer proposal exists for this invoice")
    target = TRANSITIONS.get((status, chosen))
    if target is None:
        raise InvalidTransition(f"an invoice in status {status!r} cannot be decided")
    lowered = ACTION_ORDER[chosen] < ACTION_ORDER[proposal]
    if lowered and not (note and note.strip()):
        raise NoteRequired(
            f"{chosen} is less severe than the proposed {proposal}: a note must say why"
        )
    return Transition(
        from_status=status,
        to_status=target,
        action=chosen,
        proposed=proposal,
        override=chosen != proposal,
        lowered=lowered,
    )
